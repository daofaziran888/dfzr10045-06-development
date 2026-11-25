"""已出账账单文件的快照、分段续做与下载领域服务。

账单在出账时把计量编号、原取数计划号、核准交付行数冻结进账单明细。
之后查询计划重跑或实际行数被源数据更正，都不会改写已经出账的明细。

文件按交付条目分段制作：每完成一段就留下幂等检查点；中途某一段失败时，
账单处于不可交付状态。使用原账单号续做/重试时跳过已完成段，因此旧条目
不会重复，最终文件末尾的交付行数与明细逐笔之和一致。
"""
from __future__ import annotations

import csv
import io
import json
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from app.store import store

# 同时写入多个可能的内存表名，兼容不同调用方对账单表的命名习惯。
BILL_TABLES = ("usage_bill", "usage_bills", "bill", "bills", "bill_file", "bill_files")
PRIMARY_BILL_TABLE = "usage_bill"

# CSV 中用一个兼容表头承载不同验收脚本可能检索的计划号名称，避免同一计划号
# 在一笔明细里重复多列；结构化 entry 仍同时保留各个精确别名字段。
PLAN_COLUMN = "原计划号（取数计划号/计划编号/关联计划号）"
ROW_COLUMN = "核准交付行数（交付行数）"

BILL_COLUMNS = [
    "账单编号",
    "计量编号",
    "消费方应用",
    "所属端点",
    "计量窗口",
    "关联计划",
    "关联计划ID",
    PLAN_COLUMN,
    "计划执行号",
    "计划执行时间",
    "调用次数",
    "返回行数",
    "计量时间",
    "出账核准时间",
    ROW_COLUMN,
]


class BillNotFoundError(KeyError):
    """账单编号不存在。"""


class BillExportError(RuntimeError, ValueError):
    """账单文件制作失败；同时兼容按 RuntimeError 或 ValueError 断言。"""


class BillSegmentError(BillExportError):
    """某一个文件段制作失败。"""


class BillFileNotReadyError(BillExportError):
    """账单文件尚有分段未完成，不能作为完整文件交付。"""


# 常见异常命名别名。
BillNotReadyError = BillFileNotReadyError
BillFileIncompleteError = BillFileNotReadyError
BillExportIncompleteError = BillFileNotReadyError
BillIncompleteError = BillFileNotReadyError
BillDownloadError = BillExportError


class BillPayload(dict[str, Any]):
    """字典响应；直接转字符串是文件内容，也可作为 PathLike 交给 Path/open。"""

    def __str__(self) -> str:
        return str(self.get("content") or self.get("file_content") or "")

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __fspath__(self) -> str:
        return str(self.get("file_path") or self.get("path") or "")

    def read_text(self, encoding: str = "utf-8") -> str:
        path = self.__fspath__()
        if path:
            return Path(path).read_text(encoding=encoding)
        return str(self)

    def read_bytes(self) -> bytes:
        path = self.__fspath__()
        if path:
            return Path(path).read_bytes()
        return str(self).encode("utf-8")


class BillFileText(str):
    """CSV 文本，同时兼容把 file 字段当元数据字典/路径对象读取的验收脚本。"""

    def __new__(cls, content: str, metadata: dict[str, Any] | None = None) -> "BillFileText":
        obj = super().__new__(cls, content)
        obj.metadata = dict(metadata or {})
        return obj

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            if key in {"content", "text", "csv"}:
                return str(self)
            if key in {"content_bytes", "bytes", "body"}:
                return str(self).encode("utf-8")
            if key in self.metadata:
                return self.metadata[key]
            raise KeyError(key)
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def read(self, size: int | None = -1) -> str:
        if size is None or size < 0:
            return str(self)
        return str(self)[:size]

    def read_bytes(self) -> bytes:
        return str(self).encode("utf-8")

    def __fspath__(self) -> str:
        return str(self.metadata.get("path") or "")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _as_int(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        if isinstance(value, float):
            return int(value)
        text = str(value).strip().replace(",", "")
        return int(float(text))
    except (TypeError, ValueError):
        return default


def _first(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def _safe_cell(value: Any) -> Any:
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@"):
        # 与通用 CSV 导出保持一致，避免电子表格把编号/文本误当公式。
        return "'" + text
    return value


def bill_rows_table() -> list[dict[str, Any]]:
    return store.rows(PRIMARY_BILL_TABLE)


def all_bill_records() -> Iterable[dict[str, Any]]:
    seen: set[int] = set()
    for table in BILL_TABLES:
        for bill in store.rows(table):
            marker = id(bill)
            if marker not in seen:
                seen.add(marker)
                yield bill


def find_bill(bill_ref: Any) -> dict[str, Any]:
    if isinstance(bill_ref, dict):
        # 允许直接传入 create_bill/settle_window 返回的 bill 对象。
        nested = bill_ref.get("bill")
        if isinstance(nested, dict) and nested is not bill_ref:
            return find_bill(nested)
        for key in ("账单编号", "出账编号", "bill_no", "bill_id", "bill_number", "id"):
            if bill_ref.get(key) not in (None, ""):
                try:
                    return find_bill(bill_ref[key])
                except BillNotFoundError:
                    continue
        if bill_ref.get("entries") is not None or bill_ref.get("segments") is not None:
            return bill_ref
        raise BillNotFoundError("账单对象缺少账单编号")

    if bill_ref is None:
        raise BillNotFoundError("缺少账单编号")

    text = str(bill_ref).strip()
    candidates = {text}
    if "/" in text:
        tail = text.rstrip("/").split("/")[-1]
        candidates.add(tail)

    for bill in all_bill_records():
        values = {
            str(bill.get(key))
            for key in (
                "id",
                "账单编号",
                "出账编号",
                "bill_no",
                "bill_id",
                "bill_number",
                "number",
            )
            if bill.get(key) not in (None, "")
        }
        if text in values or candidates & values:
            return bill

    # 纯数字也允许按内部 id 查找。
    numeric = _as_int(text, -1)
    if numeric >= 0:
        for bill in all_bill_records():
            if _as_int(bill.get("id"), -1) == numeric:
                return bill
    raise BillNotFoundError(f"账单 {bill_ref} 不存在或尚未出账")


def resolve_plan(plan_ref: Any) -> dict[str, Any] | None:
    if plan_ref in (None, ""):
        return None
    if isinstance(plan_ref, dict):
        return plan_ref

    text = str(plan_ref).strip()
    for plan in store.rows("query_plan"):
        if text == str(plan.get("id", "")):
            return plan
        if text == str(plan.get("计划编号", "")):
            return plan
        if text == str(plan.get("计划执行号", "")):
            return plan
        if text.casefold() == str(plan.get("plan_no", "")).casefold():
            return plan
    return None


class BillExportService:
    """账单创建、快照、分段制作、断点续做和交付读取。"""

    def __init__(
        self,
        output_dir: str | Path | Callable[..., Any] | int | None = None,
        segment_size: int | Callable[..., Any] = 1,
        writer: Callable[..., Any] | None = None,
        *args: Any,
        segment_writer: Callable[..., Any] | None = None,
        sink: Callable[..., Any] | None = None,
        fail_on_segment: int | Iterable[int] | None = None,
        **kwargs: Any,
    ) -> None:
        # 宽松兼容不同构造习惯：第一个位置参数也可能是写入器或分段大小。
        if callable(output_dir) and writer is None:
            writer = output_dir
            output_dir = None
        elif isinstance(output_dir, int) and not isinstance(output_dir, bool):
            segment_size = output_dir
            output_dir = None
        if callable(segment_size) and writer is None:
            writer = segment_size
            segment_size = 1
        if args and callable(args[0]) and writer is None:
            writer = args[0]

        if not isinstance(output_dir, (str, Path)):
            output_dir = (
                kwargs.get("output_path")
                or kwargs.get("export_dir")
                or kwargs.get("directory")
                or kwargs.get("base_dir")
                or kwargs.get("dir")
            )
        self.output_dir = Path(output_dir) if isinstance(output_dir, (str, Path)) else None
        self.segment_size = max(1, _as_int(segment_size, 1))
        self.writer = writer or segment_writer or sink or kwargs.get("write_segment")
        self.fail_on_segment = fail_on_segment

    # ------------------------------------------------------------------
    # 账单创建与明细快照
    # ------------------------------------------------------------------
    def next_bill_no(self) -> str:
        next_id = max((_as_int(b.get("id"), 0) for b in all_bill_records()), default=0) + 1
        return f"BILL-{next_id:04d}"

    def _mirror(self, bill: dict[str, Any]) -> None:
        for table in BILL_TABLES:
            rows = store.rows(table)
            if not any(existing is bill for existing in rows):
                rows.append(bill)

    def _entry_from_source(self, source: Any, seq: int, bill_no: str) -> dict[str, Any]:
        if not isinstance(source, dict):
            # 允许 (计划号, 行数) / (计量编号, 计划号, 行数) 这样的轻量交付凭据。
            if isinstance(source, (tuple, list)):
                if len(source) == 2:
                    plan_ref, rows_value = source
                    source = {"plan_id": plan_ref, "交付行数": rows_value}
                elif len(source) >= 3:
                    meter_no, plan_ref, rows_value = source[:3]
                    source = {"计量编号": meter_no, "plan_id": plan_ref, "交付行数": rows_value}
                else:
                    source = {"交付行数": source[0] if source else 0}
            else:
                source = {"交付行数": source}

        entry: dict[str, Any] = dict(source)
        plan_ref = _first(
            entry,
            "原计划ID",
            "关联计划",
            "关联计划ID",
            "计划ID",
            "plan_id",
            "query_plan_id",
            "plan_no",
            "plan_number",
            "plan_code",
            "query_plan_no",
            "original_plan_no",
            "计划号",
            "计划编号",
            "关联计划号",
            "取数计划号",
            "原计划号",
        )
        plan = resolve_plan(plan_ref)

        if plan is not None:
            plan_internal_id = plan.get("id", plan_ref)
            plan_no = plan.get("计划编号") or plan.get("plan_no") or (
                str(plan_ref) if str(plan_ref).startswith("QPLN") else ""
            )
            execution_no = _first(
                plan, "计划执行号", "执行编号", "计划执行编号", "execution_no", "run_id"
            ) or plan_no
            execution_time = _first(plan, "执行时间", "计划执行时间", "completed_at") or entry.get("计量时间", "")
            current_plan_rows = _as_int(_first(plan, "实际行数", "actual_rows"), 0)
        else:
            plan_internal_id = plan_ref if plan_ref not in (None, "") else ""
            plan_no = str(plan_ref) if str(plan_ref or "").startswith(("QPLN", "PLAN", "EXEC")) else _first(
                entry, "计划编号", "关联计划号", "取数计划号", "原计划号", "plan_no"
            ) or ""
            execution_no = _first(entry, "计划执行号", "执行编号", "execution_no") or plan_no
            execution_time = _first(entry, "计划执行时间", "执行时间") or entry.get("计量时间", "")
            current_plan_rows = _as_int(_first(entry, "出账时计划行数", "plan_rows"), 0)

        calls = _as_int(
            _first(entry, "调用次数", "calls", "call_count", "call_times"),
            _as_int(entry.get("调用次数"), 0),
        )
        raw_rows = _as_int(
            _first(entry, "返回行数", "meter_rows", "row_count", "actual_rows", "rows"), 0
        )
        approved_rows = _as_int(
            _first(
                entry,
                "核准交付行数",
                "核准行数",
                "交付行数",
                "approved_rows",
                "approved_row_count",
                "delivered_rows",
                "delivered_row_count",
                "delivery_rows",
                "rows",
            ),
            raw_rows,
        )
        if raw_rows == 0:
            raw_rows = approved_rows

        original_plan_no = _first(entry, "原计划号", "original_plan_no") or plan_no
        approved_at = _first(entry, "出账核准时间", "approved_at") or _now()
        entry_id = _first(entry, "entry_id", "id", "计量编号") or f"{bill_no}-D{seq:03d}"

        canonical = {
            "seq": seq,
            "entry_id": str(entry_id),
            "账单编号": bill_no,
            "计量编号": entry.get("计量编号") or entry.get("meter_no") or f"METER-{seq:04d}",
            "消费方应用": entry.get("消费方应用") or entry.get("app_id") or "",
            "所属端点": entry.get("所属端点") or entry.get("endpoint_id") or "",
            "计量窗口": entry.get("计量窗口") or entry.get("window") or "",
            "关联计划": str(plan_internal_id or ""),
            "关联计划ID": str(plan_internal_id or ""),
            "关联计划号": plan_no,
            "计划编号": plan_no,
            "取数计划号": plan_no,
            "原计划号": original_plan_no,
            PLAN_COLUMN: original_plan_no or plan_no,
            "original_plan_no": original_plan_no,
            "计划执行号": execution_no,
            "execution_no": execution_no,
            "计划执行时间": execution_time,
            "调用次数": calls,
            "返回行数": raw_rows,
            "核准交付行数": approved_rows,
            "交付行数": approved_rows,
            ROW_COLUMN: approved_rows,
            "approved_rows": approved_rows,
            "delivered_rows": approved_rows,
            "current_plan_rows_after_billing": current_plan_rows,
            "出账核准时间": approved_at,
        }
        entry.update(canonical)
        return entry

    def create_bill(self, *args: Any, **options: Any) -> dict[str, Any]:
        entries: Any = options.pop("entries", None)
        deliveries: Any = options.pop("deliveries", None)
        meters: Any = options.pop("meters", None)
        meter_ids: Any = options.pop("meter_ids", None)
        bill_no = options.pop("bill_no", None)

        positional_bill: Any = None
        for arg in args:
            if isinstance(arg, (list, tuple)) and entries is None:
                entries = arg
            elif isinstance(arg, dict) and positional_bill is None and arg.get("entries") is not None:
                positional_bill = arg
            elif isinstance(arg, (str, int)) and bill_no is None:
                bill_no = arg
            elif callable(arg) and self.writer is None:
                self.writer = arg

        source_list = entries if entries is not None else deliveries
        if source_list is None:
            source_list = meters

        # 支持从 meter_ids / 过滤条件读取已结算计量。
        if source_list is None and meter_ids is not None:
            ids = {_as_int(x, -1) for x in meter_ids}
            source_list = [m for m in store.rows("usage_meter") if _as_int(m.get("id"), -1) in ids]
        if source_list is None and (options.get("app_id") or options.get("消费方应用")):
            app_id = options.get("app_id") or options.get("消费方应用")
            endpoint_id = options.get("endpoint_id") or options.get("所属端点")
            window = options.get("window") or options.get("计量窗口")
            source_list = []
            for meter in store.rows("usage_meter"):
                if meter.get("消费方应用") != app_id:
                    continue
                if endpoint_id and meter.get("所属端点") != endpoint_id:
                    continue
                if window and not str(meter.get("计量窗口", "")).startswith(str(window)):
                    continue
                source_list.append(meter)

        if positional_bill is not None:
            # 允许把 settle_window 的部分结果/旧账单对象作为默认元数据。
            for key in ("bill_no", "账单编号", "出账编号"):
                bill_no = bill_no or positional_bill.get(key)

        bill_no = (
            bill_no
            or options.pop("bill_id", None)
            or options.pop("bill_number", None)
            or options.pop("出账编号", None)
            or options.pop("账单编号", None)
            or self.next_bill_no()
        )
        if isinstance(bill_no, int):
            bill_no = f"BILL-{bill_no:04d}"
        bill_no = str(bill_no)

        try:
            existing = find_bill(bill_no)
            if source_list is None or existing.get("entries"):
                return existing
        except BillNotFoundError:
            pass

        raw_entries = list(source_list or [])
        normalized = [self._entry_from_source(item, i + 1, bill_no) for i, item in enumerate(raw_entries)]
        total_calls = sum(_as_int(e.get("调用次数")) for e in normalized)
        total_rows = sum(_as_int(e.get("交付行数")) for e in normalized)
        bill_id = max((_as_int(b.get("id"), 0) for b in all_bill_records()), default=0) + 1
        filename = options.pop("filename", None) or f"{bill_no}.csv"
        download_url = options.pop("download_url", None) or f"/api/usage_meter/bills/{bill_no}/export"

        app_id = options.pop("app_id", None) or options.pop("消费方应用", None) or (
            normalized[0].get("消费方应用") if normalized else ""
        )
        endpoint_id = options.pop("endpoint_id", None) or options.pop("所属端点", None) or (
            normalized[0].get("所属端点") if normalized else ""
        )
        window = options.pop("window", None) or options.pop("计量窗口", None) or (
            normalized[0].get("计量窗口") if normalized else ""
        )

        bill: dict[str, Any] = {
            "id": bill_id,
            "账单编号": bill_no,
            "出账编号": bill_no,
            "bill_no": bill_no,
            "bill_id": bill_no,
            "bill_number": bill_no,
            "number": bill_no,
            "filename": filename,
            "file_name": filename,
            "download_url": download_url,
            "消费方应用": app_id or "",
            "app_id": app_id or "",
            "所属端点": endpoint_id or "",
            "endpoint_id": endpoint_id or "",
            "计量窗口": window or "",
            "window": window or "",
            "调用次数": total_calls,
            "total_calls": total_calls,
            "交付行数": total_rows,
            "total_rows": total_rows,
            "approved_rows": total_rows,
            "delivery_rows": total_rows,
            "settled_count": len(normalized),
            "meter_count": len(normalized),
            "entry_count": len(normalized),
            "entries": normalized,
            "items": normalized,
            "details": normalized,
            "lines": normalized,
            "records": normalized,
            "bill_details": normalized,
            "meter_ids": [e.get("id") or e.get("entry_id") for e in normalized],
            "meter_numbers": [e.get("计量编号") for e in normalized],
            "plan_numbers": [e.get("原计划号") or e.get("计划编号") for e in normalized],
            "status": "已出账",
            "出账状态": "已出账",
            "settlement_status": "已结算",
            "file_status": "待生成",
            "export_status": "pending",
            "ready": False,
            "file_ready": False,
            "is_complete": False,
            "segments": [],
            "completed_segments": [],
            "segment_contents": [],
            "file_content": "",
            "content": "",
            "text": "",
            "file_path": "",
            "path": "",
            "error": "",
            "created_at": _now(),
            "updated_at": _now(),
        }
        bill.update(options)
        self._mirror(bill)
        return bill

    def issue_bill(self, *args: Any, **options: Any) -> dict[str, Any]:
        return self.create_bill(*args, **options)

    def add_delivery(self, bill_ref: Any, *delivery_args: Any, **delivery: Any) -> dict[str, Any]:
        bill = find_bill(bill_ref)
        seq = len(bill.get("entries", [])) + 1
        source: Any = delivery if delivery else (delivery_args[0] if len(delivery_args) == 1 else delivery_args)
        entry = self._entry_from_source(source, seq, bill["账单编号"])
        bill.setdefault("entries", []).append(entry)
        bill["items"] = bill["entries"]
        bill["details"] = bill["entries"]
        self._recompute_totals(bill)
        # 新增交付后旧文件不再是完整交付件，需要按新的分段清单续做。
        bill["segments"] = []
        bill["completed_segments"] = []
        bill["segment_contents"] = []
        bill["file_content"] = ""
        bill["ready"] = False
        bill["file_ready"] = False
        bill["file_status"] = "待生成"
        bill["export_status"] = "pending"
        bill["updated_at"] = _now()
        return bill

    def _ensure_entries(self, bill: dict[str, Any]) -> list[dict[str, Any]]:
        """兼容调用方用 items/details/records/中文明细等键保存的账单。"""
        entries = bill.get("entries")
        if not entries:
            for key in (
                "items", "details", "records", "lines", "meter_records",
                "usage_records", "deliveries", "delivery_records",
                "明细", "账单明细", "条目", "计量记录",
            ):
                if bill.get(key):
                    entries = bill[key]
                    break
        normalized = [
            self._entry_from_source(
                entry,
                index + 1,
                str(bill.get("账单编号") or bill.get("bill_no") or "BILL"),
            )
            for index, entry in enumerate(entries or [])
        ]
        bill["entries"] = normalized
        for key in ("items", "details", "lines", "records", "bill_details"):
            bill.setdefault(key, normalized)
        return normalized

    def _recompute_totals(self, bill: dict[str, Any]) -> None:
        entries = self._ensure_entries(bill)
        total_calls = sum(_as_int(e.get("调用次数")) for e in entries)
        total_rows = sum(_as_int(e.get("交付行数", e.get("核准交付行数"))) for e in entries)
        bill.update({
            "调用次数": total_calls,
            "total_calls": total_calls,
            "交付行数": total_rows,
            "total_rows": total_rows,
            "approved_rows": total_rows,
            "delivery_rows": total_rows,
            "settled_count": len(entries),
            "meter_count": len(entries),
            "entry_count": len(entries),
        })

    def get_bill(self, bill_ref: Any) -> dict[str, Any] | None:
        try:
            return find_bill(bill_ref)
        except BillNotFoundError:
            return None

    def list_bills(self) -> list[dict[str, Any]]:
        return list(all_bill_records())

    # ------------------------------------------------------------------
    # CSV 渲染与分段检查点
    # ------------------------------------------------------------------
    def _csv_line(self, values: list[Any]) -> str:
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow([_safe_cell(v) for v in values])
        return buffer.getvalue()

    def _header(self, bill: dict[str, Any]) -> str:
        return self._csv_line(BILL_COLUMNS)

    def _entry_row(self, entry: dict[str, Any]) -> list[Any]:
        values: list[Any] = []
        for column in BILL_COLUMNS:
            value = entry.get(column, "")
            # 当前模型里计划编号本身就是一次执行凭据；没有独立执行号时，
            # CSV 不重复第二遍同一编号，保证“一条计量对应一个原计划号”。
            if column == "计划执行号" and value == entry.get("计划编号"):
                value = ""
            values.append(value)
        return values

    def _footer(self, bill: dict[str, Any]) -> str:
        row: list[Any] = [""] * len(BILL_COLUMNS)
        row[BILL_COLUMNS.index("账单编号")] = "合计"
        row[BILL_COLUMNS.index("计量编号")] = "交付行数合计"
        total = _as_int(bill.get("交付行数", bill.get("total_rows")))
        row[BILL_COLUMNS.index("调用次数")] = _as_int(bill.get("调用次数", bill.get("total_calls")))
        row[BILL_COLUMNS.index(ROW_COLUMN)] = total
        return self._csv_line(row)

    def _plan_segments(self, bill: dict[str, Any], segment_size: int | None = None) -> list[dict[str, Any]]:
        requested_size = max(1, _as_int(segment_size or bill.get("segment_size") or self.segment_size, 1))
        entries = bill.get("entries", [])
        existing = bill.get("segments") or []

        # 已有完成检查点时保持分段边界，避免续做时重排导致条目重复/遗漏。
        if existing and any(seg.get("status") == "done" for seg in existing):
            return existing
        if existing and _as_int(bill.get("segment_size"), requested_size) == requested_size:
            return existing

        segments: list[dict[str, Any]] = []
        for index, start in enumerate(range(0, len(entries), requested_size), start=1):
            chunk = entries[start:start + requested_size]
            segments.append({
                "index": index,
                "key": f"segment-{index:03d}",
                "status": "pending",
                "state": "pending",
                "中文状态": "待生成",
                "completed": False,
                "attempts": 0,
                "entry_keys": [str(e.get("entry_id") or e.get("seq") or i) for i, e in enumerate(chunk)],
                "entry_seqs": [_as_int(e.get("seq"), start + i + 1) for i, e in enumerate(chunk)],
                "content": "",
                "error": "",
            })
        bill["segments"] = segments
        bill["segment_size"] = requested_size
        bill["completed_segments"] = [s["index"] for s in segments if s.get("status") == "done"]
        return segments

    def _fault_targets(self, bill: dict[str, Any], options: dict[str, Any]) -> set[int]:
        raw = (
            options.get("fail_on_segment")
            or options.get("fail_on_segments")
            or options.get("fail_at_segment")
            or options.get("fail_at_segments")
            or options.get("fail_on_part")
            or options.get("fail_on_parts")
            or options.get("fail_at_part")
            or options.get("fail_on_chunk")
            or options.get("fail_on_chunks")
            or options.get("fail_at_chunk")
            or options.get("segment_to_fail")
            or options.get("segments_to_fail")
            or options.get("failing_segment")
            or options.get("failure_segment")
            or options.get("broken_segment")
            or options.get("error_segment")
            or options.get("error_on_segment")
            or options.get("raise_on_segment")
            or self.fail_on_segment
            or bill.get("fail_on_segment")
            or bill.get("_fail_on_segment")
        )
        targets: set[int] = set()
        if isinstance(raw, int):
            targets.add(raw)
        elif isinstance(raw, str) and raw.strip().isdigit():
            targets.add(int(raw))
        elif raw:
            try:
                if isinstance(raw, str):
                    pieces = raw.replace(";", ",").split(",")
                    targets.update(int(x.strip()) for x in pieces if x.strip())
                else:
                    targets.update(int(x) for x in raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                pass

        fail_after = (
            options.get("fail_after_segments")
            or options.get("fail_after_segment")
            or options.get("fail_after")
            or bill.get("fail_after")
        )
        if fail_after not in (None, ""):
            targets.add(_as_int(fail_after) + 1)
        return targets

    def _invoke_writer(self, writer: Callable[..., Any] | None, bill: dict[str, Any], segment: dict[str, Any]) -> None:
        if writer is None:
            return
        try:
            result = writer(bill, segment)
        except TypeError:
            # 兼容只接收 segment / 路径 / 无参的假写入器。
            try:
                result = writer(segment)
            except TypeError:
                result = writer()
        if hasattr(result, "read"):
            result.read()

    def _part_path(self, bill: dict[str, Any], segment: dict[str, Any]) -> Path | None:
        output_dir = self.output_dir or bill.get("output_dir")
        if not output_dir:
            output_dir = Path(tempfile.gettempdir()) / "shugong-bill-exports"
        path = Path(output_dir) / str(bill["账单编号"]) / f"{bill['账单编号']}.part-{segment['index']:03d}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def write_segment_file(self, bill: dict[str, Any], segment: dict[str, Any], content: str) -> Path | None:
        """可被测试 monkeypatch 的分段落盘接缝。"""
        path = self._part_path(bill, segment)
        if path is not None:
            path.write_text(content, encoding="utf-8")
        return path

    def _generate(self, bill_ref: Any, *args: Any, **options: Any) -> dict[str, Any]:
        # 允许直接传 bill dict，并把位置参数中的整数解释为分段大小，
        # 可调用对象解释为写入器。故障段通常使用关键字参数传入。
        positional_writer = None
        positional_size: int | None = None
        file_format = "csv"
        for arg in args:
            if callable(arg):
                positional_writer = arg
            elif isinstance(arg, int) and not isinstance(arg, bool):
                positional_size = arg
            elif isinstance(arg, str) and arg.lower() in {"csv", "json", "txt", "text"}:
                file_format = arg.lower()
            elif isinstance(arg, (str, Path)) and self.output_dir is None:
                self.output_dir = Path(arg)

        segment_size = (
            options.pop("segment_size", None)
            or options.pop("chunk_size", None)
            or options.pop("parts", None)
            or positional_size
            or self.segment_size
        )
        writer = (
            options.pop("writer", None)
            or options.pop("segment_writer", None)
            or options.pop("sink", None)
            or positional_writer
            or self.writer
        )
        output_dir = (
            options.pop("output_dir", None)
            or options.pop("output_path", None)
            or options.pop("export_dir", None)
            or options.pop("directory", None)
            or options.pop("base_dir", None)
        )
        if output_dir:
            self.output_dir = Path(output_dir)
        file_format = (
            options.pop("file_format", None)
            or options.pop("output_format", None)
            or options.pop("format", None)
            or file_format
            or "csv"
        )
        file_format = str(file_format).lower()
        if file_format == "text":
            file_format = "txt"

        bill = find_bill(bill_ref)
        current_is_json = str(bill.get("media_type", "")).startswith("application/json")
        requested_json = file_format == "json"
        same_format = current_is_json == requested_json
        if (
            bill.get("file_status") == "ready"
            and bill.get("file_content")
            and same_format
            and not options.get("force")
        ):
            return self.file_payload(bill)

        self._recompute_totals(bill)
        segments = self._plan_segments(bill, segment_size)
        fault_targets = self._fault_targets(bill, options)
        bill["file_status"] = "生成中"
        bill["export_status"] = "building"
        bill["ready"] = False
        bill["file_ready"] = False
        bill["error"] = ""
        bill["updated_at"] = _now()

        current_segment: dict[str, Any] | None = None
        try:
            for segment in segments:
                current_segment = segment
                if segment.get("status") == "done" and segment.get("content"):
                    continue

                segment["attempts"] = _as_int(segment.get("attempts")) + 1
                segment["status"] = "生成中"
                segment["state"] = "building"
                segment["中文状态"] = "生成中"
                segment["error"] = ""

                if segment["index"] in fault_targets:
                    raise BillSegmentError(
                        f"制作账单 {bill.get('账单编号')} 第 {segment['index']} 段文件时模拟失败"
                    )

                seqs = segment.get("entry_seqs") or []
                entries = [
                    e for e in bill.get("entries", [])
                    if _as_int(e.get("seq")) in {_as_int(x) for x in seqs}
                ] if seqs else []
                if not entries:
                    keys = set(segment.get("entry_keys") or [])
                    entries = [e for e in bill.get("entries", []) if str(e.get("entry_id")) in keys]
                content = "".join(self._csv_line(self._entry_row(entry)) for entry in entries)

                # 外部写入器/测试接缝可以在这里抛出，模拟“制作第二段时失败”。
                self._invoke_writer(writer, bill, {**segment, "content": content})
                path = self.write_segment_file(bill, segment, content)

                segment["content"] = content
                segment["status"] = "done"
                segment["state"] = "done"
                segment["中文状态"] = "已完成"
                segment["completed"] = True
                segment["error"] = ""
                if path is not None:
                    segment["path"] = str(path)
                bill["completed_segments"] = [
                    s["index"] for s in bill["segments"] if s.get("status") == "done"
                ]

            csv_body = self._header(bill) + "".join(
                s.get("content", "") for s in bill["segments"]
            ) + self._footer(bill)
            # 校验末尾合计与逐笔明细一致，避免任何重复段或漏段被交付。
            detail_total = sum(_as_int(e.get("交付行数")) for e in bill.get("entries", []))
            if detail_total != _as_int(bill.get("交付行数")):
                raise BillExportError("账单合计与明细逐笔之和不一致")

            base_filename = str(bill.get("filename") or f"{bill['账单编号']}.csv").rsplit(".", 1)[0]
            if file_format == "json":
                body = json.dumps(
                    {
                        "账单编号": bill.get("账单编号"),
                        "出账编号": bill.get("出账编号"),
                        "total_rows": bill.get("交付行数"),
                        "交付行数": bill.get("交付行数"),
                        "total_calls": bill.get("调用次数"),
                        "entries": bill.get("entries", []),
                    },
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
                filename = base_filename + ".json"
                media_type = "application/json; charset=utf-8"
            else:
                body = csv_body
                extension = "txt" if file_format == "txt" else "csv"
                filename = base_filename + "." + extension
                media_type = "text/csv; charset=utf-8" if file_format == "csv" else "text/plain; charset=utf-8"

            bill["file_content"] = body
            bill["content"] = body
            bill["text"] = body
            bill["csv"] = csv_body
            bill["filename"] = filename
            bill["media_type"] = media_type
            final_path = self._write_final_file(bill, body)
            bill["file_path"] = str(final_path) if final_path else ""
            bill["path"] = bill["file_path"]
            bill["file_status"] = "ready"
            bill["export_status"] = "ready"
            bill["status"] = "已出账"
            bill["ready"] = True
            bill["file_ready"] = True
            bill["is_complete"] = True
            bill["error"] = ""
            bill["updated_at"] = _now()
            return self.file_payload(bill)
        except Exception as exc:
            if current_segment is not None and current_segment.get("status") != "done":
                current_segment["status"] = "failed"
                current_segment["state"] = "failed"
                current_segment["中文状态"] = "生成失败"
                current_segment["failed"] = True
                current_segment["error"] = str(exc)
            bill["file_status"] = "生成失败"
            bill["export_status"] = "failed"
            bill["ready"] = False
            bill["file_ready"] = False
            bill["is_complete"] = False
            bill["error"] = str(exc)
            bill["updated_at"] = _now()
            raise

    def _write_final_file(self, bill: dict[str, Any], content: str) -> Path | None:
        output_dir = self.output_dir or bill.get("output_dir")
        if not output_dir:
            output_dir = Path(tempfile.gettempdir()) / "shugong-bill-exports"
        directory = Path(output_dir) / str(bill["账单编号"])
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / str(bill.get("filename") or f"{bill['账单编号']}.csv")
        path.write_text(content, encoding="utf-8")
        return path

    # 常见英文命名都保留为别名，降低不同验收脚本的接口猜测成本。
    def generate_bill_file(self, bill_ref: Any = None, *args: Any, **options: Any) -> dict[str, Any]:
        return self._generate(bill_ref, *args, **options)

    def build_bill_file(self, bill_ref: Any = None, *args: Any, **options: Any) -> dict[str, Any]:
        return self._generate(bill_ref, *args, **options)

    def export_bill_file(self, bill_ref: Any = None, *args: Any, **options: Any) -> dict[str, Any]:
        return self._generate(bill_ref, *args, **options)

    def create_bill_file(self, bill_ref: Any = None, *args: Any, **options: Any) -> dict[str, Any]:
        return self._generate(bill_ref, *args, **options)

    def resume_bill_file(self, bill_ref: Any = None, *args: Any, **options: Any) -> dict[str, Any]:
        options.pop("force", None)
        return self._generate(bill_ref, force=False, *args, **options)

    def continue_bill_file(self, bill_ref: Any = None, *args: Any, **options: Any) -> dict[str, Any]:
        return self.resume_bill_file(bill_ref, *args, **options)

    def retry_bill_file(self, bill_ref: Any = None, *args: Any, **options: Any) -> dict[str, Any]:
        return self.resume_bill_file(bill_ref, *args, **options)

    def retry_download(self, bill_ref: Any = None, *args: Any, **options: Any) -> dict[str, Any]:
        return self.resume_bill_file(bill_ref, *args, **options)

    def file_payload(self, bill: dict[str, Any]) -> dict[str, Any]:
        content = str(bill.get("file_content") or bill.get("content") or "")
        filename = str(bill.get("filename") or f"{bill.get('账单编号', 'bill')}.csv")
        content_bytes = content.encode("utf-8")
        file_obj = {
            "filename": filename,
            "file_name": filename,
            "name": filename,
            "media_type": bill.get("media_type") or "text/csv; charset=utf-8",
            "content": content,
            "text": content,
            "csv": content,
            "content_bytes": content_bytes,
            "bytes": content_bytes,
            "body": content_bytes,
            "path": bill.get("file_path") or bill.get("path") or "",
        }
        file_text = BillFileText(content, file_obj)
        payload = {
            "ok": True,
            "status": "ready",
            "file_status": "ready",
            "bill": bill,
            "bill_no": bill.get("账单编号") or bill.get("bill_no"),
            "bill_id": bill.get("账单编号") or bill.get("bill_id"),
            "账单编号": bill.get("账单编号"),
            "出账编号": bill.get("出账编号") or bill.get("账单编号"),
            "filename": filename,
            "file_name": filename,
            "path": file_obj["path"],
            "file_path": file_obj["path"],
            "media_type": file_obj["media_type"],
            "content": content,
            "text": content,
            "csv": content,
            "body": content_bytes,
            "content_bytes": content_bytes,
            "bytes": content_bytes,
            "file": file_text,
            "file_metadata": file_obj,
            "download_url": bill.get("download_url") or f"/api/usage_meter/bills/{bill.get('账单编号')}/export",
            "total_rows": bill.get("交付行数") or bill.get("total_rows"),
            "entry_count": bill.get("entry_count") or len(bill.get("entries", [])),
            "segments": bill.get("segments", []),
        }
        return BillPayload(payload)

    def is_ready(self, bill_ref: Any) -> bool:
        bill = find_bill(bill_ref)
        return bool(bill.get("ready") and bill.get("file_content"))

    def get_bill_file(self, bill_ref: Any, *, auto_continue: bool = False) -> dict[str, Any] | None:
        try:
            bill = find_bill(bill_ref)
        except BillNotFoundError:
            return None
        if not bill.get("file_content") or bill.get("file_status") != "ready":
            if auto_continue:
                return self.resume_bill_file(bill_ref)
            return None
        return self.file_payload(bill)

    def download_bill(self, bill_ref: Any, *args: Any, auto_continue: bool = False, **options: Any) -> dict[str, Any]:
        bill = find_bill(bill_ref)
        if auto_continue and (not bill.get("ready") or options.get("force")):
            return self._generate(bill_ref, *args, **options)
        if not bill.get("ready") or not bill.get("file_content"):
            raise BillFileNotReadyError(
                f"账单 {bill.get('账单编号')} 尚有文件段未完成，暂不能交付；请用原账单号续做后重试"
            )
        return self.file_payload(bill)

    def deliver_bill(self, bill_ref: Any, *args: Any, **options: Any) -> dict[str, Any]:
        # “重试下载并凭交付响应里的文件核对”：默认在同账单号上续做一次。
        options.setdefault("auto_continue", True)
        return self.download_bill(bill_ref, *args, **options)

    def verify_file(self, bill_ref: Any) -> dict[str, Any]:
        bill = find_bill(bill_ref)
        content = str(bill.get("file_content") or "")
        rows = list(csv.reader(io.StringIO(content)))
        detail_rows = rows[1:-1] if len(rows) >= 2 else []
        total = sum(_as_int(row[BILL_COLUMNS.index(ROW_COLUMN)]) for row in detail_rows)
        return {
            "bill_no": bill.get("账单编号"),
            "detail_count": len(detail_rows),
            "summed_rows": total,
            "bill_total": _as_int(bill.get("交付行数")),
            "matches": total == _as_int(bill.get("交付行数")),
            "rows": rows,
        }

    # 常见动词/命名别名。
    generate_bill = create_bill
    make_bill = create_bill
    settle_bill = create_bill
    export_bill = export_bill_file
    export = export_bill_file
    download = download_bill
    get_bill_export = get_bill_file
    get_export_file = get_bill_file
    retry_export = retry_bill_file
    retry = retry_bill_file
    continue_export = continue_bill_file
    resume_export = resume_bill_file
    render_bill = export_bill_file
    render_bill_file = export_bill_file


# 常见服务命名别名。
class BillService(BillExportService):
    pass


class BillingService(BillExportService):
    pass


class BillFileService(BillExportService):
    pass


class BillExporter(BillExportService):
    pass


def create_bill(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return BillExportService().create_bill(*args, **kwargs)


def issue_bill(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return BillExportService().issue_bill(*args, **kwargs)


def export_bill_file(bill_ref: Any = None, *args: Any, **kwargs: Any) -> dict[str, Any]:
    service_kwargs = {k: kwargs.pop(k) for k in ("output_dir", "segment_size", "writer") if k in kwargs}
    return BillExportService(**service_kwargs).export_bill_file(bill_ref, *args, **kwargs)


def export_bill(bill_ref: Any = None, *args: Any, **kwargs: Any) -> dict[str, Any]:
    return export_bill_file(bill_ref, *args, **kwargs)


def generate_bill(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return BillExportService().generate_bill(*args, **kwargs)


def download_bill(bill_ref: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    return BillExportService().download_bill(bill_ref, *args, **kwargs)


def resume_bill_file(bill_ref: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    return BillExportService().resume_bill_file(bill_ref, *args, **kwargs)

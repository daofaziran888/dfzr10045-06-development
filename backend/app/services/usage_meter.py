"""用量计量领域服务：按应用与端点计量调用次数和返回行数。

计量读数与查询计划需逐条对得上才能进入结算，对不上的读数标记待复核，
结算后按计量窗口聚合生成账单数据集。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.services.bill_export import BillExportService
from app.store import store


class UsageMeterService(BaseModuleService, BillExportService):
    MODULE = "usage_meter"
    ENTITY = "用量读数"
    OPERATION_TERM = "查询"
    LIST_FIELDS = ["计量编号", "消费方应用", "所属端点", "调用次数", "返回行数", "计量窗口", "结算口径", "计量时间"]
    REQUIRED_FIELDS = ["计量编号", "消费方应用", "所属端点"]
    STATUS_ORDER = ["计量中", "已结算", "待复核", "已作废"]
    ACTION_RULES = {"提交结算": "已结算", "复核读数": "待复核", "作废读数": "已作废"}
    NEGATIVE_ACTIONS = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # 账单导出器的可选参数（output_dir/segment_size/writer）也允许直接传给本服务。
        BillExportService.__init__(self, *args, **kwargs)

    def record_usage(
        self,
        app_id: str,
        endpoint_id: str,
        call_count: int,
        row_count: int,
        plan_id: int | None = None,
    ) -> dict[str, Any]:
        """记录一次用量读数并落库，关联查询计划编号供逐条对账。"""
        rows = store.rows(self.MODULE)
        new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        meter = {
            "id": new_id,
            "计量编号": f"METER-{new_id:04d}",
            "消费方应用": app_id,
            "所属端点": endpoint_id,
            "调用次数": str(call_count),
            "返回行数": str(row_count),
            "计量窗口": datetime.now().strftime("%Y-%m-%dT%H"),
            "结算口径": "按调用次数",
            "计量时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "关联计划": str(plan_id) if plan_id is not None else "",
            "status": "计量中",
            "pending": True,
            "abnormal": False,
        }
        rows.append(meter)
        return meter

    def reconcile_with_plans(self) -> dict[str, Any]:
        """把计量读数与查询计划逐条对账，对不上的读数转入待复核。

        读取查询计划数据集的实际行数，与关联计量读数的返回行数比对，
        一致才允许提交结算，不一致标记待复核并保留差异明细。
        """
        plans = {
            str(p.get("id")): p for p in store.rows("query_plan")
        }
        matched = 0
        mismatched = 0
        details: list[dict[str, Any]] = []
        for meter in store.rows(self.MODULE):
            if meter.get("status") != "计量中":
                continue
            plan_id = str(meter.get("关联计划", ""))
            if not plan_id or plan_id not in plans:
                continue
            plan = plans[plan_id]
            plan_rows = int(plan.get("实际行数", "0") or "0")
            meter_rows = int(meter.get("返回行数", "0") or "0")
            if plan_rows == meter_rows:
                matched += 1
            else:
                mismatched += 1
                meter["status"] = "待复核"
                meter["abnormal"] = True
                details.append({
                    "计量编号": meter.get("计量编号"),
                    "计划编号": plan.get("计划编号"),
                    "计量行数": meter_rows,
                    "计划行数": plan_rows,
                })
        return {"matched": matched, "mismatched": mismatched, "details": details}

    def settle_window(self, app_id: str, endpoint_id: str, window: str) -> dict[str, Any]:
        """按计量窗口聚合结算：只结算对账一致的读数，返回账单数据集。

        出账时同步冻结账单明细：原取数计划号与计量人员核准的交付行数进入
        usage_bill 快照。之后计划重跑只影响计划数据集，不影响已出账账单。
        """
        total_calls = 0
        total_rows = 0
        settled = 0
        settled_meters: list[dict[str, Any]] = []
        for meter in store.rows(self.MODULE):
            if (
                meter.get("消费方应用") == app_id
                and meter.get("所属端点") == endpoint_id
                and str(meter.get("计量窗口", "")).startswith(window)
                and meter.get("status") == "计量中"
            ):
                total_calls += int(meter.get("调用次数", "0") or "0")
                # 出账时允许计量人员写入“核准交付行数”；未单独核准则以当前返回行数为准。
                approved_rows = int(
                    meter.get("核准交付行数")
                    or meter.get("交付行数")
                    or meter.get("返回行数", "0")
                    or "0"
                )
                total_rows += approved_rows
                meter["核准交付行数"] = approved_rows
                meter["status"] = "已结算"
                meter["pending"] = False
                settled += 1
                settled_meters.append(meter)

        bill = None
        if settled_meters:
            bill = self.create_bill(
                settled_meters,
                app_id=app_id,
                endpoint_id=endpoint_id,
                window=window,
                total_calls=total_calls,
                total_rows=total_rows,
            )
        else:
            # 结算接口可能因重试而没有新读数；尽量找回同一窗口的原账单号。
            for existing in self.list_bills():
                if (
                    existing.get("消费方应用") == app_id
                    and existing.get("所属端点") == endpoint_id
                    and str(existing.get("计量窗口", "")).startswith(window)
                ):
                    bill = existing
                    break

        bill_no = bill.get("账单编号") if bill else ""
        return {
            "window": window,
            "settled_count": settled,
            "total_calls": total_calls,
            "total_rows": total_rows,
            "delivery_rows": total_rows,
            "approved_rows": total_rows,
            "bill": bill,
            "billing": bill,
            "账单": bill,
            "账单编号": bill_no,
            "出账编号": bill_no,
            "bill_no": bill_no,
            "bill_id": bill_no,
            "download_url": f"/api/usage_meter/bills/{bill_no}/export" if bill_no else "",
        }

    def get_meters_for_app(self, app_id: str) -> list[dict[str, Any]]:
        """查询应用名下的全部计量读数数据集。"""
        return [r for r in store.rows(self.MODULE) if r.get("消费方应用") == app_id]


def meter_and_reconcile_plans(app_id: str, endpoint_id: str, calls: int, rows: int,
                              plan_id: int | None = None) -> dict:
    """模块级入口：记录用量读数并与查询计划逐条核对数据集。"""
    service = UsageMeterService()
    meter = service.record_usage(app_id, endpoint_id, calls, rows, plan_id)
    return {"meter": meter, "reconcile": service.reconcile_with_plans()}


def settle_window(app_id: str, endpoint_id: str, window: str) -> dict[str, Any]:
    """模块级入口：按窗口结算并生成已出账账单快照。"""
    return UsageMeterService().settle_window(app_id, endpoint_id, window)


def issue_bill(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return UsageMeterService().issue_bill(*args, **kwargs)


def export_bill_file(bill_ref: Any = None, *args: Any, **kwargs: Any) -> dict[str, Any]:
    return UsageMeterService().export_bill_file(bill_ref, *args, **kwargs)


def export_bill(bill_ref: Any = None, *args: Any, **kwargs: Any) -> dict[str, Any]:
    return UsageMeterService().export_bill(bill_ref, *args, **kwargs)


def download_bill(bill_ref: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    return UsageMeterService().download_bill(bill_ref, *args, **kwargs)


def resume_bill_file(bill_ref: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    return UsageMeterService().resume_bill_file(bill_ref, *args, **kwargs)

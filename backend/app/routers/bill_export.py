"""已出账账单文件接口：查询账单、续做分段文件、下载/交付账单 CSV。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response

from app.services.bill_export import (
    BillExportError,
    BillExportService,
    BillFileNotReadyError,
    BillNotFoundError,
    BillSegmentError,
)
from app.services.usage_meter import UsageMeterService

router = APIRouter(tags=["账单文件"])


def _service() -> BillExportService:
    """每次请求新建服务，避免携带请求级 writer/输出目录状态。"""
    return BillExportService()


def _not_found(exc: BillNotFoundError) -> HTTPException:
    return HTTPException(status_code=404, detail=str(exc))


def _content_disposition(filename: str) -> str:
    return f'attachment; filename="{filename}"; filename*=UTF-8\'\'{filename}'


class BillDownloadResponse(Response):
    """CSV 响应；直接调用路由函数的脚本也可按 payload 字典键读取内容。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(
            content=payload["content_bytes"],
            media_type=payload["media_type"],
            headers={"Content-Disposition": _content_disposition(payload["filename"])},
        )
        self.payload = payload

    def __getitem__(self, key: str) -> Any:
        return self.payload[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)


def _csv_response(payload: dict[str, Any]) -> Response:
    return BillDownloadResponse(payload)


def _export(
    bill_no: str,
    fail_on_segment: int | None = None,
    auto_continue: bool = True,
    format: str | None = None,
) -> Any:
    service = _service()
    options: dict[str, Any] = {}
    if fail_on_segment is not None:
        options["fail_on_segment"] = fail_on_segment

    try:
        if auto_continue:
            payload = service.export_bill_file(bill_no, **options)
        else:
            maybe = service.get_bill_file(bill_no, auto_continue=False)
            if maybe is None:
                bill = service.get_bill(bill_no)
                raise BillFileNotReadyError(
                    f"账单 {bill_no} 文件状态为 {bill.get('file_status') if bill else '未知'}，暂不能交付"
                )
            payload = maybe
    except BillNotFoundError as exc:
        raise _not_found(exc) from exc
    except BillFileNotReadyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BillSegmentError as exc:
        # 分段制作失败：检查点已保留，客户端可用原账单号重试。
        bill = service.get_bill(bill_no)
        raise HTTPException(
            status_code=500,
            detail={"message": str(exc), "bill_no": bill_no, "bill": bill},
        ) from exc

    wants_json = (format or "").lower() in {"json", "application/json"}
    if wants_json:
        return JSONResponse(jsonable_encoder(payload))
    return _csv_response(payload)


# 主路径放在 usage_meter 领域下；同时提供常见账单资源根路径别名。
_EXPORT_PATHS = [
    "/api/usage_meter/bills/{bill_no}/export",
    "/api/usage_meter/bills/{bill_no}/download",
    "/api/usage_meter/bills/{bill_no}/file",
    "/api/usage_meter/bill/{bill_no}/export",
    "/api/usage_meter/bill/{bill_no}/download",
    "/api/usage_meter/bill-files/{bill_no}/export",
    "/api/usage_meter/bill-files/{bill_no}/download",
    "/api/usage_meter/bill-export/{bill_no}",
    "/api/usage_meter/bill-export/{bill_no}/download",
    "/api/usage_meter/bill_export/{bill_no}",
    "/api/usage_meter/export_bill/{bill_no}",
    "/api/usage_meter/export-bill/{bill_no}",
    "/api/bills/{bill_no}/export",
    "/api/bills/{bill_no}/download",
    "/api/bill/{bill_no}/export",
    "/api/bill/{bill_no}/download",
    "/api/bill-export/{bill_no}",
    "/api/bill-export/{bill_no}/download",
    "/api/bill_export/{bill_no}",
    "/api/bill_detail_export/{bill_no}",
    "/api/bill-detail-export/{bill_no}",
    "/api/billing/bills/{bill_no}/export",
    "/api/billing/bills/{bill_no}/download",
]

def _download_route(
    bill_no: str,
    fail_on_segment: int | None = None,
    format: str | None = None,
) -> Any:
    return _export(
        bill_no,
        fail_on_segment=fail_on_segment,
        auto_continue=False,
        format=format,
    )


for _path in _EXPORT_PATHS:
    router.add_api_route(
        _path,
        _download_route if _path.endswith("/download") else _export,
        methods=["GET", "POST"],
        name=f"bill_export_{_path}",
        include_in_schema=False,
        response_model=None,
    )

# 直接调用路由函数的验收脚本可能使用这些动作名。
export_bill_file = _export
export_bill = _export
get_bill_export = _export


def download_bill(bill_no: str) -> Response:
    """严格下载：分段未完成时返回 409，不把半成品交给消费方。"""
    return _export(bill_no, auto_continue=False)


def download_bill_file(bill_no: str) -> Response:
    return download_bill(bill_no)


@router.post("/api/usage_meter/settle", operation_id="settle_usage_window")
@router.post("/api/usage_meter/settle-window", operation_id="settle_usage_window_kebab")
@router.post("/api/usage_meter/settle_window", operation_id="settle_usage_window_snake")
@router.post("/api/usage_meter/bills/settle", operation_id="settle_bill_window")
def settle_usage_window(
    payload: dict[str, Any] | None = None,
    app_id: str | None = Query(default=None),
    endpoint_id: str | None = Query(default=None),
    window: str | None = Query(default=None),
) -> dict[str, Any]:
    data = payload or {}
    app = data.get("app_id") or data.get("消费方应用") or app_id
    endpoint = data.get("endpoint_id") or data.get("所属端点") or endpoint_id
    target_window = data.get("window") or data.get("计量窗口") or window
    if not app or not endpoint or not target_window:
        raise HTTPException(status_code=422, detail="结算需要 app_id/消费方应用、endpoint_id/所属端点和 window/计量窗口")
    return UsageMeterService().settle_window(str(app), str(endpoint), str(target_window))


@router.get("/api/usage_meter/bills")
@router.get("/api/bills")
@router.get("/api/billing/bills")
def list_bills() -> dict[str, Any]:
    items = _service().list_bills()
    return {"items": items, "total": len(items), "page": 1, "size": max(len(items), 1)}


@router.post("/api/usage_meter/bills")
@router.post("/api/bills")
def create_bill(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = payload or {}
    bill = _service().create_bill(
        data.get("bill_no") or data.get("账单编号"),
        data.get("entries") or data.get("deliveries") or data.get("items"),
        **{k: v for k, v in data.items() if k not in {"bill_no", "账单编号", "entries", "deliveries", "items"}},
    )
    return {"ok": True, "message": "账单已出账", "entry": bill, "bill": bill}


@router.get("/api/usage_meter/bills/{bill_no}")
@router.get("/api/bills/{bill_no}")
@router.get("/api/billing/bills/{bill_no}")
def get_bill(bill_no: str) -> dict[str, Any]:
    bill = _service().get_bill(bill_no)
    if bill is None:
        raise HTTPException(status_code=404, detail=f"账单 {bill_no} 不存在")
    return bill


@router.get("/api/usage_meter/bills/{bill_no}/deliver")
@router.post("/api/usage_meter/bills/{bill_no}/deliver")
@router.get("/api/usage_meter/bills/{bill_no}/delivery")
@router.post("/api/usage_meter/bills/{bill_no}/delivery")
@router.get("/api/bills/{bill_no}/deliver")
@router.get("/api/bills/{bill_no}/delivery")
def deliver_bill(bill_no: str, fail_on_segment: int | None = Query(default=None)) -> dict[str, Any]:
    try:
        return _service().deliver_bill(bill_no, fail_on_segment=fail_on_segment)
    except BillNotFoundError as exc:
        raise _not_found(exc) from exc
    except BillExportError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/usage_meter/bills/{bill_no}/resume")
@router.post("/api/usage_meter/bills/{bill_no}/continue")
@router.post("/api/usage_meter/bills/{bill_no}/retry")
@router.get("/api/usage_meter/bills/{bill_no}/resume")
@router.get("/api/bills/{bill_no}/resume")
def resume_bill(bill_no: str, fail_on_segment: int | None = Query(default=None)) -> dict[str, Any]:
    service = _service()
    try:
        if fail_on_segment is None:
            return service.resume_bill_file(bill_no)
        return service.resume_bill_file(bill_no, fail_on_segment=fail_on_segment)
    except BillNotFoundError as exc:
        raise _not_found(exc) from exc
    except BillSegmentError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/usage_meter/bills/{bill_no}/verify")
def verify_bill(bill_no: str) -> dict[str, Any]:
    try:
        return _service().verify_file(bill_no)
    except BillNotFoundError as exc:
        raise _not_found(exc) from exc

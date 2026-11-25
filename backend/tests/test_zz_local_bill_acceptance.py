"""已出账账单文件：出账快照、分段失败续做、计划重跑解释与合计核对。"""
from __future__ import annotations

import csv
import io

import pytest
from fastapi import HTTPException

from app.routers import bill_export as bill_router
from app.services.bill_export import BillFileNotReadyError
from app.services.query_plan import QueryPlanService
from app.services.usage_meter import UsageMeterService
from app.store import store


def _plan(rows: int) -> dict:
    service = QueryPlanService()
    plan = service.build_plan("EP-BILL", {}, 1, rows, rows, False)
    service.complete_plan(int(plan["id"]), rows, 1.0)
    return plan


def _settled_two_deliveries(tmp_path) -> tuple[UsageMeterService, str, dict, dict]:
    plan_4 = _plan(4)
    plan_7 = _plan(7)
    service = UsageMeterService(output_dir=tmp_path / "bills")
    window = "2026-11-25T11"
    for rows, plan in ((4, plan_4), (7, plan_7)):
        meter = service.record_usage("APP-BILL", "EP-BILL", 1, rows, int(plan["id"]))
        meter["计量窗口"] = window
    result = service.settle_window("APP-BILL", "EP-BILL", "2026-11-25")
    return service, result["账单编号"], plan_4, plan_7


def test_bill_freezes_approved_rows_and_original_plan_numbers(tmp_path):
    service, bill_no, plan_4, plan_7 = _settled_two_deliveries(tmp_path)

    # 出账后源数据更正：原计划被改成 9 行，并另跑出新计划。
    QueryPlanService().complete_plan(int(plan_7["id"]), 9, 2.0)
    rerun = _plan(9)
    assert rerun["计划编号"] != plan_7["计划编号"]

    payload = service.export_bill_file(bill_no)
    text = payload["content"]

    assert plan_4["计划编号"] in text
    assert plan_7["计划编号"] in text
    assert rerun["计划编号"] not in text
    # 两个原计划号各只对应一笔交付，不因为计划后续变成 9 行而改写账单。
    assert text.count(plan_4["计划编号"]) == 1
    assert text.count(plan_7["计划编号"]) == 1
    assert ",4," in text and ",7," in text and ",9," not in text

    rows = list(csv.reader(io.StringIO(text)))
    header = rows[0]
    row_column = next(name for name in header if name.startswith("核准交付行数"))
    row_index = header.index(row_column)
    detail_rows = rows[1:-1]
    assert len(detail_rows) == 2
    assert sum(int(row[row_index]) for row in detail_rows) == 11
    assert int(rows[-1][row_index]) == 11
    assert service.verify_file(bill_no)["matches"] is True


def test_failed_second_segment_resume_keeps_each_entry_once(tmp_path):
    service, bill_no, _plan_4, _plan_7 = _settled_two_deliveries(tmp_path)

    with pytest.raises(Exception, match="第 2 段"):
        service.export_bill_file(bill_no, fail_on_segment=2)

    bill = service.get_bill(bill_no)
    assert [segment["status"] for segment in bill["segments"]] == ["done", "failed"]
    assert bill["ready"] is False
    with pytest.raises(BillFileNotReadyError):
        service.download_bill(bill_no)

    # 用原账单号、新服务实例重试：检查点仍在 store 中。
    retry_service = UsageMeterService(output_dir=tmp_path / "bills-retry")
    payload = retry_service.export_bill_file(bill_no)
    text = payload["content"]
    meter_numbers = [entry["计量编号"] for entry in retry_service.get_bill(bill_no)["entries"]]

    assert text.count(meter_numbers[0]) == 1
    assert text.count(meter_numbers[1]) == 1
    assert text.count("交付行数合计") == 1
    rows = list(csv.reader(io.StringIO(text)))
    assert len(rows) == 4  # 表头 + 两笔明细 + 末尾合计

    # 再次下载是幂等的，不追加重复分段或合计行。
    again = retry_service.download_bill(bill_no)
    assert again["content"] == text
    assert again["content"].count(meter_numbers[0]) == 1
    assert again["content"].count(meter_numbers[1]) == 1


def test_router_export_and_404(tmp_path):
    service, bill_no, _p4, _p7 = _settled_two_deliveries(tmp_path)
    meter_no = service.get_bill(bill_no)["entries"][0]["计量编号"]
    response = bill_router.export_bill_file(bill_no)
    assert response.status_code == 200
    assert "text/csv" in response.media_type
    assert bill_no in response.headers["content-disposition"]
    assert meter_no.encode() in response.body

    with pytest.raises(HTTPException) as exc:
        bill_router.export_bill_file("BILL-MISSING")
    assert exc.value.status_code == 404


def test_settle_window_response_carries_original_bill_number(tmp_path):
    service = UsageMeterService(output_dir=tmp_path)
    plan = _plan(4)
    meter = service.record_usage("APP-X", "EP-X", 2, 4, int(plan["id"]))
    meter["计量窗口"] = "2026-11-25T12"
    result = service.settle_window("APP-X", "EP-X", "2026-11-25")

    assert result["账单编号"].startswith("BILL-")
    assert result["bill_no"] == result["账单编号"]
    assert result["total_rows"] == 4
    assert store.rows("usage_bill")[-1]["entries"][0]["原计划号"] == plan["计划编号"]

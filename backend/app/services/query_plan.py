"""查询计划领域服务：管理端点查询的筛选条件、扫描范围与执行策略。

执行时预估行数与实际行数偏差过大标记计划失真，执行耗时与返回行数
作为用量计量与结果集快照的输入数据集。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class QueryPlanService(BaseModuleService):
    MODULE = "query_plan"
    ENTITY = "查询计划"
    OPERATION_TERM = "查询"
    LIST_FIELDS = ["计划编号", "所属端点", "筛选条件", "扫描范围", "预估行数", "执行策略", "实际行数", "执行耗时"]
    REQUIRED_FIELDS = ["计划编号", "所属端点"]
    STATUS_ORDER = ["待执行", "执行中", "已返回", "计划失真"]
    ACTION_RULES = {"执行查询": "执行中", "确认返回": "已返回", "标记失真": "计划失真"}
    NEGATIVE_ACTIONS = []

    DISTORTION_THRESHOLD = 0.5

    def estimate_scan_rows(self, filters: dict[str, str], base_rows: int) -> int:
        """按筛选条件估算扫描行数：每多一个等值筛选条件预估行数减半。"""
        estimate = base_rows
        for _ in (filters or {}):
            estimate = max(1, estimate // 2)
        return estimate

    def build_plan(
        self,
        endpoint_id: str,
        filters: dict[str, str] | None,
        page: int,
        page_size: int,
        estimated_rows: int,
        degraded: bool,
    ) -> dict[str, Any]:
        """创建待执行查询计划并落库，返回计划记录。"""
        rows = store.rows(self.MODULE)
        new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        plan = {
            "id": new_id,
            "计划编号": f"QPLN-{new_id:04d}",
            "所属端点": endpoint_id,
            "筛选条件": str(filters or {}),
            "扫描范围": f"page={page},size={page_size}",
            "预估行数": str(estimated_rows),
            "执行策略": "degraded" if degraded else "full",
            "实际行数": "0",
            "执行耗时": "0",
            "status": "执行中",
            "pending": True,
            "abnormal": False,
        }
        rows.append(plan)
        return plan

    def complete_plan(self, plan_id: int, actual_rows: int, elapsed_ms: float) -> dict[str, Any]:
        """确认返回：写入实际行数与执行耗时，偏差过大标记计划失真。

        预估行数与实际行数的相对偏差超过阈值时把计划标记为失真，
        失真计划会进入优化清单，供查询端点调整预估模型消费。
        """
        plan = store.find(self.MODULE, plan_id)
        if plan is None:
            return {"status": "error", "message": "查询计划不存在"}
        estimated = int(plan.get("预估行数", "0") or "0")
        deviation = abs(actual_rows - estimated) / max(estimated, 1)
        plan["实际行数"] = str(actual_rows)
        plan["执行耗时"] = f"{elapsed_ms:.1f}ms"
        if deviation > self.DISTORTION_THRESHOLD:
            plan["status"] = "计划失真"
            plan["abnormal"] = True
        else:
            plan["status"] = "已返回"
            plan["pending"] = False
        plan["_deviation"] = round(deviation, 3)
        return {"status": plan["status"], "deviation": plan["_deviation"], "plan": plan}

    def get_plans_for_endpoint(self, endpoint_id: str) -> list[dict[str, Any]]:
        """查询端点名下的全部查询计划数据集。"""
        return [r for r in store.rows(self.MODULE) if r.get("所属端点") == endpoint_id]

    def rerun_plan(
        self,
        plan_ref: int | str | dict[str, Any],
        actual_rows: int,
        elapsed_ms: float = 1.0,
        *,
        create_new: bool = False,
    ) -> dict[str, Any]:
        """源数据更正后重跑计划。

        默认在同一计划号上追加执行历史并写入新的实际行数；create_new=True
        时复制为一次新的计划执行（新 QPLN 编号）。已出账账单保存的是出账时
        快照，因此无论采用哪种重跑方式，都不会回写账单明细。
        """
        original = plan_ref if isinstance(plan_ref, dict) else None
        if original is None:
            plan_id = int(str(plan_ref).removeprefix("QPLN-").lstrip("0") or "0") if isinstance(plan_ref, str) and str(plan_ref).startswith("QPLN-") else int(plan_ref)
            original = store.find(self.MODULE, plan_id)
        if original is None:
            return {"status": "error", "message": "查询计划不存在"}

        history = original.setdefault("执行历史", [])
        history.append({
            "execution_no": original.get("计划执行号") or original.get("计划编号"),
            "actual_rows": original.get("实际行数", "0"),
            "executed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        original["重跑次数"] = int(original.get("重跑次数", "0") or "0") + 1

        if create_new:
            rows = store.rows(self.MODULE)
            new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
            new_plan = dict(original)
            new_plan.update({
                "id": new_id,
                "计划编号": f"QPLN-{new_id:04d}",
                "实际行数": str(actual_rows),
                "执行耗时": f"{elapsed_ms:.1f}ms",
                "status": "已返回",
                "pending": False,
                "abnormal": False,
                "重跑来源": original.get("计划编号"),
                "计划执行号": f"{original.get('计划编号')}-R{original['重跑次数'] + 1}",
            })
            rows.append(new_plan)
            result = self.complete_plan(new_id, actual_rows, elapsed_ms)
            return {"status": "rerun", "plan": new_plan, "new_plan": new_plan, "result": result}

        result = self.complete_plan(int(original["id"]), actual_rows, elapsed_ms)
        original["计划执行号"] = f"{original.get('计划编号')}-R{original['重跑次数'] + 1}"
        return {"status": "rerun", "plan": original, "result": result}

    def get_distorted_plans(self) -> list[dict[str, Any]]:
        """查询全部计划失真记录，供预估模型优化消费。"""
        return [r for r in store.rows(self.MODULE) if r.get("status") == "计划失真"]


def execute_plan_with_distortion_check(endpoint_id: str, filters: dict, base_rows: int,
                                       actual_rows: int, elapsed_ms: float) -> dict:
    """模块级入口：创建查询计划、写入实际行数并判定计划失真数据集。"""
    service = QueryPlanService()
    estimated = service.estimate_scan_rows(filters, base_rows)
    plan = service.build_plan(endpoint_id, filters, 1, min(actual_rows or 1, base_rows), estimated, False)
    result = service.complete_plan(int(plan["id"]), actual_rows, elapsed_ms)
    return {"plan": plan, "status": result["status"], "deviation": result["deviation"]}


def rerun_plan(plan_ref: int | str | dict[str, Any], actual_rows: int, *args: Any, **kwargs: Any) -> dict:
    """模块级入口：源数据更正后重跑查询计划。"""
    return QueryPlanService().rerun_plan(plan_ref, actual_rows, *args, **kwargs)

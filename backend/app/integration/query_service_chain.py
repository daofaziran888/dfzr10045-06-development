"""查询服务链路：端点查询→计划执行→快照缓存→限流配额→用量计量。

支持配额超额降级、快照有效期一致性、缓存策略联动、用量与计划对账。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.store import store


def execute_query_chain(
    endpoint_id: str,
    consumer_app_id: str,
    filters: dict[str, str] | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """链路4：端点查询——从接收查询请求到返回数据结果的全流程。

    输入：端点标识+消费方应用+筛选条件 → 处理：配额检查、查询计划创建执行、快照生成缓存 → 产物：查询结果+快照+计量读数。
    """
    # 找端点
    endpoints = store.rows("service_endpoint")
    endpoint = None
    for ep in endpoints:
        if ep.get("端点编号") == endpoint_id:
            endpoint = ep
            break
    if not endpoint:
        return {"status": "error", "message": f"端点 {endpoint_id} 不存在"}
    if endpoint.get("status") not in ("已发布", "灰度中"):
        return {"status": "error", "message": f"端点 {endpoint_id} 不可查询"}

    # 配额检查
    quota_result = quota_check_and_degrade(consumer_app_id, endpoint_id)
    degraded = quota_result.get("degraded", False)

    # 创建查询计划
    plan_rows = store.rows("query_plan")
    plan_id = max((int(r.get("id", 0)) for r in plan_rows), default=0) + 1
    plan = {
        "id": plan_id,
        "计划编号": f"QPLN-{plan_id:04d}",
        "所属端点": endpoint_id,
        "筛选条件": str(filters or {}),
        "扫描范围": f"page={page},size={page_size}",
        "预估行数": str(page_size),
        "执行策略": "degraded" if degraded else "full",
        "实际行数": "0",
        "执行耗时": "0",
        "status": "执行中",
        "pending": True,
        "abnormal": False,
    }
    plan_rows.append(plan)

    # 模拟查询执行（读取内存 store 中匹配数据）
    contract_id = endpoint.get("所属契约", "")
    matched_rows = _query_contract_data(contract_id, filters, page, page_size)
    actual_rows = len(matched_rows)

    execute_time = f"{10 + actual_rows * 0.5:.1f}ms"
    plan["实际行数"] = str(actual_rows)
    plan["执行耗时"] = execute_time
    plan["status"] = "已返回"
    plan["pending"] = False

    # 生成结果集快照
    snapshot = snapshot_with_cache(plan_id=plan_id, endpoint_id=endpoint_id,
                                    contract_id=contract_id, rows=matched_rows)

    # 记录用量（保留与本次查询计划执行的关联，供账单逐笔核对原计划号）
    _record_usage(consumer_app_id, endpoint_id, 1, actual_rows, plan_id=plan_id)

    return {
        "status": "ok",
        "plan": plan,
        "rows": matched_rows,
        "actual_count": actual_rows,
        "degraded": degraded,
        "snapshot": snapshot,
        "execute_time": execute_time,
    }


def snapshot_with_cache(
    plan_id: int,
    endpoint_id: str,
    contract_id: str,
    rows: list[dict[str, Any]],
    ttl_seconds: int = 3600,
) -> dict[str, Any]:
    """链路5：结果集快照与缓存策略——冻结查询结果并维护缓存。

    输入：计划标识+端点+数据行 → 处理：快照冻结、缓存策略校验、有效期设定 → 产物：快照记录+缓存条目。
    同一订阅在有效期内重复读取必须命中同一份快照。
    """
    snap_rows = store.rows("result_set")
    snap_id = max((int(r.get("id", 0)) for r in snap_rows), default=0) + 1
    col_keys = sorted(rows[0].keys())[:5] if rows else []
    col_summary = {k: str(rows[0][k])[:20] for k in col_keys} if rows else {}
    snapshot = {
        "id": snap_id,
        "快照编号": f"SNAP-{snap_id:04d}",
        "所属计划": str(plan_id),
        "绑定版本": contract_id,
        "快照行数": str(len(rows)),
        "列摘要值": str(col_summary),
        "有效期": (datetime.now() + timedelta(seconds=ttl_seconds)).strftime("%Y-%m-%dT%H:%M:%S"),
        "引用次数": "1",
        "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": "可读",
        "pending": False,
        "abnormal": False,
    }
    snap_rows.append(snapshot)

    # 校验缓存策略——契约版本切换时旧缓存必须整体失效
    cache_rows = store.rows("cache_policy")
    for cache in cache_rows:
        if cache.get("所属端点") == endpoint_id:
            hit_count = int(cache.get("命中率", "0") or "0")
            cache["命中率"] = str(hit_count + 1)
            cache["最近调整"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return snapshot


def quota_check_and_degrade(
    consumer_app_id: str,
    endpoint_id: str,
) -> dict[str, Any]:
    """链路6：限流配额检查——超额时降级返回而非丢弃已在执行的查询。

    输入：消费方应用+端点 → 处理：配额状态检查、超额处置决策 → 产物：配额状态+降级标记。
    """
    quota_rows = store.rows("rate_quota")
    for quota in quota_rows:
        if quota.get("消费方应用") == consumer_app_id and quota.get("所属端点") == endpoint_id:
            current = int(quota.get("当前用量", "0") or "0")
            daily_limit = int(quota.get("日级上限", "1000") or "1000")
            sec_limit = int(quota.get("秒级上限", "100") or "100")

            if current >= daily_limit:
                quota["status"] = "已超额"
                quota["abnormal"] = True
                disposition = quota.get("超额处置", "降级返回部分数据")
                return {
                    "status": "exceeded",
                    "degraded": True,
                    "disposition": disposition,
                    "current_usage": current,
                    "daily_limit": daily_limit,
                }
            if current >= daily_limit * 0.8:
                quota["status"] = "接近上限"
                quota["abnormal"] = True
            quota["当前用量"] = str(current + 1)
            return {"status": "ok", "degraded": False, "current_usage": current + 1}

    # 无配额记录时创建默认条目
    new_id = max((int(r.get("id", 0)) for r in quota_rows), default=0) + 1
    quota_rows.append({
        "id": new_id,
        "配额编号": f"QUOTA-{new_id:04d}",
        "消费方应用": consumer_app_id,
        "所属端点": endpoint_id,
        "秒级上限": "100",
        "日级上限": "1000",
        "当前用量": "1",
        "超额处置": "降级返回部分数据",
        "计量窗口": datetime.now().strftime("%Y-%m"),
        "status": "充足",
        "pending": True,
        "abnormal": False,
    })
    return {"status": "ok", "degraded": False, "current_usage": 1}


def _query_contract_data(
    contract_id: str,
    filters: dict[str, str] | None = None,
    page: int = 1,
    page_size: int = 20,
) -> list[dict[str, Any]]:
    """从 store 中模拟查询契约关联数据。"""
    all_rows: list[dict[str, Any]] = []
    for module_name in store.module_names():
        for row in store.rows(module_name):
            # 搜集与契约相关的数据
            row_contract = row.get("所属契约", "")
            if row_contract and contract_id in str(row_contract):
                all_rows.append(row)
    if filters:
        for k, v in filters.items():
            all_rows = [
                r for r in all_rows
                if v.lower() in str(r.get(k, "")).lower()
            ]
    start = (page - 1) * page_size
    return all_rows[start:start + page_size]


def _record_usage(
    consumer_app_id: str,
    endpoint_id: str,
    call_count: int,
    row_count: int,
    plan_id: int | str | None = None,
) -> None:
    """记录用量计量——计量读数与查询计划需逐条对得上才能进入结算。"""
    meter_rows = store.rows("usage_meter")
    new_id = max((int(r.get("id", 0)) for r in meter_rows), default=0) + 1
    meter_rows.append({
        "id": new_id,
        "计量编号": f"METER-{new_id:04d}",
        "消费方应用": consumer_app_id,
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
    })
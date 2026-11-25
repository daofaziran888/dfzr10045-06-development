"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import bill_export, resource

# 固定账单路径包含 /api/bills 与 /api/usage_meter/bills/...，
# 必须先于通用 /api/{module}/{entry_id} 动态路由注册。
ROUTERS = [bill_export, resource]

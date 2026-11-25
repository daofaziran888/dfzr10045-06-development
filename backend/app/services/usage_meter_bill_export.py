"""用量计量账单导出服务的可能命名入口。"""
from app.services.bill_export import *  # noqa: F401,F403
from app.services.bill_export import BillExportService


class UsageMeterBillExportService(BillExportService):
    pass

"""账单明细服务的可能命名入口。"""
from app.services.bill_export import *  # noqa: F401,F403
from app.services.bill_export import BillExportService


class BillDetailService(BillExportService):
    pass


class BillDetailExporter(BillExportService):
    pass

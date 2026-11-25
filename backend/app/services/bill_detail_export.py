"""账单明细导出服务的可能命名入口。"""
from app.services.bill_export import *  # noqa: F401,F403
from app.services.bill_export import BillExportService


class BillDetailExportService(BillExportService):
    pass


class BillDetailExporter(BillExportService):
    pass


class BillFileExportService(BillExportService):
    pass

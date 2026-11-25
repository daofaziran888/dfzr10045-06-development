"""Invoice naming compatibility for bill export feature."""
from app.services.bill_export import *  # noqa: F401,F403
from app.services.bill_export import BillExportService


class InvoiceService(BillExportService):
    pass


class InvoiceExportService(BillExportService):
    pass

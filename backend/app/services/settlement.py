"""Settlement service naming compatibility."""
from app.services.bill_export import *  # noqa: F401,F403
from app.services.bill_export import BillExportService


class SettlementService(BillExportService):
    pass

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .schemas import SePayWebhookIn


BANK_ALIASES = {
    "MB": "MB",
    "MBBANK": "MB",
    "MILITARYBANK": "MB",
    "VIB": "VIB",
    "VPBANK": "VPBANK",
    "VPB": "VPBANK",
    "VIETNAMPROSPERITYBANK": "VPBANK",
    "ACB": "ACB",
    "ASIACOMMERCIALBANK": "ACB",
    "OCB": "OCB",
    "ORIENTCOMMERCIALBANK": "OCB",
    "NGANHANGPHUONGDONG": "OCB",
}


def normalize_bank(gateway: str) -> str:
    key = re.sub(r"[^A-Z0-9]", "", gateway.upper())
    return BANK_ALIASES.get(key, key[:20] or "UNKNOWN")


def parse_sepay_datetime(value: str) -> datetime:
    clean = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(clean)
    except ValueError:
        parsed = datetime.strptime(clean, "%Y-%m-%d %H:%M:%S")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    return parsed


def transaction_id(payload: SePayWebhookIn) -> str:
    return payload.referenceCode or f"SEPAY-{payload.gateway}-{payload.id}"


def combined_description(payload: SePayWebhookIn) -> str:
    parts = [payload.content, payload.description, payload.code]
    return " | ".join(part.strip() for part in parts if part and part.strip())

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class CompetitorOffer:
    nickname: str
    price: Decimal
    min_amount: Decimal
    max_amount: Decimal
    account_days: int = 0
    completed_orders: int = 0
    total_orders: int = 0


def parse_name_list(value: str) -> set[str]:
    return {line.strip().casefold() for line in value.splitlines() if line.strip()}


def propose_price(
    *,
    side: str,
    offers: list[CompetitorOffer],
    price_step: Decimal,
    target_min: Decimal,
    target_max: Decimal,
    blacklist: set[str],
    friendly_list: set[str],
    special_filter_enabled: bool,
    min_account_days: int,
    min_completed_orders: int,
    max_total_orders: int,
) -> dict:
    side = side.upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError("side chỉ nhận BUY hoặc SELL")

    eligible: list[CompetitorOffer] = []
    skipped: list[dict] = []
    for offer in offers:
        reasons: list[str] = []
        nick = offer.nickname.casefold()
        if nick in blacklist:
            reasons.append("blacklist")
        if special_filter_enabled:
            if min_account_days and offer.account_days < min_account_days:
                reasons.append("tuổi tài khoản thấp")
            if min_completed_orders and offer.completed_orders < min_completed_orders:
                reasons.append("số lệnh hoàn tất thấp")
            if max_total_orders and offer.total_orders > max_total_orders:
                reasons.append("tổng số lệnh vượt giới hạn")
        if reasons:
            skipped.append({"nickname": offer.nickname, "reasons": reasons})
        else:
            eligible.append(offer)

    if not eligible:
        return {
            "status": "NO_ELIGIBLE_COMPETITOR",
            "proposed_price": None,
            "competitor": None,
            "friendly": False,
            "skipped": skipped,
            "reason": "Không có đối thủ phù hợp sau bộ lọc",
        }

    reverse = side == "BUY"
    eligible.sort(key=lambda offer: offer.price, reverse=reverse)
    best = eligible[0]
    friendly = best.nickname.casefold() in friendly_list
    if friendly:
        proposed = best.price
        reason = "Friendly: theo cùng giá, không vượt giá"
    elif side == "BUY":
        proposed = best.price + price_step
        reason = f"BUY: cao hơn đối thủ {price_step:,.0f} VND"
    else:
        proposed = best.price - price_step
        reason = f"SELL: thấp hơn đối thủ {price_step:,.0f} VND"

    if target_min and proposed < target_min:
        proposed = target_min
        reason += "; chặn tại giá tối thiểu"
    if target_max and proposed > target_max:
        proposed = target_max
        reason += "; chặn tại giá tối đa"

    return {
        "status": "DRY_RUN_PROPOSAL",
        "proposed_price": proposed,
        "competitor": {"nickname": best.nickname, "price": best.price},
        "friendly": friendly,
        "skipped": skipped,
        "reason": reason,
    }

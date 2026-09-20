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
    competitor_min_amount: Decimal,
    competitor_max_amount: Decimal,
    price_limit: Decimal,
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
        # The target amount range describes the competitor ads we want to
        # compete with. An ad is eligible when its transaction range overlaps
        # the configured target range.
        if competitor_min_amount and offer.max_amount < competitor_min_amount:
            reasons.append("hạn mức tối đa thấp hơn Target")
        if competitor_max_amount and offer.min_amount > competitor_max_amount:
            reasons.append("hạn mức tối thiểu cao hơn Target")
        if special_filter_enabled:
            # Zero means the extension could not see this statistic on the
            # current page. Unknown data is reported, but is not rejected as
            # if it were a real zero-value account.
            if min_account_days and offer.account_days and offer.account_days < min_account_days:
                reasons.append("tuổi tài khoản thấp")
            if min_completed_orders and offer.completed_orders and offer.completed_orders < min_completed_orders:
                reasons.append("số lệnh hoàn tất thấp")
            if max_total_orders and offer.total_orders and offer.total_orders > max_total_orders:
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

    # A BUY slot must never bid above its ceiling. A SELL slot must never
    # offer below its floor. Zero means that no price guard is configured.
    if side == "BUY" and price_limit and proposed > price_limit:
        proposed = price_limit
        reason += "; chặn tại giá mua tối đa"
    if side == "SELL" and price_limit and proposed < price_limit:
        proposed = price_limit
        reason += "; chặn tại giá bán tối thiểu"

    return {
        "status": "DRY_RUN_PROPOSAL",
        "proposed_price": proposed,
        "competitor": {
            "nickname": best.nickname,
            "price": best.price,
            "min_amount": best.min_amount,
            "max_amount": best.max_amount,
        },
        "friendly": friendly,
        "skipped": skipped,
        "reason": reason,
    }

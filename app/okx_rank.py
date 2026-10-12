"""Read-only AutoP2P rank-10 pricing and purchase-cost floor previews."""
import time
from decimal import Decimal

from .okx_ads import number


TARGET_RANK = 10
MIN_SPREAD = Decimal("10")
SOURCE_MAX_AGE = 30


def plan_rank_price(data, side, cost_basis=None, own_nickname="VNTrader", now=None):
    side = side.upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError("Chiều quảng cáo phải là BUY hoặc SELL")
    now = time.time() if now is None else now
    base = {"mode": "PREVIEW_ONLY", "live_action_performed": False, "side": side,
            "target_rank": TARGET_RANK, "own_nickname": own_nickname,
            "min_spread_vnd": str(MIN_SPREAD), "source": "https://autop2p.biz/",
            "proposed_price": None, "action": "BLOCKED"}
    if not isinstance(data, dict) or data.get("ok") is not True or data.get("error"):
        return {**base, "reason": "AutoP2P chưa trả bảng giá thành công; chưa đủ dữ liệu để điều chỉnh."}
    if data.get("exchange") != "okx" or data.get("side") != side.lower() or data.get("panel") != "okx_" + side.lower():
        return {**base, "reason": "Nguồn dữ liệu sai sàn hoặc sai chiều mua/bán."}
    try:
        saved = float(data["saved_ts"])
        age = now - saved
        reported = float(data["age_sec"])
        if not (-5 <= age <= SOURCE_MAX_AGE) or not (0 <= reported <= SOURCE_MAX_AGE) or data.get("cache") != "fresh":
            return {**base, "reason": "Nguồn giá cũ hoặc sai thời gian; không sửa/ẩn quảng cáo theo dữ liệu này."}
    except (KeyError, TypeError, ValueError, OverflowError):
        return {**base, "reason": "Thiếu hoặc sai thời gian cập nhật AutoP2P."}
    rows = data.get("rows")
    if not isinstance(rows, list):
        return {**base, "reason": "Bảng giá AutoP2P sai cấu trúc."}
    competitors = []
    excluded = 0
    try:
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("nick"), str) or not row["nick"].strip():
                raise ValueError("Thiếu biệt danh")
            price = number(row.get("price"))
            if price != price.to_integral_value():
                raise ValueError("Giá VND sai bước giá")
            if row["nick"].strip().casefold() == own_nickname.strip().casefold():
                excluded += 1
                continue
            competitors.append({"nickname": row["nick"], "price": price})
    except ValueError:
        return {**base, "reason": "Có dòng giá thiếu/sai dữ liệu; không tự bỏ dòng làm thay đổi thứ hạng."}
    # Use the displayed table order, so rank 10 follows the user's source.
    # Only remove our own ads; no silent condition/amount filtering or re-sorting.
    if len(competitors) < TARGET_RANK:
        return {**base, "reason": "Chưa có đủ 10 quảng cáo đối thủ sau khi loại quảng cáo của mình."}
    chosen = competitors[TARGET_RANK - 1]
    price = chosen["price"]
    base.update(source_age_seconds=round(age, 2), own_ads_excluded=excluded,
                competitor={"nickname": chosen["nickname"], "price": str(price)},
                proposed_price=str(price))
    if side == "SELL":
        if cost_basis is None:
            return {**base, "reason": "Chưa có giá vốn thực tế; không suy đoán giá vốn hoặc tự sửa/ẩn quảng cáo."}
        cost = number(cost_basis)
        floor = cost + MIN_SPREAD
        base.update(cost_basis=str(cost), minimum_sell_price=str(floor),
                    spread_vnd=str(price - cost))
        if price < floor:
            return {**base, "action": "HIDE_AD", "reason":
                f"Giá bán vị trí 10 là {price:,.0f}đ, thấp hơn giá vốn {cost:,.0f}đ +10đ = {floor:,.0f}đ. "
                "Đề xuất tạm ẩn quảng cáo và báo kênh cảnh báo giá; chưa gửi yêu cầu ẩn thật."}
    return {**base, "action": "UPDATE_PRICE", "reason":
        "Đề xuất bám cùng giá đối thủ vị trí 10 sau khi loại VNTrader; không cộng/trừ thêm 1đ. "
        "Chênh giá 10đ là trước phí nếu có. Chưa gửi yêu cầu sửa thật."}


async def read_rank_plan(client, side, cost_basis=None):
    side = side.upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError("Chiều quảng cáo phải là BUY hoặc SELL")
    response = await client.get("https://autop2p.biz/", params={"api": "panel", "panel": "okx_" + side.lower()})
    response.raise_for_status()
    return plan_rank_price(response.json(), side, cost_basis)


async def send_preview_alert(plan):
    from .config import settings
    from .discord_notifications import send_discord_message
    from .telegram_commands import format_vietnam_time
    from datetime import datetime, timezone
    if not settings.discord_webhook_pricing_alerts or plan.get("action") != "HIDE_AD":
        return False
    message = ("[MÔ PHỎNG CẢNH BÁO GIÁ — chưa ẩn quảng cáo thật]\nOKX · VNTrader\n"
        f"Thời gian VN: {format_vietnam_time(datetime.now(timezone.utc))}\n"
        f"Giá vốn: {plan['cost_basis']}đ/USDT\n"
        f"Giá bán vị trí 10: {plan['proposed_price']}đ/USDT\n"
        f"Giá bán tối thiểu: {plan['minimum_sell_price']}đ/USDT\n"
        f"Lý do: {plan['reason']}")
    return await send_discord_message("PRICING_ALERT", message)

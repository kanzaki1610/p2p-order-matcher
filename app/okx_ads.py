"""Authenticated ad inspection and price-change preview. No exchange writes."""
import re
from decimal import Decimal
from urllib.parse import urlencode

from .okx_release import request


def number(value):
    try:
        result = Decimal(str(value))
    except Exception:
        raise ValueError("Giá quảng cáo thiếu hoặc sai định dạng") from None
    if not result.is_finite() or result <= 0:
        raise ValueError("Giá quảng cáo phải là số dương hữu hạn")
    return result


async def read_ad(client, ad_id):
    if not re.fullmatch(r"[0-9]{1,30}", ad_id):
        raise ValueError("ID quảng cáo không hợp lệ")
    row = await request(client, "GET", "/api/v5/p2p/ad?" + urlencode({"adId": ad_id}))
    if str(row.get("adId")) != ad_id or row.get("isOwner") is not True:
        raise ValueError("API chưa xác nhận đây là quảng cáo của tài khoản này")
    if str(row.get("cryptoCurrency", "")).upper() != "USDT" or str(row.get("fiatCurrency", "")).upper() != "VND":
        raise ValueError("Chỉ hỗ trợ quảng cáo USDT/VND")
    if row.get("side") not in {"buy", "sell"}:
        raise ValueError("API chưa xác nhận chiều mua/bán quảng cáo")
    return row


def summary(row):
    return {key: row.get(key) for key in ["adId", "side", "type", "unitPrice", "status",
        "isHidden", "minOrderSize", "maxOrderSize"]}


def price_preview(row, side, price, min_price, max_price):
    if row.get("isOwner") is not True or row.get("side") != side.lower():
        raise ValueError("Quảng cáo không thuộc tài khoản hoặc sai chiều mua/bán")
    if row.get("type") != "limit" or row.get("status") != "new" or row.get("isHidden") is not False:
        raise ValueError("Chỉ mô phỏng sửa quảng cáo giá cố định đang hiển thị và hoạt động")
    price, lower, upper = number(price), number(min_price), number(max_price)
    if lower > upper or not lower <= price <= upper:
        raise ValueError("Giá đề xuất nằm ngoài khoảng giá cho phép")
    if price != price.to_integral_value():
        raise ValueError("Giá USDT/VND phải là số đồng nguyên")
    current = number(row.get("unitPrice"))
    return {"mode": "PREVIEW_ONLY", "live_action_performed": False,
        "ad": summary(row), "proposed_price": str(price), "price_difference": str(price - current),
        "request_body": {"adId": str(row["adId"]), "unitPrice": str(price)},
        "endpoint": "/api/v5/p2p/ad/update", "would_change": price != current,
        "note": "Chưa gửi yêu cầu sửa. OKX có thể hủy quảng cáo cũ và trả newAdId; cần lưu ID mới sau khi sửa."}

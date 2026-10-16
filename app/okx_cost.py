"""Cost from the last completed authenticated BUY, ordered by completion time."""
import asyncio
import time
from decimal import Decimal
from datetime import datetime, timezone
from urllib.parse import urlencode

from .config import settings
from .okx_ads import number
from .okx_release import OKXRequestError, request, safe_code
from .okx_sync import sign


def history_rows(data):
    if isinstance(data, dict):
        data = next((data[k] for k in ("orders", "orderList", "items", "list", "records", "data") if isinstance(data.get(k), list)), None)
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict) and not data[0].get("orderId"):
        return history_rows(data[0])
    if not isinstance(data, list) or any(not isinstance(row, dict) or not row.get("orderId") for row in data):
        raise ValueError("Lịch sử mua có dòng thiếu mã lệnh")
    return data


def completion_time(row):
    values = [int(row[k]) for k in ("completionTimestamp", "completedTimestamp") if row.get(k)]
    if not values or min(values) <= 0 or len(set(values)) != 1 or values[0] > int(time.time()*1000) + 5000:
        raise ValueError("Thiếu hoặc sai thời điểm hoàn tất lệnh mua")
    return values[0]


def completed_buy(row):
    return (row.get("side") == "buy" and row.get("orderStatus") == "completed"
        and row.get("paymentStatus") == "confirmed"
        and str(row.get("cryptoCurrency", "")).upper() == "USDT"
        and str(row.get("fiatCurrency", "")).upper() == "VND")


async def history_page(client, page):
    path = "/api/v5/p2p/order/list?" + urlencode({"side": "buy", "pageIndex": page, "pageSize": 20,
        "cryptoCurrencies": "USDT", "fiatCurrencies": "VND", "completionStatus": "completed"})
    for attempt in range(3):
        stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        response = await client.get(settings.okx_base_url.rstrip("/") + path, headers={
            "OK-ACCESS-KEY": settings.okx_api_key, "OK-ACCESS-TIMESTAMP": stamp,
            "OK-ACCESS-PASSPHRASE": settings.okx_api_passphrase,
            "OK-ACCESS-SIGN": sign(settings.okx_api_secret, stamp, path)})
        if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
            await asyncio.sleep(attempt + 1)
            continue
        if response.status_code != 200:
            raise OKXRequestError(f"OKX lịch sử mua HTTP {response.status_code}.")
        payload = response.json()
        if not isinstance(payload, dict) or str(payload.get("code")) != "0":
            raise OKXRequestError("OKX lịch sử mua từ chối; mã API: " + safe_code(payload.get("code") if isinstance(payload,dict) else None))
        data = payload.get("data")
        if not isinstance(data, (dict, list)):
            raise ValueError("Lịch sử mua sai cấu trúc")
        return history_rows(data)


async def read_last_buy_cost(client):
    latest = None
    seen = set()
    for page in range(1, 101):
        rows = await history_page(client, page)
        for row in rows:
            if row["orderId"] in seen:
                raise ValueError("Lịch sử mua thay đổi giữa các trang; kiểm tra lại trước khi chỉnh giá")
            seen.add(row["orderId"])
            if completed_buy(row):
                timestamp = completion_time(row)
                if latest is None or timestamp > completion_time(latest):
                    latest = row
        if len(rows) < 20:
            break
    else:
        raise ValueError("Chưa đọc hết lịch sử mua trong giới hạn 100 trang; không đoán lệnh gần nhất")
    if latest is None:
        raise ValueError("Chưa có lệnh mua USDT/VND hoàn tất và xác nhận thanh toán")
    detail = await request(client, "GET", "/api/v5/p2p/order?" + urlencode({"orderId": latest["orderId"]}))
    if str(detail.get("orderId")) != str(latest["orderId"]) or not completed_buy(detail):
        raise ValueError("Chi tiết lệnh mua không còn hoàn tất hoặc sai định danh")
    if completion_time(detail) != completion_time(latest) or number(detail.get("unitPrice")) != number(latest.get("unitPrice")):
        raise ValueError("Chi tiết và lịch sử lệnh mua không khớp; chưa dùng làm giá vốn")
    return {"order_id": str(latest["orderId"]), "ad_id": str(latest.get("adId") or ""),
            "unit_price": str(number(detail["unitPrice"])), "completed_at_ms": completion_time(detail),
            "mode": "LAST_COMPLETED_BUY"}



def cost_revision(db):
    from .models import OKXManualCost
    row = db.get(OKXManualCost, 1, populate_existing=True)
    return row.revision if row else None


def cost_summary(db):
    from .models import OKXManualCost
    row = db.get(OKXManualCost, 1, populate_existing=True)
    if row and row.enabled:
        price = number(row.unit_price)
        return f'Giá vốn thủ công: {price:,.0f}đ/USDT · bán tối thiểu {price+10:,.0f}đ.'
    return 'Chưa nhập giá vốn OKX thủ công; chưa chỉnh hoặc ẩn quảng cáo bán.'


def save_cost(db, price, actor):
    import uuid
    from datetime import datetime, timezone
    from .config import settings
    from .models import OKXManualCost, OKXAuditLog
    if settings.database_schema == 'kayhap':
        raise ValueError('Điều khiển giá hiện chỉ hỗ trợ tài khoản gốc.')
    if price is None:
        raise ValueError('Chỉ dùng giá vốn nhập thủ công. Bấm Nhập giá vốn để lưu giá mua của bạn.')
    if price is not None:
        price = number(price)
        if price != price.to_integral_value() or price >= Decimal('1000000000000000'):
            raise ValueError('Giá vốn phải là số đồng nguyên dương, trong giới hạn hỗ trợ.')
    row = db.get(OKXManualCost, 1)
    if row is None:
        row = OKXManualCost(id=1)
        db.add(row)
    row.enabled = True
    row.unit_price = price
    row.revision = uuid.uuid4().hex
    row.updated_at = datetime.now(timezone.utc)
    row.actor = actor
    db.add(OKXAuditLog(action='OKX_COST_SOURCE', actor=actor, detail='MANUAL:'+str(price)))
    db.commit()
    return cost_summary(db)


async def read_pricing_cost(db, client, *, reader=None):
    from .models import OKXManualCost
    row = db.get(OKXManualCost, 1, populate_existing=True)
    revision = row.revision if row else None
    if row and row.enabled:
        price = number(row.unit_price)
        return {'unit_price':str(price), 'mode':'MANUAL', 'order_id':'thủ công', 'settings_revision':revision}
    raise ValueError('Chưa nhập giá vốn OKX thủ công. Bot không dùng lệnh mua gần nhất.')

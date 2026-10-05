"""Read-only OKX P2P order polling; never submits trading requests."""
import asyncio
import base64
import hashlib
import hmac
import logging
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlencode

import httpx
from sqlalchemy import String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from .config import settings
from .database import Base, SessionLocal
from .models import P2POrder
from .notifications import notify_event, notify_system_alert
from .telegram_commands import format_vietnam_time

logger = logging.getLogger(__name__)


class OKXOrderReceipt(Base):
    __tablename__ = "okx_order_notifications"
    order_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    notified_state: Mapped[str | None] = mapped_column(String(80), nullable=True)
    raw_order: Mapped[str] = mapped_column(Text, default="")


def sign(secret, timestamp, path):
    return base64.b64encode(hmac.new(secret.encode(), (timestamp + "GET" + path).encode(), hashlib.sha256).digest()).decode()


def items(data):
    if isinstance(data, dict):
        for key in ("orders", "orderList", "items", "list", "records", "data"):
            if isinstance(data.get(key), list):
                return items(data[key])
        return []
    if isinstance(data, list):
        if len(data) == 1 and isinstance(data[0], dict) and not data[0].get("orderId"):
            return items(data[0])
        return [row for row in data if isinstance(row, dict) and row.get("orderId")]
    return []


async def read_orders(client, page):
    path = "/api/v5/p2p/order/list?" + urlencode({"pageIndex": page, "pageSize": 100, "completionStatus": "pending"})
    timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    response = await client.get(settings.okx_base_url.rstrip("/") + path, headers={
        "OK-ACCESS-KEY": settings.okx_api_key,
        "OK-ACCESS-SIGN": sign(settings.okx_api_secret, timestamp, path),
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": settings.okx_api_passphrase,
    })
    if response.status_code != 200:
        raise RuntimeError(f"OKX HTTP {response.status_code}")
    payload = response.json()
    if str(payload.get("code")) != "0":
        raise RuntimeError("OKX API code " + str(payload.get("code")))
    return items(payload.get("data"))


def upsert(db, row):
    code = str(row["orderId"])
    order = db.scalar(select(P2POrder).where(P2POrder.order_code == code))
    party = row.get("counterpartyDetail") or {}
    if not isinstance(party, dict):
        party = {}
    name = str(party.get("realName") or row.get("counterpartyName") or "Chưa có tên xác thực")
    state = str(row.get("orderStatus") or "unknown")
    if order is None:
        created = datetime.fromtimestamp(int(row["createdTimestamp"]) / 1000, timezone.utc)
        order = P2POrder(order_code=code, side=str(row.get("side") or "UNKNOWN").upper(),
                         fiat_amount=Decimal(str(row["fiatAmount"])),
                         crypto_amount=Decimal(str(row["cryptoAmount"])), counterparty_name=name,
                         created_at=created, status="WAITING_PAYMENT" if state == "new" else "REVIEW_REQUIRED")
        db.add(order)
    # Do not guess the receiving bank from the buyer's payment details.
    receipt = db.get(OKXOrderReceipt, code)
    if receipt is None:
        receipt = OKXOrderReceipt(order_id=code)
        db.add(receipt)
    db.commit()
    return order, receipt


async def sync_orders(db, client):
    sent = 0
    for page in range(1, 101):
        rows = await read_orders(client, page)
        for row in rows:
            if str(row.get("cryptoCurrency")).upper() != "USDT" or str(row.get("fiatCurrency")).upper() != "VND":
                continue
            order, receipt = upsert(db, row)
            state = str(row.get("orderStatus")) + ":" + str(row.get("paymentStatus"))
            if receipt.notified_state == state:
                continue
            text = (f"LỆNH OKX P2P\nMã lệnh: {order.order_code}\nChiều: {order.side}\n"
                    f"Số tiền: {order.fiat_amount:,.0f} VND\nUSDT: {order.crypto_amount}\n"
                    f"Khách: {order.counterparty_name}\nNgân hàng nhận: {order.expected_bank or 'Chưa xác minh'}\n"
                    f"Trạng thái OKX: {state}\nThời gian VN: {format_vietnam_time(order.created_at)}\n"
                    "Chưa thực hiện mở khóa USDT")
            if await notify_event("ORDER_CREATED", text):
                receipt.notified_state = state
                db.commit()
                sent += 1
        if len(rows) < 100:
            break
    return sent


async def okx_sync_loop():
    failed = False
    async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
        while True:
            try:
                with SessionLocal() as db:
                    await sync_orders(db, client)
                if failed:
                    await notify_system_alert("Kết nối đọc lệnh OKX đã phục hồi")
                failed = False
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("OKX polling failed: %s", type(exc).__name__)
                if not failed:
                    await notify_system_alert("Không đọc được lệnh OKX. Kiểm tra khóa API và kết nối dịch vụ.")
                failed = True
            await asyncio.sleep(max(10, settings.okx_p2p_sync_seconds))

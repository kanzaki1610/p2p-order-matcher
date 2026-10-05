"""Outbound routing. Telegram remains available behind explicit flags."""
import logging
from datetime import datetime, timezone

from .config import settings
from .discord_notifications import send_discord_message
from .telegram import format_payment_notification, send_telegram_message
from .telegram_commands import format_vietnam_time
from sqlalchemy import select
from .models import P2POrder, PaymentMatch, BankTransaction

logger = logging.getLogger(__name__)


async def notify_event(event: str, text: str) -> bool:
    mode = settings.notification_provider
    if mode not in {"discord", "telegram", "both"}:
        logger.error("Invalid NOTIFICATION_PROVIDER")
        return False
    discord_ok = await send_discord_message(event, text) if mode in {"discord", "both"} else False
    telegram_ok = False
    if (mode in {"telegram", "both"} or (mode == "discord" and not discord_ok and settings.telegram_fallback_enabled)):
        telegram_ok = await send_telegram_message(settings.telegram_chat_id, text)
    if not (discord_ok or telegram_ok):
        logger.error("Notification delivery failed: event=%s", event)
    return discord_ok or telegram_ok


async def notify_match(decision, order, tx, score, reasons):
    text = (
        f"Trạng thái: {decision}\n"
        f"Mã lệnh: {order.order_code if order else 'Chưa khớp'}\n"
        f"Trạng thái lệnh: {order.status if order else 'Không có'}\n"
        f"Mã GD: {tx.transaction_id}\n"
        f"Thời gian VN: {format_vietnam_time(tx.occurred_at)}\n"
        + format_payment_notification(tx, score, reasons)
        + f"\nNội dung đầy đủ: {tx.description or 'Không có dữ liệu'}"
        + "\nLý do đầy đủ: " + "; ".join(reasons)
    )
    await notify_event(decision, text)
    if decision == "AUTO_MATCHED" and order and order.status == "PAYMENT_DETECTED":
        await notify_event("PAYMENT_DETECTED", text.replace("Trạng thái: AUTO_MATCHED", "Trạng thái: PAYMENT_DETECTED", 1))


async def notify_system_alert(text: str):
    await notify_event("SYSTEM_ALERT", f"Thời gian VN: {format_vietnam_time(datetime.now(timezone.utc))}\n{text}")


async def notify_mexc_new_order(order):
    from .telegram import format_mexc_new_order_notification
    text = format_mexc_new_order_notification(order)
    text += f"\nMã lệnh: {order.order_code}\nTrạng thái: {order.status}\nTạo lúc VN: {format_vietnam_time(order.created_at)}"
    return await notify_event("ORDER_CREATED", text)


async def notify_command_result(reply, db):
    event = None
    if reply.startswith("✅ ĐÃ XÁC NHẬN THANH TOÁN"):
        event = "CONFIRMED"
    elif reply.startswith("✅ ĐÃ TẠO ĐƠN"):
        event = "ORDER_CREATED"
    elif reply.startswith("⛔ ĐÃ TỪ CHỐI GIAO DỊCH"):
        event = "REJECTED"
    if not event:
        return
    code = next((line.removeprefix("Mã đơn: ").strip() for line in reply.splitlines() if line.startswith("Mã đơn: ")), "")
    order = db.scalar(select(P2POrder).where(P2POrder.order_code == code))
    if order:
        reply += f"\nTrạng thái: {order.status}\nNgân hàng nhận: {order.expected_bank or 'Không chỉ định'}"
        match = db.scalar(select(PaymentMatch).where(PaymentMatch.order_id == order.id).order_by(PaymentMatch.id.desc()))
        if match:
            tx = db.get(BankTransaction, match.transaction_id)
            if tx:
                reply += f"\nNgân hàng: {tx.bank}\nMã GD: {tx.transaction_id}\nNội dung: {tx.description or 'Không có'}\nThời gian giao dịch VN: {format_vietnam_time(tx.occurred_at)}"
    await notify_event(event, reply)

"""Outbound routing. Telegram remains available behind explicit flags."""
import logging
import hashlib
import re
from datetime import datetime, timezone

from .config import settings
from .discord_notifications import send_discord_message
from .telegram import format_payment_notification, send_telegram_message
from .telegram_commands import format_vietnam_time
from sqlalchemy import select
from .models import P2POrder, PaymentMatch, BankTransaction, NotificationDelivery
from .database import SessionLocal
from sqlalchemy.orm import object_session
from sqlalchemy.exc import IntegrityError


async def notify_once(db, entity, event, text, fingerprint=None, sender=None):
    key=hashlib.sha256((entity+'|'+event+'|'+(fingerprint if fingerprint is not None else text)).encode()).hexdigest()
    existing=db.get(NotificationDelivery,key)
    if existing is not None: return existing.state=='SENT'
    db.add(NotificationDelivery(key=key,state='PENDING'))
    try: db.commit()
    except IntegrityError:
        db.rollback();return False
    delivered=await (sender or notify_event)(event,text)
    row=db.get(NotificationDelivery,key)
    row.state='SENT' if delivered else 'FAILED'
    db.commit()
    return delivered


def release_text(db,exchange,order,tx,state,reason=None):
    from .matching import funding_parts
    parts=funding_parts(db,order)
    labels={'RELEASED':'Đã mở khóa','WAITING_BUYER_PAYMENT':'Chờ người mua xác nhận thanh toán','WAITING_PAYMENT_INFO':'Chờ thông tin thanh toán','SUBMITTED':'Đã gửi yêu cầu, chờ sàn xác nhận','UNKNOWN':'Chưa rõ kết quả mở khóa','SUBMITTING':'Đang kiểm tra yêu cầu','REVIEW_REQUIRED':'Cần kiểm tra thủ công'}
    text=f'{exchange} · {labels.get(state,state)}\nLệnh: {order.order_code}\nNgười mua: {order.counterparty_name}'
    if tx:
        text+=f'\nNgân hàng: {tx.bank}\nĐã nhận: {tx.amount:,.0f}/{order.fiat_amount:,.0f} VND'
        if tx.amount>order.fiat_amount:text+=f' · dư {tx.amount-order.fiat_amount:,.0f} VND'
        text+='\nGD: '+(', '.join(part.transaction_id for part in parts[:10]) if parts else tx.transaction_id)
        if len(parts)>1:text+=f' ({len(parts)} khoản)'
        text+='\nNội dung: '+(tx.description or '').split('|',1)[0].strip()[:220]
    if reason:text+='\nLý do: '+reason[:500]
    text+='\nVN: '+format_vietnam_time(datetime.now(timezone.utc))
    return text


def compact_memo(value):
    from .matching import normalize_text
    parts=[]
    for part in (value or 'Không có').split('|'):
        part=part.strip()
        if part and not any(normalize_text(old) in normalize_text(part) for old in parts): parts.append(part)
    return ' | '.join(parts)[:220]

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
    from .matching import funding_parts, memo_contains_order_code, memo_contains_name, same_name
    db=object_session(tx) or (object_session(order) if order else None)
    owned=db is None
    if owned: db=SessionLocal()
    try:
        if order and order.id is not None:
            order=db.get(P2POrder,order.id);db.refresh(order)
            if decision in {'UNMATCHED','REVIEW_REQUIRED','PARTIAL_PAYMENT'} and order.status in {'RELEASED','CONFIRMED','CANCELLED','REJECTED'}:
                return
        parts=funding_parts(db,order) if order and order.id is not None else []
        total=sum((part.amount for part in parts),0) if parts else tx.amount
        labels={'AUTO_MATCHED':'Đã đủ tiền','PARTIAL_PAYMENT':'Chờ chuyển thêm','REVIEW_REQUIRED':'Cần kiểm tra thủ công','UNMATCHED':'Chưa xác định lệnh'}
        order_label='Lệnh tham chiếu' if decision in {'UNMATCHED','REVIEW_REQUIRED'} else 'Lệnh'
        text=f"{labels.get(decision,decision)}\n{order_label}: {order.order_code if order else 'Chưa khớp'}\nNgân hàng: {tx.bank}"
        if order and order.fiat_amount is not None:
            text+=f'\nĐã nhận: {total:,.0f}/{order.fiat_amount:,.0f} VND'
            if total<order.fiat_amount:text+=f' · thiếu {order.fiat_amount-total:,.0f} VND'
            elif total>order.fiat_amount:text+=f' · dư {total-order.fiat_amount:,.0f} VND'
            text+=f'\nNgười mua: {order.counterparty_name}'
            if memo_contains_order_code(tx.description,order.order_code):text+='\nĐối chiếu: đúng mã lệnh'
            elif same_name(order.counterparty_name,tx.sender_name) or memo_contains_name(tx.description,order.counterparty_name):text+='\nĐối chiếu: đủ họ tên khớp'
        else: text+=f'\nTiền nhận: {tx.amount:,.0f} VND'
        if len(parts)>1:text+=f'\n{len(parts)} khoản: '+ ' + '.join(f'{part.amount:,.0f}' for part in parts[:10])+' VND'
        text+='\nGD: '+', '.join(part.transaction_id for part in parts[:10]) if parts else '\nGD: '+tx.transaction_id
        text+='\nNội dung: '+compact_memo(tx.description)
        if decision in {'UNMATCHED','REVIEW_REQUIRED'}:
            roots=[reason for reason in reasons if reason.startswith(('Có ','Không auto:','Không có ','Khoản ','Lệnh ','Các khoản ','Chưa ','Giao dịch ','Cấu hình ')) and 'điểm' not in reason.casefold()]
            reason=roots[-1] if roots else 'Chưa đủ căn cứ xác định khoản tiền thuộc lệnh này.'
            text+='\nLý do: '+re.sub(r'\s*\+\d+','',reason)[:900]
        text+='\nVN: '+format_vietnam_time(tx.occurred_at)
        entity=f'match:{decision}:{order.id if order else "none"}:{tx.id}'
        if tx.id is None:await notify_event(decision,text)
        else:await notify_once(db,entity,decision,text)
        if decision=='AUTO_MATCHED' and order and order.status=='PAYMENT_DETECTED':
            if tx.id is None:await notify_event('PAYMENT_DETECTED',text)
            else:await notify_once(db,entity,'PAYMENT_DETECTED',text)
    finally:
        if owned:db.close()


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

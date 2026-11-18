"""Primary account's one-time OKX order greetings; never retry chat writes."""
import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import Boolean, DateTime, String, Text, select, update, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base, SessionLocal
from .config import settings
from . import primary_controls
from .models import P2POrder, OKXAuditLog
from .okx_sync import OKXOrderReceipt
from .okx_release import detail, request, failure_reason

logger = logging.getLogger('uvicorn.error.okx_auto_messages')


def now():
    return datetime.now(timezone.utc)


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class AutoMessageConfig(Base):
    __tablename__ = 'okx_auto_message_config'
    side: Mapped[str] = mapped_column(String(4), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    body: Mapped[str] = mapped_column(Text, default='')
    revision: Mapped[str] = mapped_column(String(32), default=lambda: uuid.uuid4().hex)
    enabled_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AutoMessageDelivery(Base):
    __tablename__ = 'okx_auto_message_deliveries'
    order_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    side: Mapped[str] = mapped_column(String(4))
    body: Mapped[str] = mapped_column(Text)
    revision: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(20), default='PENDING', index=True)
    reason: Mapped[str] = mapped_column(String(500), default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ConfigConflict(ValueError):
    pass


def enabled():
    return __package__ == 'app' and primary_controls.enabled()


def configured():
    return settings.okx_p2p_enabled and all((settings.okx_api_key, settings.okx_api_secret, settings.okx_api_passphrase))


def config_row(db, side):
    if side not in {'BUY', 'SELL'}:
        raise ValueError('Chọn Mua USDT hoặc Bán USDT.')
    row = db.get(AutoMessageConfig, side)
    if row is None:
        db.add(AutoMessageConfig(side=side))
        try: db.commit()
        except IntegrityError: db.rollback()
        row = db.get(AutoMessageConfig, side)
    return row


def save(db, side, active, body, revision):
    if not enabled(): raise ValueError('Tin nhắn OKX chỉ dành cho tài khoản chính.')
    if type(active) is not bool or not isinstance(body, str): raise ValueError('Cấu hình tin nhắn không hợp lệ.')
    body = body.replace('\r\n', '\n').strip()
    if len(body) > 1000 or any(ord(c) < 32 and c not in '\n\t' for c in body):
        raise ValueError('Nội dung tối đa 1.000 ký tự, không chứa ký tự điều khiển.')
    if active and not body: raise ValueError('Nhập nội dung trước khi bật tự gửi.')
    config_row(db, side)
    row = db.scalar(select(AutoMessageConfig).where(AutoMessageConfig.side == side)
                    .with_for_update().execution_options(populate_existing=True))
    if row.revision != revision: raise ConfigConflict('Cấu hình đã đổi ở thiết bị khác. Tải lại cấu hình trước khi lưu.')
    if row.enabled == active and row.body == body: return
    if active and not row.enabled: row.enabled_since = now()
    if not active: row.enabled_since = None
    row.enabled, row.body, row.revision, row.updated_at = active, body, uuid.uuid4().hex, now()
    db.add(OKXAuditLog(action='OKX_AUTO_MESSAGE_CONFIG', actor='web:primary',
        detail=f'side={side}; enabled={active}; characters={len(body)}'))
    db.commit()


def view(db):
    rows = [config_row(db, side) for side in ('BUY', 'SELL')]
    deliveries = db.scalars(select(AutoMessageDelivery).order_by(AutoMessageDelivery.created_at.desc()).limit(50)).all()
    def stamp(value): return utc(value).timestamp() if value else None
    return {'configured': bool(configured()), 'settings': [dict(side=r.side, enabled=r.enabled, text=r.body,
        revision=r.revision, enabled_since=stamp(r.enabled_since)) for r in rows],
        'deliveries': [dict(order_id=r.order_id, side=r.side, text=r.body,
            state=('UNKNOWN' if r.state == 'SUBMITTING' and r.claimed_at and now()-utc(r.claimed_at)>timedelta(seconds=120) else r.state),
            reason=r.reason, created_at=stamp(r.created_at), sent_at=stamp(r.sent_at)) for r in deliveries]}


def eligible(row, cfg):
    try:
        created = datetime.fromtimestamp(int(row['createdTimestamp']) / 1000, timezone.utc)
        return (cfg.enabled and bool(cfg.body) and cfg.enabled_since is not None
            and utc(cfg.enabled_since) <= created <= now()+timedelta(seconds=5)
            and re.fullmatch(r'[0-9]{1,30}', str(row.get('orderId', ''))) is not None
            and str(row.get('side')).upper() == cfg.side
            and str(row.get('cryptoCurrency')).upper() == 'USDT'
            and str(row.get('fiatCurrency')).upper() == 'VND'
            and str(row.get('orderStatus')).lower() == 'new'
            and str(row.get('paymentStatus')).lower() in {'unpaid', 'paid', 'unreceived'}
            and row.get('isFrozen') is not True)
    except (ValueError, TypeError, KeyError, OverflowError, OSError): return False


def enqueue(db):
    configs = {r.side: r for r in db.scalars(select(AutoMessageConfig).where(AutoMessageConfig.enabled.is_(True)))}
    cutoffs = [utc(r.enabled_since) for r in configs.values() if r.enabled_since]
    if not cutoffs: return
    rows = db.scalars(select(OKXOrderReceipt).join(P2POrder, P2POrder.order_code == OKXOrderReceipt.order_id)
        .outerjoin(AutoMessageDelivery, AutoMessageDelivery.order_id == OKXOrderReceipt.order_id)
        .where(AutoMessageDelivery.order_id.is_(None), P2POrder.created_at >= min(cutoffs))
        .order_by(P2POrder.created_at).limit(100)).all()
    for receipt in rows:
        try:
            row = json.loads(receipt.raw_order)
            age = now().timestamp() - float(row.get('_checked_at', 0))
            cfg = configs.get(str(row.get('side')).upper())
            if cfg is None or not -5 <= age <= 60 or not eligible(row, cfg): continue
            db.add(AutoMessageDelivery(order_id=receipt.order_id, side=cfg.side, body=cfg.body, revision=cfg.revision))
            db.commit()
        except IntegrityError: db.rollback()
        except (ValueError, TypeError, AttributeError): db.rollback()


def finish(db, code, state, reason='', sent=False, expected='PENDING'):
    db.execute(update(AutoMessageDelivery).where(AutoMessageDelivery.order_id == code,
        AutoMessageDelivery.state == expected).values(
        state=state, reason=reason[:500], sent_at=now() if sent else None))
    db.commit()


async def process(db, client, code):
    if not enabled() or not configured(): return
    record = db.get(AutoMessageDelivery, code, populate_existing=True)
    if not record or record.state != 'PENDING': return
    cfg = db.get(AutoMessageConfig, record.side, populate_existing=True)
    if not cfg or not cfg.enabled or cfg.revision != record.revision:
        finish(db, code, 'SKIPPED', 'Đã tắt hoặc đổi cấu hình trước lúc gửi.'); return
    db.commit()  # Do not hold database locks across network reads.
    try: row = await detail(client, code)
    except asyncio.CancelledError: raise
    except Exception as error:
        finish(db, code, 'PENDING', 'Chưa gửi; đang chờ đọc lại lệnh. ' + failure_reason(error)); return
    if not eligible(row, cfg):
        finish(db, code, 'SKIPPED', 'Lệnh không còn phù hợp: đã đóng/hủy, sai chiều hoặc ngoài thời điểm bật.'); return
    order = db.scalar(select(P2POrder).where(P2POrder.order_code == code))
    try:
        consistent = (str(row.get('orderId')) == code and order is not None
            and order.side == record.side and Decimal(str(row['fiatAmount'])) == order.fiat_amount
            and Decimal(str(row['cryptoAmount'])) == order.crypto_amount)
    except (KeyError, ValueError, ArithmeticError): consistent = False
    if not consistent:
        finish(db, code, 'PENDING', 'Chưa gửi; dữ liệu lệnh chưa khớp lần đồng bộ.'); return
    db.expire_all()
    cfg = db.scalar(select(AutoMessageConfig).where(AutoMessageConfig.side == record.side)
                    .with_for_update().execution_options(populate_existing=True))
    if not cfg or not cfg.enabled or cfg.revision != record.revision:
        finish(db, code, 'SKIPPED', 'Đã tắt hoặc đổi cấu hình trong lúc kiểm tra lệnh.'); return
    latest = db.scalar(select(func.max(AutoMessageDelivery.claimed_at)))
    if latest and now()-utc(latest) < timedelta(seconds=2): db.rollback(); return
    body = record.body
    claimed = db.execute(update(AutoMessageDelivery).where(AutoMessageDelivery.order_id == code,
        AutoMessageDelivery.state == 'PENDING', AutoMessageDelivery.revision == cfg.revision)
        .values(state='SUBMITTING', claimed_at=now(), reason='Đã bắt đầu gửi; không tự gửi lại.'))
    db.commit()
    if claimed.rowcount != 1: return
    try:
        result = await request(client, 'POST', '/api/v5/p2p/chat/send-message',
            {'orderId': code, 'content': body, 'contentType': '101'})
        if result.get('isSentSuccessfully') is True:
            finish(db, code, 'SENT', sent=True, expected='SUBMITTING')
        elif result.get('isSentSuccessfully') is False:
            finish(db, code, 'FAILED', 'OKX xác nhận chưa gửi được tin; không tự gửi lại.', expected='SUBMITTING')
        else:
            finish(db, code, 'UNKNOWN', 'Phản hồi chưa xác nhận gửi thành công. Kiểm tra chat OKX; không tự gửi lại.', expected='SUBMITTING')
    except asyncio.CancelledError: raise  # Durable SUBMITTING blocks replay after restart.
    except Exception as error:
        finish(db, code, 'UNKNOWN', failure_reason(error) + ' Kiểm tra chat OKX; không tự gửi lại.', expected='SUBMITTING')


async def cycle(db, client):
    if not enabled() or not configured(): return
    enqueue(db)
    codes = db.scalars(select(AutoMessageDelivery.order_id).where(AutoMessageDelivery.state == 'PENDING')
        .order_by(AutoMessageDelivery.created_at).limit(20)).all()
    for code in codes: await process(db, client, code)


async def message_loop():
    while enabled():
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                with SessionLocal() as db: await cycle(db, client)
        except asyncio.CancelledError: raise
        except Exception:
            logger.warning('OKX auto-message cycle failed; no uncertain chat request will be repeated')
        await asyncio.sleep(5)

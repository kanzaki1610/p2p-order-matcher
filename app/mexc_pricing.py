"""Separate MEXC pricing state; never resubmit uncertain exchange writes."""
import asyncio
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import httpx
from sqlalchemy import Boolean, Integer, Numeric, String, Text, select, update
from sqlalchemy.orm import Mapped, mapped_column

from .config import settings
from .database import Base, SessionLocal
from .mexc import MexcP2PClient, MexcAPIError
from .mexc_ads import read_ad, update_body, write_price, write_visibility
from .okx_ads import number
from .okx_rank import plan_rank_price
from .notifications import notify_once
from .discord_notifications import send_discord_message, valid_webhook

logger = logging.getLogger('uvicorn.error.mexc_pricing')


class MexcPriceSettings(Base):
    __tablename__ = 'mexc_price_settings'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    cost: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    revision: Mapped[str] = mapped_column(String(32), default=lambda: uuid.uuid4().hex)


class MexcPriceSlot(Base):
    __tablename__ = 'mexc_price_slots'
    key: Mapped[str] = mapped_column(String(12), primary_key=True)
    ad: Mapped[str] = mapped_column(String(40), default='')
    nickname: Mapped[str] = mapped_column(String(100), default='')
    rank: Mapped[int] = mapped_column(Integer, default=10)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[str] = mapped_column(String(32), default=lambda: uuid.uuid4().hex)
    state: Mapped[str] = mapped_column(String(20), default='IDLE')
    action: Mapped[str | None] = mapped_column(String(12), nullable=True)
    wanted_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    wanted_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    paused_by_floor: Mapped[bool] = mapped_column(Boolean, default=False)
    attempt_id: Mapped[str] = mapped_column(String(32), default='')
    last_action: Mapped[Decimal] = mapped_column(Numeric(20, 3), default=0)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)


def cost_settings(db):
    row = db.get(MexcPriceSettings, 1, populate_existing=True)
    if row is None:
        row = MexcPriceSettings(id=1)
        db.add(row); db.commit()
    return row


async def alert(db, slot, reason, suffix=''):
    text = (f'MEXC · Bám giá {slot.key}\nQuảng cáo: {slot.ad}\n{reason}\n'
            + datetime.now(timezone.utc).astimezone(__import__('zoneinfo').ZoneInfo('Asia/Ho_Chi_Minh')).strftime('VN: %H:%M:%S %d/%m/%Y'))
    key = f'mexc-price:{slot.key}:{slot.ad}:{slot.attempt_id}:{suffix}'
    await notify_once(db, key, 'MEXC_PRICING_ALERT', text, fingerprint=reason,
        sender=lambda event, body: send_discord_message(event, body))


async def rank_plan(client, slot, cost):
    side = slot.key.split(':')[0]
    response = await client.get('https://autop2p.biz/', params={'api':'panel','panel':'mexc_'+side.lower()})
    response.raise_for_status()
    data = response.json()
    if (not isinstance(data, dict) or data.get('exchange') != 'mexc'
            or str(data.get('side')).upper() != side or data.get('panel') != 'mexc_'+side.lower()):
        raise ValueError('Nguồn giá không đúng sàn MEXC hoặc chiều giao dịch.')
    # AutoP2P MEXC uses uppercase side and cache=new for newly fetched snapshots.
    normalized = {**data, 'exchange':'okx', 'side':side.lower(), 'panel':'okx_'+side.lower()}
    if data.get('cache') == 'new': normalized['cache'] = 'fresh'
    return plan_rank_price(normalized, side, cost, own_nickname=slot.nickname, target_rank=slot.rank)


async def confirm(db, api, slot):
    attempt_id = slot.attempt_id
    row = await read_ad(api, slot.ad, slot.key.split(':')[0])
    db.refresh(slot)
    if slot.attempt_id != attempt_id or slot.state not in {'UNKNOWN','CONFIRMING'}:
        return False
    if number(row['price']) != slot.wanted_price or row['advStatus'] != slot.wanted_status:
        await alert(db, slot, slot.reason or 'Chưa xác minh được kết quả sửa/ẩn quảng cáo. Bot dừng gửi lại; kiểm tra MEXC.', 'unknown')
        return False
    action = slot.action
    values = {'state':'IDLE', 'reason':None}
    if action == 'HIDE': values['paused_by_floor'] = True
    if action == 'SHOW': values['paused_by_floor'] = False
    changed = db.execute(update(MexcPriceSlot).where(MexcPriceSlot.key==slot.key,
        MexcPriceSlot.attempt_id==attempt_id,MexcPriceSlot.state.in_(['UNKNOWN','CONFIRMING'])).values(**values))
    db.commit()
    if changed.rowcount != 1: return False
    db.refresh(slot)
    if action in {'HIDE','SHOW'}:
        await alert(db, slot, 'Đã xác minh tạm ẩn: giá vị trí thấp hơn giá vốn +10đ.' if action=='HIDE' else 'Đã xác minh hiển thị lại quảng cáo do bot tạm ẩn; giá đạt giá vốn +10đ.', 'confirmed')
    return True


async def process_pricing(db, api, source):
    if (not settings.mexc_ad_pricing_enabled or settings.database_schema == 'kayhap'
            or not valid_webhook(settings.discord_webhook_mexc_pricing)):
        return
    slots = db.scalars(select(MexcPriceSlot).where(MexcPriceSlot.enabled.is_(True))).all()
    for slot in slots:
        try:
            if slot.state != 'IDLE':
                await confirm(db, api, slot)
                continue
            if sum(other.ad == slot.ad for other in slots) != 1:
                raise ValueError('Một quảng cáo đang được gán cho nhiều slot; chưa sửa giá.')
            side = slot.key.split(':')[0]
            row = await read_ad(api, slot.ad, side)
            if row['advStatus'] != 'OPEN' and not (row['advStatus']=='CLOSE' and slot.paused_by_floor):
                continue  # Never reopen manually closed or low-stock advertisements.
            config = cost_settings(db)
            cost, revision, slot_revision = config.cost, config.revision, slot.revision
            if side == 'SELL' and cost is None:
                raise ValueError('Chưa nhập giá vốn MEXC riêng; chưa chỉnh hoặc ẩn quảng cáo bán.')
            plan = await rank_plan(source, slot, cost if side=='SELL' else None)
            if plan['action']=='BLOCKED': raise ValueError(plan['reason'])
            current, proposed = number(row['price']), number(plan['proposed_price'])
            action = 'HIDE' if plan['action']=='HIDE_AD' else 'PRICE'
            if action=='HIDE' and row['advStatus']=='CLOSE': continue
            if action=='PRICE' and current==proposed:
                if slot.paused_by_floor and row['advStatus']=='CLOSE': action='SHOW'
                else: continue
            price = proposed if action=='PRICE' else current
            wanted_status = 'CLOSE' if action=='HIDE' else 'OPEN' if action=='SHOW' else row['advStatus']
            if action=='PRICE': update_body(row, price)  # Validate every required field before claiming.
            if time.time()-float(slot.last_action) < max(5, settings.mexc_ad_pricing_seconds): continue
            attempt = uuid.uuid4().hex
            claim = db.execute(update(MexcPriceSlot).where(MexcPriceSlot.key==slot.key,
                MexcPriceSlot.state=='IDLE', MexcPriceSlot.enabled.is_(True), MexcPriceSlot.revision==slot_revision).values(
                    state='UNKNOWN', action=action, wanted_price=price, wanted_status=wanted_status,
                    attempt_id=attempt, last_action=Decimal(str(time.time())), reason=None))
            db.commit()
            if claim.rowcount != 1: continue
            db.refresh(slot)
            if cost_settings(db).revision != revision:
                slot.state='IDLE'; db.commit(); continue
            # Verify the original ad has not changed since the plan was calculated.
            latest = await read_ad(api, slot.ad, side)
            if latest != row:
                slot.state='IDLE'; db.commit(); continue
            db.refresh(slot)
            if not slot.enabled or slot.revision != slot_revision:
                slot.state='IDLE'; db.commit(); continue
            if cost_settings(db).revision != revision:
                slot.state='IDLE'; db.commit(); continue
            if action=='PRICE': await write_price(api, row, price)
            else: await write_visibility(api, slot.ad, action=='SHOW')
            changed=db.execute(update(MexcPriceSlot).where(MexcPriceSlot.key==slot.key,
                MexcPriceSlot.attempt_id==attempt,MexcPriceSlot.state=='UNKNOWN').values(state='CONFIRMING'))
            db.commit()
            if changed.rowcount != 1: continue
            db.refresh(slot)
            await confirm(db, api, slot)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            db.rollback(); db.refresh(slot)
            # Never display signed URLs, response bodies or arbitrary network errors.
            reason = str(error)[:450] if isinstance(error, ValueError) else 'Không xác minh được phản hồi MEXC ('+type(error).__name__+').'
            if isinstance(error, MexcAPIError):
                code=re.match(r'MEXC HTTP ([0-9]{3}) · ([A-Za-z0-9_-]{1,20}):',str(error))
                if code: reason=f'MEXC HTTP {code[1]} · mã API {code[2]}.'
            if slot.state != 'IDLE':
                slot.reason='Chưa rõ kết quả sửa/ẩn quảng cáo. '+reason+' Không tự gửi lại; kiểm tra MEXC.'
                db.commit(); reason=slot.reason
            await alert(db, slot, reason, 'unknown' if slot.state!='IDLE' else 'blocked')


async def pricing_loop():
    async with MexcP2PClient() as api, httpx.AsyncClient(timeout=30, follow_redirects=False) as source:
        while True:
            started = time.monotonic()
            try:
                with SessionLocal() as db: await process_pricing(db, api, source)
            except asyncio.CancelledError: raise
            except Exception as error: logger.error('MEXC pricing cycle: %s', type(error).__name__)
            await asyncio.sleep(max(.1, max(5, settings.mexc_ad_pricing_seconds)-(time.monotonic()-started)))

"""Explicit operator adoption of current SELL state. Exchange access is GET only."""
import asyncio
import json
import time
import uuid
from decimal import Decimal

from sqlalchemy import Boolean, Numeric, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .models import OKXAuditLog
from .mexc import MexcP2PClient
from .mexc_ads import read_ad
from .mexc_pricing import MexcPriceSlot
from .mexc_price_state import MexcPendingPrice, source_snapshot
from .mexc_price_recovery import slot_snapshot


class MexcAdoptionDraft(Base):
    __tablename__ = 'mexc_adoption_drafts'
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner: Mapped[str] = mapped_column(String(100))
    slot: Mapped[str] = mapped_column(String(12))
    record: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    expires: Mapped[Decimal] = mapped_column(Numeric(20, 3))


def record(slot, pending):
    return {'slot': list(slot_snapshot(slot)), 'enabled': slot.enabled,
            'pending': None if pending is None else {k: getattr(pending, k) for k in
                ('ad', 'attempt', 'cause', 'before_json', 'expected_json', 'body_json')}}


def eligible(slot, pending):
    if (slot is None or slot.key.split(':')[0] != 'SELL'
            or slot.state not in {'UNKNOWN', 'CONFIRMING'} or slot.action != 'PRICE'
            or time.time() - float(slot.last_action) < 600):
        raise ValueError('Chỉ nhận lại cấu hình slot Bán bị khóa giá từ ít nhất 10 phút; yêu cầu mới hoặc đổi hiển thị vẫn cần kiểm tra.')
    if pending is None or pending.ad != slot.ad or pending.attempt != slot.attempt_id or pending.cause != 'PRICE':
        raise ValueError('Thiếu hồ sơ yêu cầu giá cũ; chưa nhận cấu hình mới.')
    try:
        body = json.loads(pending.body_json)
        before = json.loads(pending.before_json)
        expected = json.loads(pending.expected_json)
        if (body['advNo'] != slot.ad or body['side'] != 'SELL'
                or {'supplyQuantity', 'reduceQuantity'} & set(body)
                or Decimal(body['initQuantity']) != Decimal(before['body']['initQuantity'])
                or Decimal(expected['quantity']) != Decimal(before['body']['initQuantity'])
                or body['advStatus'] != before['status']):
            raise ValueError()
    except (ValueError, TypeError, KeyError, ArithmeticError):
        raise ValueError('Yêu cầu cũ có đổi số lượng/hiển thị hoặc thiếu dữ liệu; chưa nhận cấu hình mới.') from None


async def stable_source(slot):
    async with MexcP2PClient() as api:
        first = await read_ad(api, slot.ad, 'SELL')
        source = source_snapshot(first)
        if Decimal(source['frozen']) != 0:
            raise ValueError('Quảng cáo có lệnh giữ USDT; chờ xử lý xong rồi kiểm tra lại.')
        await asyncio.sleep(2)
        second = await read_ad(api, slot.ad, 'SELL')
        if source_snapshot(second) != source or first.get('updateTime') != second.get('updateTime'):
            raise ValueError('Quảng cáo vừa thay đổi; kiểm tra lại để lấy cấu hình mới.')
    return source


async def preview(db, owner, number):
    if str(number) not in {'1', '2'}:
        raise ValueError('Chọn slot 1 hoặc 2.')
    slot = db.get(MexcPriceSlot, 'SELL:' + str(number), populate_existing=True)
    pending = db.get(MexcPendingPrice, slot.key) if slot else None
    eligible(slot, pending)
    # Pause first, but preserve the old attempt and every request field.
    slot.enabled = False
    slot.revision = uuid.uuid4().hex
    db.commit()
    prior = record(slot, pending)
    source = await stable_source(slot)
    db.expire_all()
    slot = db.get(MexcPriceSlot, slot.key, populate_existing=True)
    pending = db.get(MexcPendingPrice, slot.key, populate_existing=True)
    if record(slot, pending) != prior:
        raise ValueError('Slot vừa thay đổi; tạo xác nhận mới.')
    draft = MexcAdoptionDraft(id=uuid.uuid4().hex, owner=owner, slot=slot.key,
        record=json.dumps(prior, sort_keys=True), source=json.dumps(source, sort_keys=True),
        expires=Decimal(str(time.time() + 300)))
    db.add(draft)
    db.commit()
    body = source['body']
    text = (f'Nhận cấu hình hiện tại · MEXC {slot.key}\nQuảng cáo: {slot.ad}'
        f'\nGiá: {Decimal(body["price"]):,.0f}đ/USDT'
        f'\nTổng: {body["initQuantity"]} USDT · khả dụng: {source["available"]} USDT'
        f'\nMin: {Decimal(body["minSingleTransAmount"]):,.0f}đ · Max: {Decimal(body["maxSingleTransAmount"]):,.0f}đ'
        f'\nHiển thị: {source["status"]}'
        '\nBạn xác nhận dùng cấu hình MEXC này làm mốc hiện tại, lưu yêu cầu cũ vào nhật ký và gỡ khóa slot.'
        '\nKết quả yêu cầu cũ vẫn chưa được xác nhận. Chỉ đọc API; không gửi lại, không sửa quảng cáo. Auto giữ TẮT.'
        '\nXác nhận hết hạn sau 5 phút; có thay đổi dữ liệu sẽ dừng.')
    return {'content': text, 'components': [{'type': 1, 'components': [
        {'type': 2, 'style': 3, 'label': 'Xác nhận nhận cấu hình', 'custom_id': 'mprice:adoptconfirm:' + draft.id},
        {'type': 2, 'style': 2, 'label': 'Hủy', 'custom_id': 'mprice:adoptcancel:' + draft.id}]}]}


async def confirm(db, owner, draft_id):
    draft = db.get(MexcAdoptionDraft, draft_id, populate_existing=True)
    if draft is None or draft.owner != owner or draft.used or float(draft.expires) < time.time():
        raise ValueError('Xác nhận đã dùng, hết hạn hoặc không thuộc tài khoản này.')
    slot = db.get(MexcPriceSlot, draft.slot, populate_existing=True)
    pending = db.get(MexcPendingPrice, draft.slot, populate_existing=True)
    eligible(slot, pending)
    if record(slot, pending) != json.loads(draft.record):
        raise ValueError('Slot/yêu cầu cũ đã thay đổi; tạo xác nhận mới.')
    source = await stable_source(slot)
    if source != json.loads(draft.source):
        raise ValueError('Giá, số dư, Min/Max, ngân hàng hoặc điều kiện MEXC đã đổi; tạo xác nhận mới.')
    db.expire_all()
    # Same lock order as pricing/recovery; recheck after slow authenticated reads.
    slot = db.scalar(select(MexcPriceSlot).where(MexcPriceSlot.key == draft.slot)
        .with_for_update().execution_options(populate_existing=True))
    pending = db.get(MexcPendingPrice, draft.slot, populate_existing=True)
    draft = db.scalar(select(MexcAdoptionDraft).where(MexcAdoptionDraft.id == draft_id)
        .with_for_update().execution_options(populate_existing=True))
    if (draft is None or draft.owner != owner or draft.used or float(draft.expires) < time.time()
            or slot is None or record(slot, pending) != json.loads(draft.record)):
        db.rollback()
        raise ValueError('Xác nhận/slot vừa thay đổi; chưa gỡ khóa.')
    eligible(slot, pending)
    db.add(OKXAuditLog(action='MEXC_SLOT_ADOPTED', actor='discord:' + owner,
        detail=json.dumps({'slot': slot.key, 'ad': slot.ad, 'previous': json.loads(draft.record),
            'adopted_source': source, 'old_result': 'UNRESOLVED', 'exchange_write': False}, sort_keys=True)))
    slot.enabled = False
    slot.state, slot.reason, slot.action = 'IDLE', None, None
    slot.wanted_price, slot.wanted_status, slot.attempt_id = None, None, ''
    slot.paused_by_floor = False
    slot.revision = uuid.uuid4().hex
    draft.used = True
    db.delete(pending)
    db.commit()
    return f'Đã nhận cấu hình hiện tại MEXC {slot.key} và lưu hồ sơ cũ (kết quả chưa xác nhận). Đã gỡ khóa; Auto vẫn TẮT. Không gửi sửa/ẩn MEXC. Kiểm tra giá vốn, vị trí và Min trước khi tự bật Auto.'

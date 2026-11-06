"""Operator adoption with GET-only reads and preserved BUY spending ledger."""
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
from .mexc_buy_budget import MexcBuyBudget, MexcBudgetOrder, remaining


class MexcAdoptionDraft(Base):
    __tablename__ = 'mexc_adoption_drafts'
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner: Mapped[str] = mapped_column(String(100))
    slot: Mapped[str] = mapped_column(String(12))
    record: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    expires: Mapped[Decimal] = mapped_column(Numeric(20, 3))


def budget_record(db, slot, *, lock=False):
    query=select(MexcBuyBudget).where(MexcBuyBudget.ad==slot.ad).execution_options(populate_existing=True)
    if lock:query=query.with_for_update()
    budget=db.scalar(query)
    if budget is None or budget.slot!=slot.key:
        raise ValueError('Chưa có sổ ngân sách Mua đúng slot; chưa nhận cấu hình. Cần thiết lập ngân sách Mua trước.')
    orders=db.scalars(select(MexcBudgetOrder).where(MexcBudgetOrder.ad==slot.ad)
        .order_by(MexcBudgetOrder.order).execution_options(populate_existing=True)).all()
    return {'total_vnd':str(budget.total_vnd),'orders':[
        [x.order,x.state,str(x.fiat),str(x.crypto)] for x in orders]}


def record(slot, pending, db=None, *, lock=False):
    result = {'slot': list(slot_snapshot(slot)), 'enabled': slot.enabled,
            'pending': None if pending is None else {k: getattr(pending, k) for k in
                ('ad', 'attempt', 'cause', 'before_json', 'expected_json', 'body_json')}}
    if db is not None and slot.key.startswith('BUY:'):result['budget']=budget_record(db,slot,lock=lock)
    return result


def eligible(slot, pending):
    if slot is not None and slot.key.startswith('BUY:') and slot.state=='IDLE' and pending is None:
        return  # A blocked history read has no submitted write to reconcile.
    if (slot is None or slot.key.split(':')[0] not in {'BUY','SELL'}
            or slot.state not in {'UNKNOWN', 'CONFIRMING'} or slot.action != 'PRICE'
            or time.time() - float(slot.last_action) < 600):
        raise ValueError('Chỉ nhận lại slot bị khóa giá từ ít nhất 10 phút; yêu cầu mới hoặc đổi hiển thị vẫn cần kiểm tra.')
    if pending is None or pending.ad != slot.ad or pending.attempt != slot.attempt_id or pending.cause != 'PRICE':
        raise ValueError('Thiếu hồ sơ yêu cầu giá cũ; chưa nhận cấu hình mới.')
    try:
        body = json.loads(pending.body_json)
        before = json.loads(pending.before_json)
        expected = json.loads(pending.expected_json)
        initial = Decimal(body['initQuantity'])
        if slot.key.startswith('BUY:'):
            supply=Decimal(body.get('supplyQuantity','0'));reduce=Decimal(body.get('reduceQuantity','0'))
            if (body['advNo']!=slot.ad or body['side']!='BUY'
                    or not all(x.is_finite() and x>=0 for x in (initial,supply,reduce))
                    or (supply>0 and reduce>0) or initial!=Decimal(before['body']['initQuantity'])
                    or Decimal(expected['quantity'])!=initial+supply-reduce
                    or body['advStatus']!=before['status']
                    or Decimal(body['price'])!=Decimal(expected['price'])
                    or any(Decimal(body[k])!=Decimal(expected[k]) for k in ('minSingleTransAmount','maxSingleTransAmount'))):
                raise ValueError()
            return
        unchanged_quantity = initial == Decimal(before['body']['initQuantity'])
        remaining_quantity = (expected.get('quantity_mode') == 'SELL_AVAILABLE'
            and initial == Decimal(before['available'])
            and Decimal(expected['stock_ceiling']) == Decimal(before['available']) + Decimal(before['frozen']))
        if (body['advNo'] != slot.ad or body['side'] != 'SELL'
                or {'supplyQuantity', 'reduceQuantity'} & set(body)
                or not (unchanged_quantity or remaining_quantity)
                or Decimal(expected['quantity']) != Decimal(before['body']['initQuantity'])
                or body['advStatus'] != before['status']):
            raise ValueError()
    except (ValueError, TypeError, KeyError, ArithmeticError):
        raise ValueError('Yêu cầu cũ có đổi số lượng/hiển thị hoặc thiếu dữ liệu; chưa nhận cấu hình mới.') from None


async def stable_source(slot, db=None):
    side=slot.key.split(':')[0]
    async with MexcP2PClient() as api:
        first = await read_ad(api, slot.ad, side)
        source = source_snapshot(first)
        if Decimal(source['frozen']) != 0:
            raise ValueError('Quảng cáo có lệnh giữ USDT; chờ xử lý xong rồi kiểm tra lại.')
        if side=='BUY':
            budget_record(db,slot)
            left=await remaining(db,api,slot,first,force=True)
            if left<0:raise ValueError('Ngân sách Mua còn lại âm; cần kiểm tra trước khi nhận cấu hình.')
        await asyncio.sleep(2)
        second = await read_ad(api, slot.ad, side)
        if source_snapshot(second) != source or first.get('updateTime') != second.get('updateTime'):
            raise ValueError('Quảng cáo vừa thay đổi; kiểm tra lại để lấy cấu hình mới.')
    return source


async def preview(db, owner, number, side='SELL'):
    if side not in {'BUY','SELL'} or str(number) not in {'1', '2'}:
        raise ValueError('Chọn slot 1 hoặc 2.')
    slot = db.get(MexcPriceSlot, side+':' + str(number), populate_existing=True)
    pending = db.get(MexcPendingPrice, slot.key) if slot else None
    eligible(slot, pending)
    # Pause first, but preserve the old attempt and every request field.
    slot.enabled = False
    slot.revision = uuid.uuid4().hex
    db.commit()
    prior = record(slot, pending)
    source = await stable_source(slot,db)
    db.expire_all()
    slot = db.get(MexcPriceSlot, slot.key, populate_existing=True)
    pending = db.get(MexcPendingPrice, slot.key, populate_existing=True)
    if record(slot, pending) != prior:
        raise ValueError('Slot vừa thay đổi; tạo xác nhận mới.')
    prior=record(slot,pending,db)
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
        '\nBạn xác nhận dùng cấu hình MEXC này làm mốc hiện tại, lưu hồ sơ và gỡ khóa slot nếu có.'
        '\nChỉ đọc API; không gửi lại, không sửa quảng cáo. Auto giữ TẮT.'
        '\nXác nhận hết hạn sau 5 phút; có thay đổi dữ liệu sẽ dừng.')
    if side=='BUY':
        saved=prior['budget'];spent=sum((Decimal(x[2]) for x in saved['orders'] if x[1]=='DONE'),Decimal(0))
        text+=f'\nNgân sách Mua còn lại: {Decimal(saved["total_vnd"])-spent:,.0f} VND. Giữ sổ tiền đã chi; không đặt lại theo Max trên sàn.'
    if pending is not None:text+='\nKết quả yêu cầu cũ vẫn chưa được xác nhận.'
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
    if record(slot, pending,db) != json.loads(draft.record):
        raise ValueError('Slot/yêu cầu cũ đã thay đổi; tạo xác nhận mới.')
    source = await stable_source(slot,db)
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
            or slot is None or record(slot, pending,db,lock=True) != json.loads(draft.record)):
        db.rollback()
        raise ValueError('Xác nhận/slot vừa thay đổi; chưa gỡ khóa.')
    eligible(slot, pending)
    db.add(OKXAuditLog(action='MEXC_SLOT_ADOPTED', actor='discord:' + owner,
        detail=json.dumps({'slot': slot.key, 'ad': slot.ad, 'previous': json.loads(draft.record),
            'adopted_source': source, 'old_result': 'UNRESOLVED' if pending is not None else 'NO_PENDING_REQUEST', 'exchange_write': False}, sort_keys=True)))
    slot.enabled = False
    slot.state, slot.reason, slot.action = 'IDLE', None, None
    slot.wanted_price, slot.wanted_status, slot.attempt_id = None, None, ''
    slot.paused_by_floor = False
    slot.revision = uuid.uuid4().hex
    draft.used = True
    if pending is not None:db.delete(pending)
    db.commit()
    return f'Đã nhận cấu hình hiện tại MEXC {slot.key} và lưu hồ sơ. Đã gỡ khóa nếu có; Auto vẫn TẮT. Không gửi sửa/ẩn MEXC. '+('Giữ nguyên ngân sách VND và sổ tiền đã chi. ' if slot.key.startswith('BUY:') else '')+'Kiểm tra vị trí và hạn mức trước khi tự bật Auto.'

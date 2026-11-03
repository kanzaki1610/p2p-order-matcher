"""Operator-requested GET-only reconciliation. Never retries an uncertain POST."""
import json
import time
import uuid

from sqlalchemy import select

from .mexc import MexcP2PClient
from .mexc_ads import read_ad
from .mexc_price_state import MexcPendingPrice, matches, source_snapshot
from .mexc_pricing import MexcPriceSlot
from .models import OKXAuditLog
from .mexc_buy_budget import rebase_after_manual


def slot_snapshot(slot):
    return tuple(str(getattr(slot, field)) for field in (
        'ad', 'state', 'action', 'attempt_id', 'wanted_price', 'wanted_status',
        'revision', 'last_action', 'reason', 'paused_by_floor'))


async def recover(db, owner, side, number):
    if side not in {'BUY', 'SELL'} or str(number) not in {'1', '2'}:
        raise ValueError('Chọn Mua/Bán và slot 1 hoặc 2.')
    key = side + ':' + str(number)
    slot = db.scalar(select(MexcPriceSlot).where(MexcPriceSlot.key == key)
        .with_for_update().execution_options(populate_existing=True))
    if slot is None:
        raise ValueError('Slot MEXC chưa được cấu hình.')
    slot.enabled, slot.revision = False, uuid.uuid4().hex
    db.commit()
    if slot.state == 'IDLE':
        return 'Slot MEXC không bị khóa. Auto đang TẮT; kiểm tra rồi tự bật khi sẵn sàng.'
    if slot.state not in {'SUBMITTING', 'UNKNOWN', 'CONFIRMING'} or time.time() - float(slot.last_action) < 120:
        return 'Yêu cầu đang xử lý hoặc chưa qua 120 giây; giữ khóa. Auto đã tắt, không gửi lại.'
    pending = db.get(MexcPendingPrice, key)
    if pending is None or pending.attempt != slot.attempt_id or pending.ad != slot.ad:
        return 'Yêu cầu cũ thiếu hồ sơ số lượng/Min/Max để xác minh đầy đủ. Auto tắt; không gỡ khóa hoặc gửi lại chỉ dựa vào giá.'
    before = slot_snapshot(slot)
    pending_before = (pending.attempt, pending.ad, pending.before_json, pending.expected_json, pending.cause)
    try:
        async with MexcP2PClient() as api:
            row = await read_ad(api, slot.ad, side)
        if slot.state == 'SUBMITTING':
            if source_snapshot(row) != json.loads(pending.before_json):
                return 'Quảng cáo khác dữ liệu trước yêu cầu; tiếp tục giữ khóa, Auto tắt.'
            outcome = 'NOT_SENT'
        elif matches(row, json.loads(pending.expected_json)):
            outcome = 'CONFIRMED'
        else:
            return 'Chưa khớp đầy đủ giá, số lượng, Min/Max và hiển thị đã gửi; giữ khóa, Auto tắt. Không gửi lại MEXC.'
    except Exception:
        return 'Chưa đọc/xác minh được quảng cáo MEXC của tài khoản này. Auto tắt; không gửi lại.'
    db.expire_all()
    slot = db.scalar(select(MexcPriceSlot).where(MexcPriceSlot.key == key)
        .with_for_update().execution_options(populate_existing=True))
    pending = db.get(MexcPendingPrice, key, populate_existing=True)
    if (slot.enabled or slot_snapshot(slot) != before or pending is None
            or (pending.attempt, pending.ad, pending.before_json, pending.expected_json, pending.cause) != pending_before):
        db.rollback()
        return 'Cấu hình vừa thay đổi trong lúc kiểm tra; chưa khôi phục. Kiểm tra lại trạng thái slot.'
    db.add(OKXAuditLog(action='MEXC_SLOT_RECOVERED', actor='discord:' + owner,
        detail=json.dumps({'slot': key, 'ad': slot.ad, 'attempt': slot.attempt_id,
            'outcome': outcome, 'expected': json.loads(pending.expected_json)}, sort_keys=True)))
    if outcome == 'CONFIRMED':
        if pending.cause == 'MANUAL_REBASE':
            expected = json.loads(pending.expected_json)
            if 'rebase_completed_vnd' not in expected:
                db.rollback()
                return 'Thiếu mốc ngân sách tại lúc gửi; giữ khóa, Auto tắt.'
            rebase_after_manual(db,slot,expected['maxSingleTransAmount'],expected['rebase_completed_vnd'])
        if slot.action == 'HIDE': slot.paused_by_floor = pending.cause == 'FLOOR'
        elif slot.action == 'SHOW': slot.paused_by_floor = False
    slot.state, slot.reason = 'IDLE', None
    db.delete(pending)
    db.commit()
    return ('Đã xác minh kết quả MEXC.' if outcome == 'CONFIRMED' else 'Đã xác minh yêu cầu chưa đến bước gửi MEXC.') + f'\n{key} · {slot.ad}. Auto vẫn TẮT; tự bật sau khi kiểm tra. Bước này chỉ đọc, không gửi yêu cầu sửa/ẩn.'

"""Operator-triggered reconciliation using authenticated GETs only."""
import json
from datetime import datetime, timezone
from sqlalchemy import select, update
import httpx
from .config import settings
from .models import OKXSlot, OKXAuditLog
from .okx_ads import read_ad, number
from .okx_ad_worker import OKXAdPriceAttempt, safe_failure
from .okx_ad_limits import OKXAutoLimits, OKXPendingLimits, limits_match


FIELDS = ('state', 'action', 'old_ad_id', 'result_ad_id', 'price', 'hidden',
          'last_action_at', 'reason', 'paused_by_floor')


def snapshot(attempt):
    result = {key: getattr(attempt, key) for key in FIELDS}
    stamp = result['last_action_at']
    if stamp:
        result['last_action_at'] = (stamp.replace(tzinfo=timezone.utc) if not stamp.tzinfo else stamp.astimezone(timezone.utc)).isoformat()
    return result


async def recover_slot(db, owner, side, slot_number):
    if settings.database_schema == 'kayhap':
        raise ValueError('Chức năng bám giá này chỉ dành cho tài khoản gốc.')
    if side not in {'BUY', 'SELL'} or type(slot_number) is not int or slot_number not in {1, 2}:
        raise ValueError('Chọn chiều mua/bán và slot 1 hoặc 2.')
    slot = db.scalar(select(OKXSlot).where(OKXSlot.side == side,
        OKXSlot.slot_number == slot_number).with_for_update().execution_options(populate_existing=True))
    if slot is None:
        raise ValueError('Slot chưa được cấu hình.')
    # Stop new writes before inspecting the previous one. Never enable on recovery.
    slot.auto_enabled = False
    db.commit()
    attempt = db.get(OKXAdPriceAttempt, slot.id)
    if attempt is None or attempt.state == 'IDLE':
        return 'Slot không có yêu cầu bị khóa. Auto đang tắt; kiểm tra Min/Max rồi tự bật khi sẵn sàng.'
    before = snapshot(attempt)
    slot_before = (slot.adv_id, slot.side, slot.current_price)
    now = datetime.now(timezone.utc)
    last = attempt.last_action_at
    last = last.replace(tzinfo=timezone.utc) if last and not last.tzinfo else last
    if (attempt.state not in {'UNKNOWN', 'CONFIRMING'} or last is None
            or (now - last).total_seconds() < 120):
        return 'Chưa khôi phục: yêu cầu đang xử lý hoặc chưa qua 120 giây. Auto đã tắt; không gửi lại OKX.'
    if attempt.old_ad_id != slot.adv_id or attempt.action not in {'UPDATE_PRICE', 'HIDE_AD'}:
        return 'Chưa khôi phục: ID/loại yêu cầu cũ chưa phù hợp để xác minh tự động. Auto đã tắt.'
    pending = db.get(OKXPendingLimits, slot.id)
    pending_before = (pending.ad_id, pending.minimum, pending.maximum) if pending else None
    code = attempt.result_ad_id or attempt.old_ad_id
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            ad = await read_ad(client, code)
        if (ad.get('side') != side.lower() or ad.get('type') != 'limit'
                or ad.get('status') != 'new' or type(ad.get('isHidden')) is not bool):
            raise ValueError('Quảng cáo không còn hoạt động đúng chiều/loại hoặc thiếu trạng thái hiển thị.')
        current = number(ad.get('unitPrice'))
        if attempt.result_ad_id:
            if current != attempt.price or ad['isHidden'] is not attempt.hidden or not limits_match(pending, attempt, ad):
                raise ValueError('Quảng cáo trả về chưa khớp giá, trạng thái hoặc Min/Max của yêu cầu cũ.')
            outcome = 'CONFIRMED'
        else:
            # A transport error, timeout or rate limit is never evidence of rejection.
            # For the explicit legacy 400/55723 rejection, require the same owned
            # active ad to still have its pre-request price, not the requested price.
            if (attempt.action != 'UPDATE_PRICE'
                    or attempt.reason != 'OKX HTTP 400; mã API: 55723.'
                    or current != slot.current_price or current == attempt.price
                    or ad['isHidden'] is not attempt.hidden):
                raise ValueError('Chưa có bằng chứng yêu cầu cũ bị từ chối hoặc hoàn tất; tiếp tục giữ khóa.')
            outcome = 'REJECTED_UNCHANGED'
    except Exception as error:
        reason = str(error) if isinstance(error, ValueError) else safe_failure(error)
        return 'Chưa khôi phục: ' + reason + ' Auto vẫn tắt; không gửi lại yêu cầu.'
    # Re-read after the network call; do not overwrite a concurrent operator/worker.
    db.expire_all()
    slot = db.scalar(select(OKXSlot).where(OKXSlot.id == slot.id).with_for_update()
        .execution_options(populate_existing=True))
    attempt = db.scalar(select(OKXAdPriceAttempt).where(OKXAdPriceAttempt.slot_id == slot.id)
        .with_for_update().execution_options(populate_existing=True))
    pending = db.get(OKXPendingLimits, slot.id)
    if (slot.auto_enabled or (slot.adv_id, slot.side, slot.current_price) != slot_before
            or attempt is None or snapshot(attempt) != before
            or ((pending.ad_id, pending.minimum, pending.maximum) if pending else None) != pending_before):
        db.rollback()
        return 'Cấu hình/yêu cầu đã thay đổi trong lúc kiểm tra; chưa gỡ khóa. Kiểm tra lại trạng thái slot.'
    collision = db.scalar(select(OKXSlot.id).where(OKXSlot.id != slot.id, OKXSlot.adv_id == code))
    if collision is not None:
        db.rollback()
        return 'Chưa khôi phục: ID quảng cáo đang gắn với slot khác. Auto vẫn tắt.'
    db.add(OKXAuditLog(action='OKX_SLOT_RECOVERED', actor='discord:' + owner,
        detail=json.dumps({'slot': slot_number, 'side': side, 'outcome': outcome,
            'previous_attempt': before, 'verified_ad': code, 'verified_price': str(current),
            'hidden': ad['isHidden'], 'minimum': ad.get('minOrderSize'),
            'maximum': ad.get('maxOrderSize')}, default=str)))
    if outcome == 'CONFIRMED':
        db.execute(update(OKXAutoLimits).where(OKXAutoLimits.slot_id == slot.id,
            OKXAutoLimits.ad_id == slot.adv_id).values(ad_id=code))
        slot.adv_id = code
        attempt.paused_by_floor = attempt.action == 'HIDE_AD'
    slot.current_price = current
    if ad.get('minOrderSize') is not None: slot.min_amount = number(ad['minOrderSize'])
    if ad.get('maxOrderSize') is not None: slot.max_amount = number(ad['maxOrderSize'])
    if pending: db.delete(pending)
    attempt.state = 'IDLE'
    attempt.action = attempt.old_ad_id = attempt.result_ad_id = None
    attempt.price = attempt.hidden = attempt.reason = attempt.notified_key = None
    # Keep last_action_at so recovery cannot bypass the shared write cooldown.
    db.commit()
    conclusion = 'Đã xác minh kết quả yêu cầu cũ.' if outcome == 'CONFIRMED' else 'OKX đã trả lỗi 400/55723; quảng cáo cũ vẫn giữ giá trước yêu cầu. Đã lưu hồ sơ lỗi và gỡ khóa.'
    return (conclusion + f'\n{side} slot {slot_number} · ID {code} · giá {current:,.0f}đ/USDT.'
            + '\nAuto vẫn TẮT. Min/Max bạn đã lưu được giữ nguyên; kiểm tra rồi bấm Bật Auto. Không gửi yêu cầu sửa/ẩn quảng cáo trong bước khôi phục.')

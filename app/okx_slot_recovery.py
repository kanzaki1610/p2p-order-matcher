"""Operator-triggered reconciliation using authenticated GETs only."""
import json
from datetime import datetime, timezone
from sqlalchemy import select, update
import httpx
from .config import settings
from .models import OKXSlot, OKXAuditLog
from .okx_ads import read_ad, number
from . import okx_buy_budget as buy_budget
from . import okx_replacement as replacement
from .okx_ad_worker import OKXAdPriceAttempt, safe_failure
from .okx_ad_limits import OKXAutoLimits, OKXPendingLimits, OKXPendingSellQuantity, limits_match, sell_quantity_matches


FIELDS = ('state', 'action', 'old_ad_id', 'result_ad_id', 'price', 'hidden',
          'last_action_at', 'reason', 'paused_by_floor')


def snapshot(attempt):
    result = {key: getattr(attempt, key) for key in FIELDS}
    stamp = result['last_action_at']
    if stamp:
        result['last_action_at'] = (stamp.replace(tzinfo=timezone.utc) if not stamp.tzinfo else stamp.astimezone(timezone.utc)).isoformat()
    return result


def manual_intent(db, attempt):
    from .discord_price_panel import ManualPriceDraft, ManualAdTerms
    from datetime import timedelta
    last=attempt.last_action_at
    last=last.replace(tzinfo=timezone.utc) if last and not last.tzinfo else last
    matches=[]
    for draft in db.scalars(select(ManualPriceDraft).where(
            ManualPriceDraft.slot_id==attempt.slot_id, ManualPriceDraft.ad_id==attempt.old_ad_id,
            ManualPriceDraft.price==attempt.price, ManualPriceDraft.hidden==attempt.hidden,
            ManualPriceDraft.used.is_(True))):
        expiry=draft.expires.replace(tzinfo=timezone.utc) if not draft.expires.tzinfo else draft.expires
        if last is None or not expiry-timedelta(minutes=5)<=last<=expiry:continue
        terms=db.get(ManualAdTerms,draft.id,populate_existing=True)
        if terms:matches.append((draft.id,terms.snapshot_json,terms.terms_json))
    if len(matches)!=1:
        raise ValueError('Chưa tìm được duy nhất hồ sơ đặt giá thủ công đã dùng đúng thời điểm yêu cầu; giữ khóa.')
    return matches[0]


def verify_manual_rejection(intent, attempt, ad):
    from .discord_price_panel import ad_snapshot
    before=json.loads(intent[1]); terms=json.loads(intent[2])
    fields={'unitPrice','minOrderLimit','maxOrderLimit','cryptoAmount'}
    if (not isinstance(before,dict) or not fields<=set(before)
            or set(before)-fields-{'_wallet_remaining'} or not isinstance(terms,dict)
            or set(terms)-{'minOrderLimit','maxOrderLimit','cryptoAmount'}):
        raise ValueError('Hồ sơ đặt giá thủ công thiếu giá/số USDT/Min/Max; giữ khóa.')
    current=ad_snapshot(ad)
    if any(number(current.get(k))!=number(before[k]) for k in fields):
        raise ValueError('Giá, số USDT hoặc Min/Max quảng cáo đã khác trước yêu cầu thủ công; chưa gỡ khóa.')
    if number(before['unitPrice'])==attempt.price and all(number(v)==number(before[k]) for k,v in terms.items()):
        raise ValueError('Hồ sơ thủ công không có thay đổi để đối chiếu chắc chắn; giữ khóa.')
    # Visibility may have been changed manually since rejection. Never restore it.
    return current


async def recover_slot(db, owner, side, slot_number):
    if settings.database_schema == 'kayhap' and not settings.discord_pricing_commands_enabled:
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
        return 'Slot không có yêu cầu bị khóa. Auto đang tắt; bấm Đọc lại từ sàn để kiểm tra lượng USDT, Min và trạng thái hiện/ẩn trước khi bật.'
    before = snapshot(attempt)
    slot_before = (slot.adv_id, slot.side, slot.current_price)
    now = datetime.now(timezone.utc)
    last = attempt.last_action_at
    last = last.replace(tzinfo=timezone.utc) if last and not last.tzinfo else last
    if (attempt.state not in {'UNKNOWN', 'CONFIRMING'} or last is None
            or (now - last).total_seconds() < 120):
        return 'Chưa khôi phục: yêu cầu đang xử lý hoặc chưa qua 120 giây. Auto đã tắt; không gửi lại OKX.'
    if attempt.old_ad_id != slot.adv_id:
        return 'Chưa khôi phục: ID quảng cáo hiện tại khác ID của yêu cầu cũ. Auto đã tắt; giữ khóa để tránh xác minh nhầm quảng cáo.'
    if attempt.action not in {'UPDATE_PRICE','HIDE_AD','MANUAL_PRICE'}:
        return 'Chưa khôi phục: loại yêu cầu cũ chưa được hỗ trợ xác minh. Auto đã tắt.'
    manual=None
    if attempt.action=='MANUAL_PRICE':
        if attempt.result_ad_id or attempt.reason!='OKX HTTP 400; mã API: 55723.':
            return 'Chưa khôi phục: yêu cầu đặt giá thủ công chưa có bằng chứng bị từ chối rõ ràng (400/55723); giữ khóa, không gửi lại. Auto đã tắt.'
        try:manual=manual_intent(db,attempt)
        except ValueError as error:return 'Chưa khôi phục: '+str(error)+' Auto đã tắt.'
        if db.get(OKXPendingLimits,slot.id) or db.get(buy_budget.OKXPendingBuyBudget,slot.id):
            return 'Chưa khôi phục: còn hồ sơ Min/Max hoặc ngân sách của yêu cầu khác; giữ khóa. Auto đã tắt.'
    pending = db.get(OKXPendingLimits, slot.id)
    pending_before = (pending.ad_id, pending.minimum, pending.maximum) if pending else None
    budget_pending = db.get(buy_budget.OKXPendingBuyBudget, slot.id)
    budget_before = (budget_pending.root_ad_id, budget_pending.old_ad_id, budget_pending.quantity) if budget_pending else None
    sell_pending=db.get(OKXPendingSellQuantity,slot.id)
    sell_before=(sell_pending.ad_id,sell_pending.quantity) if sell_pending else None
    code = attempt.result_ad_id or attempt.old_ad_id
    unavailable=False
    discovered=False
    replacement_before=replacement.snapshot(db,slot.id)
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            if replacement.lost_response(attempt):
                replacement.verify_pending(db, slot, attempt, replacement_before)
                code, ad = await replacement.discover(client, slot, attempt, replacement_before)
                discovered=True
            else:
                try:
                    ad = await read_ad(client, code)
                except Exception as error:
                    # Only a recorded explicit rejection may be retired when the old
                    # ID is unavailable AND absent from the authenticated active list.
                    # A timeout/429 or an accepted request without a result stays locked.
                    if (attempt.action!='UPDATE_PRICE' or attempt.result_ad_id
                            or attempt.reason!='OKX HTTP 400; mã API: 55723.'
                            or safe_failure(error)!='OKX HTTP 400; mã API: 55651.'):
                        raise
                    from .ad_visibility import owned_list
                    rows,more=await owned_list(client,'OKX',side,1)
                    if more or any(str(r.get('adId',''))==code for r in rows):
                        raise ValueError('ID cũ vẫn có trong danh sách quảng cáo; giữ khóa để kiểm tra lại.')
                    unavailable=True
                    ad={'isHidden':None}
        if not unavailable and (ad.get('side') != side.lower() or ad.get('type') != 'limit'
                or ad.get('status') != 'new' or type(ad.get('isHidden')) is not bool):
            raise ValueError('Quảng cáo không còn hoạt động đúng chiều/loại hoặc thiếu trạng thái hiển thị.')
        current = None if unavailable else number(ad.get('unitPrice'))
        if unavailable:
            outcome='REJECTED_AD_UNAVAILABLE'
        elif manual is not None:
            verify_manual_rejection(manual,attempt,ad)
            outcome='MANUAL_REJECTED_UNCHANGED'
        elif attempt.result_ad_id or discovered:
            if current != attempt.price or (ad['isHidden'] is not attempt.hidden and not discovered) or not limits_match(pending, attempt, ad) or not buy_budget.pending_matches(db, slot.id, attempt, ad) or not sell_quantity_matches(db,slot.id,attempt,ad):
                raise ValueError('Quảng cáo trả về chưa khớp giá, trạng thái hoặc Min/Max của yêu cầu cũ.')
            outcome = 'CONFIRMED'
        else:
            # A transport error, timeout or rate limit is never evidence of rejection.
            # An explicit 400/55147 may also be reconciled without knowing the
            # code's business meaning: the same owned fixed-price ad must still
            # exist at the saved pre-request price, different from the requested
            # price. An accepted cancel-and-replace would not leave that old ad.
            # 55147 recovery may preserve a subsequently hidden/exhausted ad;
            # never show it or treat a timeout/429 as a rejection.
            rejected_55147 = attempt.reason == 'OKX HTTP 400; mã API: 55147.'
            if (attempt.action != 'UPDATE_PRICE'
                    or attempt.reason not in {'OKX HTTP 400; mã API: 55723.', 'OKX HTTP 400; mã API: 55147.'}
                    or current != slot.current_price or current == attempt.price
                    or (ad['isHidden'] is not attempt.hidden and not rejected_55147)):
                raise ValueError('Chưa có bằng chứng yêu cầu cũ bị từ chối hoặc hoàn tất; tiếp tục giữ khóa.')
            if ((budget_pending and budget_pending.old_ad_id != attempt.old_ad_id)
                    or (pending and pending.ad_id != attempt.old_ad_id)):
                raise ValueError('Hồ sơ ngân sách/Min Max không thuộc yêu cầu cũ; giữ khóa.')
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
    budget_pending = db.get(buy_budget.OKXPendingBuyBudget, slot.id)
    sell_pending=db.get(OKXPendingSellQuantity,slot.id)
    if (slot.auto_enabled or (slot.adv_id, slot.side, slot.current_price) != slot_before
            or attempt is None or snapshot(attempt) != before
            or replacement.snapshot(db,slot.id) != replacement_before
            or ((pending.ad_id, pending.minimum, pending.maximum) if pending else None) != pending_before
            or ((budget_pending.root_ad_id, budget_pending.old_ad_id, budget_pending.quantity) if budget_pending else None) != budget_before
            or ((sell_pending.ad_id,sell_pending.quantity) if sell_pending else None)!=sell_before):
        db.rollback()
        return 'Cấu hình/yêu cầu đã thay đổi trong lúc kiểm tra; chưa gỡ khóa. Kiểm tra lại trạng thái slot.'
    if manual is not None:
        try:unchanged=manual_intent(db,attempt)==manual and not pending and not db.get(buy_budget.OKXPendingBuyBudget,slot.id)
        except ValueError:unchanged=False
        if not unchanged:
            db.rollback()
            return 'Hồ sơ đặt giá thủ công đã thay đổi trong lúc kiểm tra; chưa gỡ khóa. Auto vẫn tắt.'
    if discovered:
        try:replacement.verify_pending(db,slot,attempt,replacement_before)
        except ValueError as error:
            db.rollback()
            return 'Chưa khôi phục: '+str(error)+' Auto vẫn tắt.'
    collision = db.scalar(select(OKXSlot.id).where(OKXSlot.id != slot.id, OKXSlot.adv_id == code))
    if collision is not None:
        db.rollback()
        return 'Chưa khôi phục: ID quảng cáo đang gắn với slot khác. Auto vẫn tắt.'
    db.add(OKXAuditLog(action='OKX_SLOT_RECOVERED', actor='discord:' + owner,
        detail=json.dumps({'slot': slot_number, 'side': side, 'outcome': outcome,
            'previous_attempt': before, 'manual_draft_id':manual[0] if manual else None, 'replacement_evidence':replacement_before if discovered else None, 'verified_ad': code, 'verified_price': str(current),
            'hidden': ad['isHidden'], 'minimum': ad.get('minOrderSize'),
            'maximum': ad.get('maxOrderSize')}, default=str)))
    if outcome == 'CONFIRMED':
        buy_budget.bind_result(db, slot.id, slot.adv_id, code)
        db.execute(update(OKXAutoLimits).where(OKXAutoLimits.slot_id == slot.id,
            OKXAutoLimits.ad_id == slot.adv_id).values(ad_id=code))
        slot.adv_id = code
        attempt.paused_by_floor = attempt.action == 'HIDE_AD' and side == 'SELL'
    if current is not None: slot.current_price = current
    if ad.get('minOrderSize') is not None: slot.min_amount = number(ad['minOrderSize'])
    if ad.get('maxOrderSize') is not None: slot.max_amount = number(ad['maxOrderSize'])
    leftover = db.get(buy_budget.OKXPendingBuyBudget, slot.id)
    if leftover: db.delete(leftover)
    if pending: db.delete(pending)
    if sell_pending:db.delete(sell_pending)
    attempt.state = 'IDLE'
    attempt.action = attempt.old_ad_id = attempt.result_ad_id = None
    attempt.price = attempt.hidden = attempt.reason = attempt.notified_key = None
    # Keep last_action_at so recovery cannot bypass the shared write cooldown.
    if unavailable or (outcome == 'REJECTED_UNCHANGED' and before['reason'] == 'OKX HTTP 400; mã API: 55147.'):
        attempt.paused_by_floor=False
    db.commit()
    rejected_code = '55147' if before['reason'] == 'OKX HTTP 400; mã API: 55147.' else '55723'
    conclusion = 'Đã xác minh kết quả yêu cầu cũ.' if outcome == 'CONFIRMED' else f'OKX đã trả lỗi 400/{rejected_code}; quảng cáo cũ vẫn giữ giá trước yêu cầu. Đã lưu hồ sơ lỗi và gỡ khóa.'
    if discovered:
        conclusion='Đã tìm và xác minh duy nhất quảng cáo thay thế sau lỗi 504. Đã cập nhật ID slot và giữ chuỗi ngân sách cũ; không gửi lại yêu cầu đổi giá.'
    if manual is not None:
        conclusion='Đã xác minh lỗi 400/55723 của lần đặt giá thủ công: quảng cáo cũ vẫn giữ nguyên giá, số USDT và Min/Max. Đã lưu hồ sơ và gỡ khóa.'
    if unavailable:
        return ('Đã lưu hồ sơ lỗi 400/55723 và gỡ khóa yêu cầu cũ. OKX trả 55651 cho ID cũ và danh sách quảng cáo hoạt động không còn ID này.'
                + f'\n{side} slot {slot_number} · ID cũ {code}. Auto vẫn TẮT.'
                + '\nBấm Chọn từ quảng cáo của tôi để gắn quảng cáo đang hoạt động, kiểm tra Min rồi tự bật Auto. Không tự chọn ID thay thế hoặc gửi lại yêu cầu cũ.')
    visibility=' Quảng cáo vẫn đang ẩn; khôi phục không tự hiện quảng cáo. Bạn có thể chọn ID quảng cáo đang hoạt động khác cho slot.' if ad['isHidden'] else ''
    next_step = 'Kiểm tra cấu hình rồi tự bật Auto khi sẵn sàng.'
    if ad['isHidden']:
        next_step = 'Quảng cáo đang ẩn; tự kiểm tra trạng thái trên sàn trước khi bật Auto.'
    if 'availableAmount' in ad:
        try:
            available = buy_budget.nonnegative(ad['availableAmount'])
            minimum = number(ad.get('minOrderSize'))
            if available == 0 or available * current < minimum:
                shown_available = format(available, ',.8f').rstrip('0').rstrip('.')
                next_step = (f'Còn {shown_available} USDT · giá trị {available * current:,.0f} VND, '
                    + f'chưa đủ Min {minimum:,.0f} VND. Không bật Auto; cần điều chỉnh lượng hoặc Min rồi đọc lại từ sàn.')
        except (ValueError, ArithmeticError):
            next_step = 'Chưa xác minh được lượng USDT/Min. Không bật Auto; bấm Đọc lại từ sàn trước.'
    return (conclusion + visibility + f'\n{side} slot {slot_number} · ID {code} · giá {current:,.0f}đ/USDT.'
            + '\nAuto vẫn TẮT. ' + next_step
            + '\nKhôi phục không gửi yêu cầu sửa/ẩn quảng cáo hoặc thay đổi Min/Max.')

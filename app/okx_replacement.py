"""Durable evidence for a lost cancel-and-replace response. Exchange GETs only."""
import json
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .okx_ads import read_ad, number
from .okx_buy_budget import stock_total
from .okx_ad_limits import maximum_matches
from .ad_visibility import owned_list


class ReplacementEvidenceError(ValueError):
    """Only controlled diagnostics from the replacement-evidence validator."""


class OKXReplacementIntent(Base):
    __tablename__ = 'okx_replacement_intents'
    slot_id: Mapped[int] = mapped_column(ForeignKey('okx_slots.id'), primary_key=True)
    revision: Mapped[str] = mapped_column(String(32))
    evidence: Mapped[str] = mapped_column(Text)


def stamp(value):
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)).isoformat()


async def active_ids(client, side):
    rows, more = await owned_list(client, 'OKX', side, 1)
    if more or len(rows) >= 100:
        raise ReplacementEvidenceError('Chưa đọc đủ danh sách quảng cáo OKX để lưu/xác minh ID thay thế.')
    ids = [str(r.get('adId', '')) for r in rows]
    if any(not re.fullmatch(r'[0-9]{1,30}', x) for x in ids) or len(set(ids)) != len(ids):
        raise ReplacementEvidenceError('Danh sách quảng cáo OKX thiếu/trùng ID; chưa xác minh thay thế.')
    return sorted(ids)


async def baseline(client, slot):
    ids = await active_ids(client, slot.side)
    if slot.adv_id not in ids:
        raise ReplacementEvidenceError('ID hiện tại không còn trong danh sách quảng cáo; chưa gửi đổi giá.')
    return ids


def save(db, slot, attempt, terms, ids):
    from .okx_buy_budget import OKXPendingBuyBudget
    # Production sessions deliberately disable autoflush. Materialize the
    # pending budget/terms in this transaction before querying by primary key.
    # The worker still commits evidence and terms together before the POST.
    db.flush()
    pending = db.get(OKXPendingBuyBudget, slot.id) if slot.side == 'BUY' else None
    if slot.side == 'BUY' and (not pending or pending.old_ad_id != slot.adv_id):
        raise ReplacementEvidenceError('Thiếu hồ sơ ngân sách mua trước khi gửi; chưa đổi giá.')
    expected = {
        'old': slot.adv_id, 'side': slot.side, 'started': stamp(attempt.last_action_at),
        'price': str(number(attempt.price)), 'hidden': attempt.hidden,
        'minimum': str(number(terms.get('minOrderLimit'))),
        'maximum': str(number(terms.get('maxOrderLimit'))),
        'quantity': str(number(terms.get('cryptoAmount'))), 'baseline': ids,
        'budget_root': pending.root_ad_id if pending else None,
    }
    db.merge(OKXReplacementIntent(slot_id=slot.id, revision=uuid.uuid4().hex,
        evidence=json.dumps(expected, sort_keys=True)))


def snapshot(db, slot_id):
    row = db.get(OKXReplacementIntent, slot_id, populate_existing=True)
    return (row.revision, row.evidence) if row else None


def lost_response(attempt):
    return (attempt.action == 'UPDATE_PRICE' and not attempt.result_ad_id
            and (not attempt.reason or attempt.reason in {
                'Hết thời gian chờ phản hồi từ sàn.',
                'Lỗi kết nối mạng đến sàn; kiểm tra kết nối dịch vụ.',
                'Dữ liệu API thiếu hoặc sai định dạng bắt buộc.',
            } or str(attempt.reason).startswith(('OKX HTTP 500;', 'OKX HTTP 502;',
                'OKX HTTP 503;', 'OKX HTTP 504;'))))


def validate(evidence, slot, attempt):
    if not evidence:
        raise ReplacementEvidenceError('Yêu cầu 504 cũ chưa có hồ sơ ID trước khi gửi; chưa tự gắn ID. Chọn quảng cáo của tôi sau khi xác minh ID cũ.')
    try:
        e = json.loads(evidence[1])
        if (set(e) != {'old','side','started','price','hidden','minimum','maximum','quantity','baseline','budget_root'}
                or e['old'] != attempt.old_ad_id or e['old'] != slot.adv_id
                or e['side'] != slot.side or e['started'] != stamp(attempt.last_action_at)
                or number(e['price']) != attempt.price or type(e['hidden']) is not bool
                or e['hidden'] is not attempt.hidden or not isinstance(e['baseline'], list)
                or e['old'] not in e['baseline']
                or any(not re.fullmatch(r'[0-9]{1,30}', x) for x in e['baseline'])
                or len(set(e['baseline'])) != len(e['baseline'])):
            raise ValueError()
        for key in ('minimum','maximum','quantity'):number(e[key])
        if e['side'] == 'BUY':
            if not isinstance(e['budget_root'], str) or not re.fullmatch(r'[0-9]{1,30}', e['budget_root']):raise ValueError()
        elif e['budget_root'] is not None:raise ValueError()
        if number(e['minimum']) > number(e['maximum']):raise ValueError()
        return e
    except (ValueError, TypeError, KeyError):
        raise ReplacementEvidenceError('Hồ sơ trước khi gửi không khớp yêu cầu 504 hiện tại; giữ khóa.') from None


def verify_pending(db, slot, attempt, evidence):
    from .okx_ad_limits import OKXPendingLimits, OKXPendingSellQuantity
    from .okx_buy_budget import OKXPendingBuyBudget, OKXBuyBudget, OKXBuyBudgetAd
    e = validate(evidence, slot, attempt)
    limits = db.get(OKXPendingLimits, slot.id)
    if (not limits or limits.ad_id != e['old'] or limits.minimum != number(e['minimum'])
            or limits.maximum != number(e['maximum'])):
        raise ReplacementEvidenceError('Hồ sơ Min/Max đã thiếu hoặc khác lần gửi 504; giữ khóa.')
    if slot.side == 'BUY':
        q = db.get(OKXPendingBuyBudget, slot.id)
        root = db.get(OKXBuyBudget, e['budget_root'])
        link = db.get(OKXBuyBudgetAd, e['old'])
        if (not q or q.old_ad_id != e['old'] or q.root_ad_id != e['budget_root']
                or q.quantity != number(e['quantity']) or not root or root.slot_id != slot.id
                or not link or link.root_ad_id != e['budget_root']):
            raise ReplacementEvidenceError('Chuỗi ngân sách mua không khớp hồ sơ 504; giữ khóa.')
    else:
        q = db.get(OKXPendingSellQuantity, slot.id)
        if not q or q.ad_id != e['old'] or q.quantity != number(e['quantity']):
            raise ReplacementEvidenceError('Lượng USDT còn bán không khớp hồ sơ 504; giữ khóa.')



def visibility_matches(row, e):
    if row.get('isHidden') is e['hidden']:
        return True
    # A replacement may become hidden after buyers exhaust its tradable stock.
    # Operator recovery may bind that ID, preserving hidden and Auto OFF.
    # A hidden ad with enough stock or any change in the saved total stays locked.
    try:
        from .okx_buy_budget import nonnegative
        available = nonnegative(row.get('availableAmount'))
        total = stock_total(row)
        return (e['side'] == 'SELL' and e['hidden'] is False and row.get('isHidden') is True
                and total == number(e['quantity']) and available < total
                and available * number(row.get('unitPrice')) < number(row.get('minOrderSize')))
    except (ValueError, ArithmeticError):
        return False


def matches(row, code, e):
    try:
        created = Decimal(str(row.get('createdTimestamp')))
        started = Decimal(str(datetime.fromisoformat(e['started']).timestamp())) * 1000
        return (str(row.get('adId')) == code and row.get('isOwner') is True
            and str(row.get('cryptoCurrency', '')).upper() == 'USDT'
            and str(row.get('fiatCurrency', '')).upper() == 'VND'
            and row.get('side') == e['side'].lower() and row.get('type') == 'limit'
            and row.get('status') == 'new' and visibility_matches(row, e)
            and number(row.get('unitPrice')) == number(e['price'])
            and number(row.get('minOrderSize')) == number(e['minimum'])
            and maximum_matches(row, e['maximum'], buy_quantity=e['quantity'] if e['side'] == 'BUY' else None)
            and stock_total(row) == number(e['quantity'])
            and created.is_finite() and started - 5000 <= created <= started + 180000
            and created <= Decimal(str(datetime.now(timezone.utc).timestamp())) * 1000 + 5000)
    except (ValueError, TypeError, ArithmeticError):return False


async def discover(client, slot, attempt, evidence):
    e = validate(evidence, slot, attempt)
    from .okx_release import OKXRequestError, failure_reason
    try:
        await read_ad(client, e['old'])
    except OKXRequestError as error:
        # Authenticated Get Ad uses both codes for 'Ad is no longer available'.
        # This permits discovery only: absence from two owned active lists plus
        # unique pre-write evidence and two matching candidate reads are still
        # mandatory. Never classify the original POST as rejected or retry it.
        if failure_reason(error) not in {
                'OKX HTTP 400; mã API: 55651.',
                'OKX HTTP 400; mã API: 55349.'}:
            raise
    else:
        raise ReplacementEvidenceError('ID cũ vẫn đọc được; chưa chứng minh đã đổi sang quảng cáo khác. Giữ khóa, không gửi lại.')
    ids = await active_ids(client, slot.side)
    if e['old'] in ids:raise ReplacementEvidenceError('ID cũ vẫn có trong danh sách quảng cáo; giữ khóa.')
    new_ids = sorted(set(ids) - set(e['baseline']))
    if len(new_ids) > 10:raise ReplacementEvidenceError('Có quá nhiều ID mới để xác minh chắc chắn; giữ khóa.')
    candidates = []
    for code in new_ids:
        row = await read_ad(client, code)  # An unreadable candidate cannot be ignored.
        if matches(row, code, e):candidates.append((code, row))
    if len(candidates) != 1:
        raise ReplacementEvidenceError(f'Có {len(candidates)} quảng cáo thay thế khớp đủ giá, số USDT, Min/Max, trạng thái và thời gian; cần đúng 1 để gỡ khóa.')
    code, row = candidates[0]
    if await active_ids(client, slot.side) != ids:
        raise ReplacementEvidenceError('Danh sách quảng cáo thay đổi trong lúc xác minh; kiểm tra lại, chưa gỡ khóa.')
    row = await read_ad(client, code)
    if not matches(row, code, e):raise ReplacementEvidenceError('Quảng cáo thay thế thay đổi trong lúc xác minh; giữ khóa.')
    return code, row

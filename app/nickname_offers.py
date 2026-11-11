"""Explicit public-offer selection. Only local target settings are written."""
import hashlib
import json
import time
import uuid
from collections import Counter
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy import String, Text, Float, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base


class OfferSelection(Base):
    __tablename__ = 'pricing_nickname_offer_selections'
    key: Mapped[str] = mapped_column(String(24), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))


class OfferNotice(Base):
    __tablename__ = 'pricing_nickname_offer_notices'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)


class OfferPicker(Base):
    __tablename__ = 'pricing_nickname_offer_pickers'
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner: Mapped[str] = mapped_column(String(100))
    expires: Mapped[float] = mapped_column(Float)
    context: Mapped[str] = mapped_column(Text)


def fingerprint(row):
    # AutoP2P has no ad ID. Never use rank, price or price-dependent Max as identity.
    try:
        minimum = Decimal(str(row['min']))
        if not minimum.is_finite() or minimum <= 0:
            return None
        methods = row.get('payMethodCodes')
        if not isinstance(methods, list) or any(not isinstance(x, (str, int)) for x in methods):
            return None
        identity = {'min': format(minimum.normalize(), 'f'),
                    'methods': sorted(set(str(x) for x in methods)),
                    'method': str(row.get('payMethod', ''))}
        for name in ('supportKycCountry', 'buyerRegDaysLimit', 'minRegisterDate',
                     'userAllTradeCountMin', 'userAllTradeCountMax', 'exchangeCount',
                     'requireMobile', 'onlyTradeKybUser', 'blockTrade', 'orderPayCount',
                     'privateAd', 'maxPayLimit'):
            identity[name] = row.get(name)
        return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    except (KeyError, ValueError, TypeError, InvalidOperation):
        return None


def slots_snapshot(db, exchange, side):
    from .models import OKXSlot
    from .mexc_pricing import MexcPriceSlot
    from .nickname_target import target
    result = {}
    for n in (1, 2):
        if exchange == 'OKX':
            slot = db.scalar(select(OKXSlot).where(OKXSlot.side == side, OKXSlot.slot_number == n))
            ad = slot.adv_id if slot else ''
        else:
            slot = db.get(MexcPriceSlot, f'{side}:{n}', populate_existing=True)
            ad = slot.ad if slot else ''
        result[str(n)] = {'ad': ad, 'target': list(target(db, exchange, side, n) or ())}
    return result


async def read_offers(exchange, side, nickname):
    from .okx_rank import plan_rank_price
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        response = await client.get('https://autop2p.biz/', params={'api':'panel', 'panel':exchange.lower()+'_'+side.lower()})
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, dict) or data.get('exchange') != exchange.lower() or str(data.get('side')).upper() != side or data.get('panel') != exchange.lower()+'_'+side.lower():
        raise ValueError('Nguồn bảng giá sai sàn/chiều; chưa cho chọn.')
    normalized = {**data, 'exchange':'okx', 'side':side.lower(), 'panel':'okx_'+side.lower()}
    if exchange == 'MEXC' and data.get('cache') == 'new':
        normalized['cache'] = 'fresh'
    checked = plan_rank_price(normalized, side, own_nickname='', target_rank=1, enforce_cost=False)
    if checked['action'] != 'UPDATE_PRICE':
        raise ValueError(checked['reason'])
    return [r for r in data['rows'] if r['nick'].strip().casefold() == nickname.casefold()]


def render(db, context, owner, rows, page=0, heading=''):
    exchange, side = context['exchange'], context['side']
    prefix = 'price' if exchange == 'OKX' else 'mprice'
    context = {**context, 'slots': slots_snapshot(db, exchange, side),
               'offers': [fingerprint(r) for r in rows]}
    token = uuid.uuid4().hex
    db.add(OfferPicker(id=token, owner=owner, expires=time.time()+600,
                       context=json.dumps(context, ensure_ascii=True)))
    db.commit()
    counts = Counter(fingerprint(r) for r in rows)
    pages = max(1, (len(rows)+3)//4)
    page = min(max(0, page), pages-1)
    components = []
    lines = [heading[:500], f"{exchange} · {side} · {context['nickname']} · trang {page+1}/{pages}",
             'Chọn Slot ở bên phải dòng giá. Xanh = đã chọn dòng; không phải trạng thái Auto. Công tắc Auto giữ nguyên.',
             f'Chênh {context["offset"]:+d}đ · sàn/trần {context["bound"]:,}đ (0 = không giới hạn). Áp dụng cho slot bạn bấm.']
    for i in range(page*4, min(len(rows), page*4+4)):
        row = rows[i]; identity = fingerprint(row)
        unique = bool(identity and counts[identity] == 1)
        label = f"{row['nick']} · {Decimal(str(row['price'])):,.0f}đ"
        buttons = [{'type':2, 'style':2, 'label':label[:80], 'custom_id':f'{prefix}:offer:label:{token}:{i}', 'disabled':True}]
        lines.append(f"{i+1}. {Decimal(str(row['price'])):,.0f}đ · Min {row.get('min', '?')} · Max {row.get('max', '?')} · NH {row.get('payMethod', '?')}" + ('' if unique else ' · Không phân biệt được dòng này'))
        for n in ('1', '2'):
            current = context['slots'][n]
            follow = current['target']
            selected = bool(unique and len(follow)>4 and follow[0].casefold()==context['nickname'].casefold() and follow[4]==identity)
            buttons.append({'type':2, 'style':3 if selected else 2, 'label':f'Slot {n}' + (' ✓' if selected else ''),
                            'custom_id':f'{prefix}:offer:pick:{token}:{i}:{n}', 'disabled':not unique or not current['ad']})
        components.append({'type':1, 'components':buttons})
    nav = [{'type':2, 'style':2, 'label':'Đọc lại giá', 'custom_id':f'{prefix}:offer:page:{token}:{page}'}]
    for p, label in ((page-1, 'Trước'), (page+1, 'Sau')):
        if 0 <= p < pages:
            nav.append({'type':2, 'style':2, 'label':label, 'custom_id':f'{prefix}:offer:page:{token}:{p}'})
    components.append({'type':1, 'components':nav})
    lines.append('Nguồn không có ID quảng cáo. Nếu Min/phương thức thanh toán/điều kiện thay đổi hoặc trùng, bot dừng và yêu cầu chọn lại. Bảng hết hạn sau 10 phút.')
    return {'content':'\n'.join(x for x in lines if x)[:1900], 'components':components, 'allowed_mentions':{'parse':[]}}


async def open_picker(db, exchange, side, slot, owner, heading=''):
    from .nickname_target import target
    follow = target(db, exchange, side, slot)
    if not follow:
        raise ValueError('Lưu biệt danh cho slot trước.')
    context = {'exchange':exchange, 'side':side, 'nickname':follow[0], 'offset':follow[1], 'bound':follow[3]}
    try:
        rows = await read_offers(exchange, side, follow[0])
    except (httpx.HTTPError, ValueError):
        rows = []
        heading = 'Đã lưu biệt danh. Chưa đọc được bảng giá mới; bấm Đọc lại giá. Auto giữ nguyên.'
    return render(db, context, owner, rows, heading=heading)


async def interact(payload, db, owner, exchange):
    from .nickname_target import save, key
    parts = payload['data']['custom_id'].split(':')
    if len(parts) not in (5, 6) or parts[2] not in {'page', 'pick'}:
        raise ValueError('Nút chọn quảng cáo không hợp lệ.')
    session = db.get(OfferPicker, parts[3], populate_existing=True)
    if not session or session.owner != owner or (session.expires < time.time() and parts[2] != 'page'):
        raise ValueError('Bảng chọn hết hạn hoặc không thuộc bạn. Mở Bám biệt danh để lấy bảng mới.')
    context = json.loads(session.context)
    if context['exchange'] != exchange:
        raise ValueError('Nút sai sàn.')
    rows = await read_offers(exchange, context['side'], context['nickname'])
    # Recheck after network I/O; concurrent clicks cannot overwrite newer targets.
    db.expire_all()
    session = db.scalar(select(OfferPicker).where(OfferPicker.id == parts[3]).with_for_update())
    if not session or session.owner != owner or (session.expires < time.time() and parts[2] != 'page') or slots_snapshot(db, exchange, context['side']) != context['slots']:
        raise ValueError('Cấu hình đã thay đổi hoặc bảng hết hạn. Mở lại Bám biệt danh trước khi chọn.')
    if parts[2] == 'page':
        if len(parts) != 5 or not parts[4].isdigit():
            raise ValueError('Trang không hợp lệ.')
        return render(db, context, owner, rows, int(parts[4]))
    if len(parts) != 6 or not parts[4].isdigit() or parts[5] not in {'1', '2'}:
        raise ValueError('Slot không hợp lệ.')
    i, n = int(parts[4]), parts[5]
    offers = context['offers']
    identity = offers[i] if i < len(offers) else None
    matches = [r for r in rows if fingerprint(r) == identity] if identity else []
    if len(matches) != 1 or not context['slots'][n]['ad']:
        raise ValueError('Dòng đã thay đổi, biến mất hoặc bị trùng. Đọc lại giá và chọn lại; chưa thay cấu hình.')
    # Consume before save commits; owner/channel authorization is enforced by the router.
    session.expires = 0
    save(db, exchange, context['side'], {'slot':n, 'target':context['nickname'],
         'offset':str(context['offset']), 'bound':str(context['bound'])}, owner, commit=False)
    db.merge(OfferSelection(key=key(exchange, context['side'], n), fingerprint=identity))
    db.commit()
    return render(db, context, owner, rows, i//4,
                  f'Đã chọn dòng giá cho Slot {n}; giữ chênh {context["offset"]:+d}đ và sàn/trần {context["bound"]:,}đ. Auto giữ nguyên.')


async def publish_ambiguity(db, exchange, side, slot, follow, plan):
    """One bot-authored picker per target revision, in the authorized control channel.

    Claim before POST: uncertain Discord delivery is never blindly repeated.
    Users can always reopen via Bám biệt danh if delivery fails.
    """
    from .config import settings
    rows = plan.get('nickname_offer_choices')
    if not rows or not follow or not settings.discord_bot_token:
        return False
    owner = settings.discord_allowed_user_ids.split(',')[0].strip()
    channels = [c.strip() for c in settings.discord_command_channel_ids.split(',')
                if c.strip() and c.strip() not in {settings.discord_mexc_pricing_channel_id,
                    settings.discord_release_channel_id, settings.discord_buy_payment_channel_id}]
    channel = settings.discord_mexc_pricing_channel_id if exchange == 'MEXC' else (channels[0] if channels else '')
    if not owner.isdigit() or not channel.isdigit():
        return False
    notice_key = hashlib.sha256(f'{exchange}:{side}:{slot}:{follow[2]}'.encode()).hexdigest()
    if db.get(OfferNotice, notice_key):
        return False
    db.add(OfferNotice(key=notice_key))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return False
    context = {'exchange':exchange, 'side':side, 'nickname':follow[0], 'offset':follow[1], 'bound':follow[3]}
    outgoing = render(db, context, owner, rows,
        heading='Biệt danh có nhiều giá. Chọn quảng cáo cụ thể ở bảng dưới để tiếp tục theo đúng dòng.')
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.post(f'https://discord.com/api/v10/channels/{channel}/messages',
                headers={'Authorization':'Bot '+settings.discord_bot_token}, json=outgoing)
        return response.is_success
    except httpx.HTTPError:
        return False

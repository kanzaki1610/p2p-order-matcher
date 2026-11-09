"""Account-local per-slot nickname targets; no exchange writes in controls."""
import re
import uuid

from sqlalchemy import String, Integer, Numeric, select, delete
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class NicknameTarget(Base):
    __tablename__ = 'pricing_nickname_targets'
    key: Mapped[str] = mapped_column(String(24), primary_key=True)
    nickname: Mapped[str] = mapped_column(String(100))
    offset: Mapped[int] = mapped_column(Integer, default=0)
    revision: Mapped[str] = mapped_column(String(32))


class NicknamePriceBound(Base):
    # Separate table keeps previously deployed target rows compatible without ALTER.
    __tablename__ = 'pricing_nickname_bounds'
    key: Mapped[str] = mapped_column(String(24), primary_key=True)
    price: Mapped[int] = mapped_column(Numeric(20, 0))


def validate_bound(value=''):
    value = str(value).strip()
    if not value or value == '0':
        return 0
    if re.fullmatch(r'[0-9]{1,3}(\.[0-9]{3})+', value):
        value = value.replace('.', '')
    if not re.fullmatch(r'[0-9]{1,12}', value) or int(value) <= 0:
        raise ValueError('Giá sàn/trần phải là số VND nguyên dương; trống hoặc 0 để bỏ giới hạn.')
    return int(value)


def plan_options(follow):
    return {'target_nickname': follow[0], 'price_offset': follow[1],
            'price_bound': follow[3] if len(follow) > 3 else 0} if follow else {}


def validate_target(nickname, offset=''):
    nickname = nickname.strip()
    if not nickname or len(nickname) > 100 or any(ord(c) < 32 for c in nickname):
        raise ValueError('Nhập biệt danh đối thủ từ 1–100 ký tự, không xuống dòng.')
    offset = str(offset).strip() or '0'
    if not re.fullmatch(r'[+-]?[0-9]{1,9}', offset):
        raise ValueError('Chênh giá là số đồng nguyên: -1, +1 hoặc để trống bằng giá.')
    return nickname, int(offset)


def key(exchange, side, slot):
    if exchange not in {'OKX', 'MEXC'} or side not in {'BUY', 'SELL'} or str(slot) not in {'1', '2'}:
        raise ValueError('Chọn sàn, chiều và slot 1 hoặc 2 hợp lệ.')
    return f'{exchange}:{side}:{slot}'


def target(db, exchange, side, slot):
    row = db.get(NicknameTarget, key(exchange, side, slot), populate_existing=True)
    if row is None:
        return None
    bound = db.get(NicknamePriceBound, row.key, populate_existing=True)
    return row.nickname, row.offset, row.revision, int(bound.price) if bound else 0


def clear(db, exchange, side, slot=None):
    for n in ([slot] if slot is not None else [1, 2]):
        db.execute(delete(NicknameTarget).where(NicknameTarget.key == key(exchange, side, n)))
        db.execute(delete(NicknamePriceBound).where(NicknamePriceBound.key == key(exchange, side, n)))


def modal(prefix, side):
    if prefix not in {'price', 'mprice'} or side not in {'BUY', 'SELL'}:
        raise ValueError('Chiều không hợp lệ.')
    fields = [('slot', 'Slot 1 hoặc 2', '1', True),
              ('target', 'Biệt danh đối thủ trên AutoP2P', 'Ví dụ 1phut30s', True),
              ('offset', 'Chênh giá VND: -1, +1; trống = 0', '-1', False),
              ('bound', 'Giá bán thấp nhất (VND/USDT)' if side == 'SELL' else 'Giá mua cao nhất (VND/USDT)',
               'Trống hoặc 0: không giới hạn', False)]
    return {'type': 9, 'data': {'custom_id': f'{prefix}:form:follow:{side}',
        'title': 'Bám biệt danh ' + ('Mua' if side == 'BUY' else 'Bán'),
        'components': [{'type': 1, 'components': [{'type': 4, 'custom_id': k,
            'label': label, 'style': 1, 'required': required, 'placeholder': hint,
            'max_length': 100 if k == 'target' else 16 if k == 'bound' else 10}]} for k, label, hint, required in fields]}}


def save(db, exchange, side, values, owner):
    from .models import OKXSlot, OKXAuditLog
    from .config import settings
    from .mexc_pricing import MexcPriceSlot
    n = values.get('slot', '')
    code = key(exchange, side, n)
    nick, offset = validate_target(values.get('target', ''), values.get('offset', ''))
    bound = validate_bound(values.get('bound', ''))
    if exchange == 'OKX':
        slot = db.scalar(select(OKXSlot).where(OKXSlot.side == side, OKXSlot.slot_number == int(n)).with_for_update())
        own = settings.okx_merchant_nicknames
        ad = slot.adv_id if slot else ''
    else:
        slot = db.get(MexcPriceSlot, f'{side}:{n}', with_for_update=True)
        own = settings.mexc_merchant_nicknames + ',' + (slot.nickname if slot else '')
        ad = slot.ad if slot else ''
    if slot is None or not ad:
        raise ValueError('Chọn quảng cáo cho slot trước khi bám biệt danh.')
    if nick.casefold() in {x.strip().casefold() for x in own.split(',') if x.strip()}:
        raise ValueError('Không bám theo quảng cáo của chính tài khoản này.')
    db.merge(NicknameTarget(key=code, nickname=nick, offset=offset, revision=uuid.uuid4().hex))
    db.merge(NicknamePriceBound(key=code, price=bound))
    if exchange == 'MEXC':
        slot.revision = uuid.uuid4().hex
    db.add(OKXAuditLog(action='PRICING_NICKNAME_TARGET', actor='discord:' + owner,
                      detail=f'{code} nickname={nick}; offset={offset:+d}; bound={bound}'))
    db.commit()
    guard = (f'Giá {"sàn bán" if side == "SELL" else "trần mua"}: {bound:,}đ/USDT; giữ tại mức này khi giá đối thủ cộng chênh lệch vượt giới hạn. '
             if bound else 'Không đặt giới hạn sàn/trần riêng cho biệt danh. ')
    return {'content': f'Đã lưu {exchange} {side} slot {n}: bám {nick}, chênh {offset:+d}đ/USDT. ' + guard +
        'Giữ nguyên công tắc Auto. Không thấy người này hoặc có nhiều giá khác nhau thì chờ và cảnh báo. '
        'Giá vốn, giới hạn giá, Min/Max và giãn cách sửa giá vẫn áp dụng. Bấm Vị trí để trở lại bám vị trí.', 'components': []}


def summary(db, exchange, side, slot):
    value = target(db, exchange, side, slot)
    if not value:
        return ''
    return f'bám {value[0]} {value[1]:+d}đ' + (f' · {"sàn bán" if side == "SELL" else "trần mua"} {value[3]:,}đ' if value[3] else '')

"""Explicit per-slot limits and the exact limits sent with an auto price write."""
from decimal import Decimal, ROUND_DOWN
from sqlalchemy import ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base
from .okx_ads import number


class OKXAutoLimits(Base):
    __tablename__ = 'okx_auto_limits'
    slot_id: Mapped[int] = mapped_column(ForeignKey('okx_slots.id'), primary_key=True)
    ad_id: Mapped[str] = mapped_column(String(30))
    minimum: Mapped[Decimal] = mapped_column(Numeric(20, 0))
    maximum: Mapped[Decimal] = mapped_column(Numeric(20, 0))


class OKXPendingLimits(Base):
    __tablename__ = 'okx_pending_limits'
    slot_id: Mapped[int] = mapped_column(ForeignKey('okx_slots.id'), primary_key=True)
    ad_id: Mapped[str] = mapped_column(String(30))
    minimum: Mapped[Decimal] = mapped_column(Numeric(20, 0))
    maximum: Mapped[Decimal] = mapped_column(Numeric(20, 0))


def price_limits(db, slot, ad, price):
    # BUY tracks price only, including slots with legacy saved limits.
    if slot.side == 'BUY':
        return {}
    config = db.get(OKXAutoLimits, slot.id)
    if config is None or config.ad_id != slot.adv_id:
        return {}
    lower, upper = number(config.minimum), number(config.maximum)
    if lower > upper:
        raise ValueError('Hạn mức Min phải nhỏ hơn hoặc bằng Max.')
    if slot.side == 'SELL':
        capacity = (number(ad.get('availableAmount')) * number(price)).to_integral_value(rounding=ROUND_DOWN)
        upper = min(upper, capacity)
        if upper < lower:
            raise ValueError(f'USDT khả dụng chỉ đủ {capacity:,.0f} VND ở giá mới, thấp hơn Min {lower:,.0f} VND. Chưa sửa quảng cáo.')
    return {'minOrderLimit': format(lower, 'f'), 'maxOrderLimit': format(upper, 'f')}


def limits_match(pending, attempt, ad):
    if pending is None or attempt.action not in {'UPDATE_PRICE'}:
        return True
    return (pending.ad_id == attempt.old_ad_id
            and number(ad.get('minOrderSize')) == pending.minimum
            and number(ad.get('maxOrderSize')) == pending.maximum)

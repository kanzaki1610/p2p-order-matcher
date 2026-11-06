"""Durable pricing controls and exact request/readback records, per account schema."""
import json
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, String, Text, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .mexc_ads import update_body


class MexcPricingSwitch(Base):
    __tablename__ = 'mexc_pricing_switches'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class MexcAutoLimits(Base):
    __tablename__ = 'mexc_auto_limits'
    slot: Mapped[str] = mapped_column(ForeignKey('mexc_price_slots.key'), primary_key=True)
    ad: Mapped[str] = mapped_column(String(40))
    minimum: Mapped[Decimal] = mapped_column(Numeric(20, 0))


class MexcPendingPrice(Base):
    __tablename__ = 'mexc_pending_prices'
    slot: Mapped[str] = mapped_column(ForeignKey('mexc_price_slots.key'), primary_key=True)
    ad: Mapped[str] = mapped_column(String(40))
    attempt: Mapped[str] = mapped_column(String(32))
    before_json: Mapped[str] = mapped_column(Text)
    expected_json: Mapped[str] = mapped_column(Text)
    # Internal account database only. Never log request fields or signatures.
    body_json: Mapped[str] = mapped_column(Text)
    cause: Mapped[str] = mapped_column(String(20), default='PRICE')


def amount(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise ValueError()
        return result
    except Exception:
        raise ValueError('Thiếu/sai số lượng hoặc số tiền MEXC; chưa gửi yêu cầu.') from None


def active(db, *, lock=False):
    query = select(MexcPricingSwitch).where(MexcPricingSwitch.id == 1).execution_options(populate_existing=True)
    if lock:
        query = query.with_for_update()
    row = db.scalar(query)
    if row is None:
        try:
            with db.begin_nested():
                db.add(MexcPricingSwitch(id=1, enabled=True))
                db.flush()
        except IntegrityError:
            pass
        row = db.scalar(query)
    return row.enabled


def source_snapshot(row):
    return {'body': update_body(row, amount(row.get('price'))), 'status': row['advStatus'],
            'available': str(amount(row.get('availableQuantity'))),
            'frozen': str(amount(row.get('frozenQuantity')))}


def expected_for(row, body, *, status=None):
    total = amount(body.get('initQuantity', row.get('quantity')))
    total += amount(body.get('supplyQuantity', 0)) - amount(body.get('reduceQuantity', 0))
    if total < 0:
        raise ValueError('Số lượng tổng sau cập nhật không hợp lệ.')
    return {'price': str(amount(body.get('price', row.get('price')))), 'quantity': str(total),
            'minSingleTransAmount': str(amount(body.get('minSingleTransAmount', row.get('minSingleTransAmount')))),
            'maxSingleTransAmount': str(amount(body.get('maxSingleTransAmount', row.get('maxSingleTransAmount')))),
            'advStatus': status or body.get('advStatus', row['advStatus'])}


def matches(row, expected):
    try:
        return (row.get('advStatus') == expected['advStatus']
                and ('stock_ceiling' not in expected or
                    amount(row.get('availableQuantity')) + amount(row.get('frozenQuantity')) <= amount(expected['stock_ceiling']))
                and all(amount(row.get(key)) == amount(expected[key])
                        for key in ('price', 'quantity', 'minSingleTransAmount', 'maxSingleTransAmount')))
    except (ValueError, KeyError, TypeError):
        return False


def record_pending(db, slot, row, body, *, status=None, cause='PRICE', expected_extra=None):
    pending = db.get(MexcPendingPrice, slot.key)
    if pending is None:
        pending = MexcPendingPrice(slot=slot.key)
        db.add(pending)
    pending.ad, pending.attempt = slot.ad, slot.attempt_id
    pending.before_json = json.dumps(source_snapshot(row), sort_keys=True)
    pending.expected_json = json.dumps({**expected_for(row, body, status=status), **(expected_extra or {})}, sort_keys=True)
    pending.body_json, pending.cause = json.dumps(body, sort_keys=True), cause
    db.flush()
    return pending

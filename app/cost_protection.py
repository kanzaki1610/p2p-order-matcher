"""Explicit per-exchange cost-floor switch for the primary web controller."""
import uuid
from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base
from . import primary_controls


class PricingCostSwitch(Base):
    __tablename__='pricing_cost_switches'
    exchange: Mapped[str] = mapped_column(String(10),primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean,default=True)


def enforced(db,exchange):
    if not primary_controls.enabled():return True
    row=db.get(PricingCostSwitch,exchange,populate_existing=True)
    return row.enabled if row else True


def set_enabled(db,exchange,enabled,actor):
    from .models import OKXManualCost,OKXAuditLog
    from .mexc_pricing import MexcPriceSettings
    if not primary_controls.enabled() or exchange not in {'OKX','MEXC'} or type(enabled) is not bool:
        raise ValueError('Công tắc giá vốn không hợp lệ hoặc sai tài khoản.')
    model=OKXManualCost if exchange=='OKX' else MexcPriceSettings
    row=db.get(model,1,populate_existing=True)
    saved=(row.unit_price if row and row.enabled else None) if exchange=='OKX' else row.cost if row else None
    if enabled and (saved is None or saved<=0):raise ValueError('Nhập giá vốn hợp lệ trước khi bật áp dụng giá vốn.')
    if row is None:
        row=OKXManualCost(id=1,enabled=False,actor=actor,revision=uuid.uuid4().hex) if exchange=='OKX' else MexcPriceSettings(id=1)
        db.add(row)
    # Existing workers/manual drafts already fence their pre-POST cost revision.
    row.revision=uuid.uuid4().hex
    db.merge(PricingCostSwitch(exchange=exchange,enabled=enabled))
    db.add(OKXAuditLog(action='PRICING_COST_SWITCH',actor=actor,detail=exchange+' enabled='+str(enabled)))
    db.commit()
    return ('Đã bật áp dụng giá vốn '+exchange+': bán từ giá vốn +10đ.' if enabled else
            'Đã tắt áp dụng giá vốn '+exchange+'. Bot bỏ kiểm tra giá vốn +10đ; vẫn giữ giới hạn bám biệt danh, ngân sách và kiểm tra của sàn. Giá vốn đã nhập được giữ lại.')

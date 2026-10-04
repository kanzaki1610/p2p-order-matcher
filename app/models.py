from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class P2POrder(Base):
    __tablename__ = "p2p_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_code: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    side: Mapped[str] = mapped_column(String(10), default="SELL")
    fiat_amount: Mapped[Decimal] = mapped_column(Numeric(20, 0), index=True)
    crypto_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    counterparty_name: Mapped[str] = mapped_column(String(255))
    expected_bank: Mapped[str | None] = mapped_column(String(20), nullable=True)
    payment_note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="WAITING_PAYMENT", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    matches: Mapped[list["PaymentMatch"]] = relationship(back_populates="order")


class MexcNotification(Base):
    __tablename__ = 'mexc_notifications'

    order_code: Mapped[str] = mapped_column(String(100), primary_key=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BankTransaction(Base):
    __tablename__ = "bank_transactions"
    __table_args__ = (UniqueConstraint("bank", "transaction_id", name="uq_bank_tx"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bank: Mapped[str] = mapped_column(String(20), index=True)
    transaction_id: Mapped[str] = mapped_column(String(150))
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 0), index=True)
    direction: Mapped[str] = mapped_column(String(10), default="CREDIT")
    sender_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sender_account: Mapped[str | None] = mapped_column(String(100), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String(30), default="UNMATCHED", index=True)

    matches: Mapped[list["PaymentMatch"]] = relationship(back_populates="transaction")


class PaymentMatch(Base):
    __tablename__ = "payment_matches"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("p2p_orders.id"), index=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("bank_transactions.id"), index=True)
    score: Mapped[int]
    decision: Mapped[str] = mapped_column(String(30))
    reasons: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    order: Mapped[P2POrder] = relationship(back_populates="matches")
    transaction: Mapped[BankTransaction] = relationship(back_populates="matches")


class OrderConfirmation(Base):
    __tablename__ = "order_confirmations"
    __table_args__ = (UniqueConstraint("order_id", name="uq_order_confirmation"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("p2p_orders.id"), index=True)
    telegram_user_id: Mapped[str] = mapped_column(String(50))
    telegram_username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    telegram_display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OrderRejection(Base):
    __tablename__ = "order_rejections"
    __table_args__ = (UniqueConstraint("order_id", name="uq_order_rejection"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("p2p_orders.id"), index=True)
    reason: Mapped[str] = mapped_column(String(500))
    telegram_user_id: Mapped[str] = mapped_column(String(50))
    telegram_username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    telegram_display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    rejected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OKXDashboardConfig(Base):
    __tablename__ = "okx_dashboard_config"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    buy_target_min: Mapped[Decimal] = mapped_column(Numeric(20, 0), default=0)
    buy_target_max: Mapped[Decimal] = mapped_column(Numeric(20, 0), default=0)
    sell_target_min: Mapped[Decimal] = mapped_column(Numeric(20, 0), default=0)
    sell_target_max: Mapped[Decimal] = mapped_column(Numeric(20, 0), default=0)
    scan_buy_seconds: Mapped[int] = mapped_column(Integer, default=10)
    scan_sell_seconds: Mapped[int] = mapped_column(Integer, default=10)
    price_step: Mapped[Decimal] = mapped_column(Numeric(20, 0), default=1)
    special_filter_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    min_account_days: Mapped[int] = mapped_column(Integer, default=0)
    min_completed_orders: Mapped[int] = mapped_column(Integer, default=0)
    max_total_orders: Mapped[int] = mapped_column(Integer, default=0)
    buy_auto_reply: Mapped[str] = mapped_column(Text, default="")
    sell_auto_reply: Mapped[str] = mapped_column(Text, default="")
    blacklist: Mapped[str] = mapped_column(Text, default="")
    friendly_list: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class OKXSlot(Base):
    __tablename__ = "okx_slots"
    __table_args__ = (UniqueConstraint("side", "slot_number", name="uq_okx_slot"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    side: Mapped[str] = mapped_column(String(10), index=True)
    slot_number: Mapped[int] = mapped_column(Integer)
    auto_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    current_price: Mapped[Decimal] = mapped_column(Numeric(20, 0), default=0)
    min_amount: Mapped[Decimal] = mapped_column(Numeric(20, 0), default=0)
    max_amount: Mapped[Decimal] = mapped_column(Numeric(20, 0), default=0)
    adv_id: Mapped[str] = mapped_column(String(150), default="")
    target_price: Mapped[Decimal] = mapped_column(Numeric(20, 0), default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class OKXAuditLog(Base):
    __tablename__ = "okx_audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    detail: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(100), default="dashboard")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class OKXMarketSnapshot(Base):
    __tablename__ = "okx_market_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    side: Mapped[str] = mapped_column(String(10), unique=True, index=True)
    offers_json: Mapped[str] = mapped_column(Text)
    page_url: Mapped[str] = mapped_column(Text, default="")
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ExchangeMarketSnapshot(Base):
    __tablename__ = "exchange_market_snapshots"
    __table_args__ = (UniqueConstraint("exchange", "side", name="uq_exchange_market_snapshot"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    exchange: Mapped[str] = mapped_column(String(20), index=True)
    side: Mapped[str] = mapped_column(String(10), index=True)
    offers_json: Mapped[str] = mapped_column(Text)
    page_url: Mapped[str] = mapped_column(Text, default="")
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ArbitrageConfig(Base):
    __tablename__ = "arbitrage_config"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    min_spread_vnd: Mapped[Decimal] = mapped_column(Numeric(20, 0), default=100)
    min_spread_percent: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=Decimal("0.20"))
    min_trade_vnd: Mapped[Decimal] = mapped_column(Numeric(20, 0), default=1000000)
    max_trade_vnd: Mapped[Decimal] = mapped_column(Numeric(20, 0), default=10000000)
    max_trade_usdt: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=Decimal("500"))
    target_trade_vnd: Mapped[Decimal] = mapped_column(Numeric(20, 0), default=0)
    min_available_usdt: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=0)
    allow_same_exchange: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class LockedP2PTrade(Base):
    __tablename__ = "locked_p2p_trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    exchange: Mapped[str] = mapped_column(String(20), index=True)
    side: Mapped[str] = mapped_column(String(10), index=True)
    price: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    amount_usdt: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    fixed_fee_vnd: Mapped[Decimal] = mapped_column(Numeric(20, 0), default=0)
    transfer_fee_usdt: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=0)
    note: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(20), default="OPEN", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint
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

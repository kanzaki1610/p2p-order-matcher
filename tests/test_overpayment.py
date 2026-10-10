from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.matching import evaluate, match_transaction, satisfies_payment_rules
from app.models import BankTransaction, P2POrder
from app.telegram import format_payment_notification


@pytest.mark.parametrize("amount,accepted", [("4999999", False), ("5000000", True),
    ("5000001", True), ("6000000", True)])
def test_received_amount_boundary(amount, accepted):
    order = P2POrder(side="SELL", order_code="TEST-5000000", counterparty_name="NGUYEN VAN TEST",
        fiat_amount=Decimal("5000000"))
    tx = BankTransaction(direction="CREDIT", amount=Decimal(amount), description="TEST-5000000")
    assert satisfies_payment_rules(order, tx) is accepted
    assert (evaluate(order, tx)[0] == 100) is accepted


def test_overpayment_does_not_replace_identity():
    order = P2POrder(side="SELL", order_code="TEST-5000000", counterparty_name="NGUYEN VAN TEST",
        fiat_amount=Decimal("5000000"))
    tx = BankTransaction(direction="CREDIT", amount=Decimal("5000001"), description="khong co dinh danh")
    assert not satisfies_payment_rules(order, tx)


def test_overpayment_matches_once_and_shows_excess():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        order = P2POrder(side="SELL", order_code="TEST-5000000", counterparty_name="NGUYEN VAN TEST",
            fiat_amount=Decimal("5000000"), created_at=now, status="WAITING_PAYMENT")
        tx = BankTransaction(bank="MB", transaction_id="TEST-PLUS-ONE", direction="CREDIT",
            amount=Decimal("5000001"), description="TEST-5000000", occurred_at=now)
        db.add_all([order, tx]); db.commit()
        decision, chosen, score, reasons = match_transaction(db, tx)
        assert decision == "AUTO_MATCHED" and chosen.id == order.id
        assert "dư 1 VND" in " ".join(reasons)
        assert "Số tiền đủ" in format_payment_notification(tx, score, reasons)
        another = P2POrder(side="SELL", order_code="TEST-SECOND", counterparty_name="NGUYEN VAN TEST",
            fiat_amount=Decimal("4000000"), created_at=now, status="WAITING_PAYMENT")
        db.add(another); db.commit()
        assert match_transaction(db, tx)[1].id == order.id
        assert another.status == "WAITING_PAYMENT"
    engine.dispose()


def test_one_overpayment_covers_two_same_buyer_orders_requires_review():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        orders = [P2POrder(side="SELL", order_code=code, counterparty_name="NGUYEN VAN TEST",
            fiat_amount=Decimal(amount), created_at=now, status="WAITING_PAYMENT")
            for code, amount in [("TEST-ONE", "5000000"), ("TEST-TWO", "4000000")]]
        tx = BankTransaction(bank="MB", transaction_id="TEST-AMBIGUOUS", direction="CREDIT",
            amount=Decimal("5000001"), description="NGUYEN VAN TEST", occurred_at=now)
        db.add_all([*orders, tx]); db.commit()
        decision, _, _, reasons = match_transaction(db, tx)
        assert decision == "REVIEW_REQUIRED"
        assert all(order.status == "WAITING_PAYMENT" for order in orders)
        message = " ".join(reasons)
        assert "TEST-ONE" in message and "TEST-TWO" in message
        assert "không vượt quá tiền nhận" in message
    engine.dispose()

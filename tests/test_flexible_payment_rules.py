from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.matching import match_transaction, memo_contains_order_code, same_name, satisfies_payment_rules
from app.models import BankTransaction, P2POrder
from app.okx_release import consistent


@pytest.mark.parametrize("memo", ["NGUYEN VAN BA", "BA VAN NGUYEN", "BA NGUYEN VAN chuyen tien",
    "Nguyễn, Văn, Ba", "261005183030445", "BA NGUYEN VAN 261005183030445", "TT261005183030445"])
def test_name_permutations_or_full_code(memo):
    order = P2POrder(side="SELL", order_code="261005183030445", counterparty_name="NGUYEN VAN BA", fiat_amount=Decimal("1000000"))
    tx = BankTransaction(direction="CREDIT", amount=Decimal("1000000"), description=memo)
    assert satisfies_payment_rules(order, tx)
    tx.amount = Decimal("999999")
    assert not satisfies_payment_rules(order, tx)


@pytest.mark.parametrize("memo", ["NGUYEN BA", "NGUYEN VAN BAN", "NGUYEN VAN BAO", "183030445", "12610051830304459"])
def test_partial_name_or_partial_code_not_accepted(memo):
    order = P2POrder(side="SELL", order_code="261005183030445", counterparty_name="NGUYEN VAN BA", fiat_amount=Decimal("1000000"))
    tx = BankTransaction(direction="CREDIT", amount=Decimal("1000000"), description=memo)
    assert not satisfies_payment_rules(order, tx)


def test_repeated_name_words_must_have_same_count():
    assert same_name("NGUYEN VAN BA", "BA NGUYEN VAN")
    assert not same_name("NGUYEN VAN VAN", "VAN NGUYEN")
    assert not memo_contains_order_code("999ABC123888", "ABC123")


def test_two_orders_same_name_and_amount_require_review():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        orders = [P2POrder(side="SELL", order_code=code, counterparty_name="NGUYEN VAN BA",
            fiat_amount=Decimal("1000000"), created_at=now, status="WAITING_PAYMENT") for code in ("CODE1", "CODE2")]
        tx = BankTransaction(bank="MB", transaction_id="FAKE", direction="CREDIT", amount=Decimal("1000000"),
            description="BA NGUYEN VAN", occurred_at=now)
        db.add_all([*orders, tx])
        db.commit()
        assert match_transaction(db, tx)[0] == "REVIEW_REQUIRED"
        assert all(order.status == "WAITING_PAYMENT" for order in orders)
    engine.dispose()


def test_release_identity_allows_name_reordering():
    order = P2POrder(order_code="TEST", fiat_amount=Decimal("1000000"), crypto_amount=Decimal("38"), counterparty_name="NGUYEN VAN BA")
    row = {"orderId": "TEST", "side": "sell", "isOwner": True, "cryptoCurrency": "USDT", "fiatCurrency": "VND",
        "fiatAmount": "1000000", "cryptoAmount": "38", "counterpartyDetail": {"realName": "BA VAN NGUYEN"}}
    assert consistent(order, row)

from decimal import Decimal

import pytest

from app.matching import evaluate, satisfies_payment_rules
from app.models import BankTransaction, P2POrder


def payment():
    return (
        P2POrder(side="SELL", order_code="123456789", counterparty_name="Đặng Văn An",
                 fiat_amount=Decimal("100000"), expected_bank="MB"),
        BankTransaction(direction="CREDIT", bank="MB", amount=Decimal("100000"),
                        description="dang van an chuyen tien"),
    )


def test_name_in_custom_memo_is_sufficient_with_amount_and_bank():
    order, tx = payment()
    assert satisfies_payment_rules(order, tx)
    assert evaluate(order, tx)[0] == 100


@pytest.mark.parametrize("field,value", [
    ("amount", Decimal("99999")),
    ("direction", "DEBIT"), ("description", "Chuyen tien"),
])
def test_missing_condition_blocks_automatic_match(field, value):
    order, tx = payment()
    setattr(tx, field, value)
    assert not satisfies_payment_rules(order, tx)
    assert evaluate(order, tx)[0] < 90


def test_bank_is_optional_but_buy_order_is_blocked():
    order, tx = payment()
    order.expected_bank = None
    assert satisfies_payment_rules(order, tx)
    order.expected_bank = "MB"
    order.side = "BUY"
    assert not satisfies_payment_rules(order, tx)


def test_exact_sender_name_can_replace_memo():
    order, tx = payment()
    tx.description = "Chuyen tien"
    tx.sender_name = order.counterparty_name
    assert satisfies_payment_rules(order, tx)

from decimal import Decimal

import pytest

from app.telegram_commands import normalize_amount, parse_create_order


def test_parse_create_order():
    code, amount, name, bank = parse_create_order("/don P2P003 | 1.000.000 | Nguyen Van A | mb")
    assert code == "P2P003"
    assert amount == Decimal("1000000")
    assert name == "Nguyen Van A"
    assert bank == "MB"


def test_parse_create_order_rejects_bank():
    with pytest.raises(ValueError):
        parse_create_order("/don P2P003 | 1000000 | Nguyen Van A | ACB")


def test_normalize_amount():
    assert normalize_amount("12,345") == Decimal("12345")

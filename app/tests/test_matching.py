from datetime import datetime, timezone
from decimal import Decimal

from app.matching import evaluate, normalize_text
from app.models import BankTransaction, P2POrder


def test_normalize_vietnamese_name():
    assert normalize_text("Nguyễn Văn A") == "NGUYENVANA"


def test_high_confidence_match():
    order = P2POrder(
        order_code="OKX-ABC123",
        fiat_amount=Decimal("52360000"),
        counterparty_name="Nguyễn Văn An",
        expected_bank="VIB",
    )
    tx = BankTransaction(
        bank="VIB",
        transaction_id="VIB001",
        amount=Decimal("52360000"),
        sender_name="NGUYEN VAN AN",
        description="Thanh toan OKX-ABC123",
        occurred_at=datetime.now(timezone.utc),
    )
    score, reasons = evaluate(order, tx)
    assert score == 100
    assert any("Số tiền" in reason for reason in reasons)


def test_sepay_match_without_sender_name_uses_order_code():
    order = P2POrder(
        order_code="P2P001",
        fiat_amount=Decimal("10000"),
        counterparty_name="Unknown Sender",
        expected_bank="MB",
    )
    tx = BankTransaction(
        bank="MB",
        transaction_id="FT-DEMO",
        amount=Decimal("10000"),
        sender_name=None,
        description="TESTP2P001 FT26264040336009",
        occurred_at=datetime.now(timezone.utc),
    )
    score, _ = evaluate(order, tx)
    assert score >= 90


def test_amount_only_is_not_auto_match():
    order = P2POrder(order_code="ORDER1", fiat_amount=Decimal("1000000"), counterparty_name="Tran Van B")
    tx = BankTransaction(
        bank="VPBANK",
        transaction_id="VP001",
        amount=Decimal("1000000"),
        sender_name="Le Thi C",
        description="chuyen tien",
        occurred_at=datetime.now(timezone.utc),
    )
    score, _ = evaluate(order, tx)
    assert score < 90

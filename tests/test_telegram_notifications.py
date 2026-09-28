from decimal import Decimal

from app.models import BankTransaction
from app.telegram import format_payment_notification


def test_payment_notification_contains_only_requested_fields():
    tx = BankTransaction(
        bank="VPBANK",
        transaction_id="FT123",
        amount=Decimal("2003"),
        direction="CREDIT",
        sender_name=None,
        description="DUONG DUC HUY chuyen 32345 | BankAPINotify DUONG DUC HUY chuyen 32345",
    )

    message = format_payment_notification(
        tx,
        score=80,
        reasons=[
            "Số tiền khớp chính xác +50",
            "Ngân hàng không trả tên người chuyển",
            "Tên người thanh toán có trong nội dung +25",
            "Đúng ngân hàng dự kiến +5",
        ],
    )

    assert message == (
        "Ngân hàng: VPBANK\n"
        "Số tiền: 2,003 VND\n"
        "Nội dung: DUONG DUC HUY chuyen 32345\n"
        "Điểm khớp: 80/100\n"
        "Lý do:\n"
        "Số tiền chính xác\n"
        "Họ tên chính xác"
    )
    assert "Mã GD" not in message
    assert "Hành động" not in message
    assert "BankAPINotify" not in message


def test_payment_notification_explicitly_marks_mismatches():
    tx = BankTransaction(
        bank="VPBANK",
        transaction_id="FT456",
        amount=Decimal("2100"),
        direction="CREDIT",
        sender_name=None,
        description="NGUYEN VAN A chuyen",
    )

    message = format_payment_notification(
        tx,
        score=5,
        reasons=["Đúng ngân hàng dự kiến +5"],
    )

    assert "Điểm khớp: 5/100" in message
    assert "Số tiền KHÔNG KHỚP" in message
    assert "Họ tên KHÔNG KHỚP" in message

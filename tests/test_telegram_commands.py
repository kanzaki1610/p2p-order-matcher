from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import OrderConfirmation, OrderRejection, P2POrder
from app.telegram_commands import (
    build_daily_report,
    format_vietnam_time,
    handle_command,
    normalize_amount,
    parse_create_order,
    payment_reference,
)


def test_parse_create_order():
    code, amount, name, bank = parse_create_order(
        "/don 260920160719335 | 1.000.000 | THI HONG THAM DAO | mb"
    )
    assert code == "260920160719335"
    assert amount == Decimal("1000000")
    assert name == "THI HONG THAM DAO"
    assert bank == "MB"


def test_parse_create_order_accepts_acb():
    code, amount, name, bank = parse_create_order(
        "/don 260920160719335 | 1000000 | Nguyen Van A | ACB"
    )
    assert code == "260920160719335"
    assert amount == Decimal("1000000")
    assert name == "Nguyen Van A"
    assert bank == "ACB"


def test_parse_create_order_rejects_unsupported_bank():
    with pytest.raises(ValueError):
        parse_create_order("/don 260920160719335 | 1000000 | Nguyen Van A | XYZBANK")


def test_payment_reference_uses_last_five_digits():
    assert payment_reference("260920160719335") == "19335"


def test_parse_create_order_requires_five_trailing_digits():
    with pytest.raises(ValueError):
        parse_create_order("/don P2P003 | 1000000 | Nguyen Van A | MB")


def test_normalize_amount():
    assert normalize_amount("12,345") == Decimal("12345")


def test_format_vietnam_time_from_utc():
    value = datetime(2026, 9, 20, 13, 12, 45, tzinfo=timezone.utc)
    assert format_vietnam_time(value) == "20:12:45 20/09/2026"


def test_format_vietnam_time_treats_naive_database_value_as_utc():
    value = datetime(2026, 9, 20, 13, 12, 45)
    assert format_vietnam_time(value) == "20:12:45 20/09/2026"


def test_confirm_payment_detected_order():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        order = P2POrder(
            order_code="CONFIRM001",
            fiat_amount=Decimal("12345"),
            counterparty_name="NGUYEN VAN A",
            expected_bank="MB",
            status="PAYMENT_DETECTED",
        )
        db.add(order)
        db.commit()

        reply = handle_command(
            "/xacnhan CONFIRM001",
            db,
            telegram_user_id="123456",
            telegram_username="operator_a",
            telegram_display_name="Operator A",
        )

        db.refresh(order)
        confirmation = db.query(OrderConfirmation).filter_by(order_id=order.id).one()
        assert order.status == "CONFIRMED"
        assert confirmation.telegram_user_id == "123456"
        assert "ĐÃ XÁC NHẬN THANH TOÁN" in reply


def test_reject_confirm_waiting_order():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        order = P2POrder(
            order_code="WAIT001",
            fiat_amount=Decimal("50000"),
            counterparty_name="NGUYEN VAN B",
            expected_bank="MB",
        )
        db.add(order)
        db.commit()

        reply = handle_command("/xacnhan WAIT001", db, telegram_user_id="123456")
        assert "Chỉ xác nhận đơn PAYMENT_DETECTED" in reply


def test_reject_payment_detected_order_with_reason():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        order = P2POrder(
            order_code="REJECT001",
            fiat_amount=Decimal("67890"),
            counterparty_name="NGUYEN VAN C",
            expected_bank="MB",
            status="PAYMENT_DETECTED",
        )
        db.add(order)
        db.commit()

        reply = handle_command(
            "/tuchoi REJECT001 | Giao dịch có dấu hiệu bất thường",
            db,
            telegram_user_id="654321",
            telegram_username="operator_b",
            telegram_display_name="Operator B",
        )

        db.refresh(order)
        rejection = db.query(OrderRejection).filter_by(order_id=order.id).one()
        assert order.status == "REJECTED"
        assert rejection.reason == "Giao dịch có dấu hiệu bất thường"
        assert rejection.telegram_username == "operator_b"
        assert "ĐÃ TỪ CHỐI GIAO DỊCH" in reply


def test_reject_command_requires_reason():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        reply = handle_command("/tuchoi REJECT001", db, telegram_user_id="654321")
        assert "Sai cú pháp" in reply


def test_cannot_reject_confirmed_order():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        order = P2POrder(
            order_code="CONFIRMED002",
            fiat_amount=Decimal("99999"),
            counterparty_name="NGUYEN VAN D",
            expected_bank="MB",
            status="CONFIRMED",
        )
        db.add(order)
        db.commit()

        reply = handle_command(
            "/tuchoi CONFIRMED002 | Thử từ chối đơn đã xác nhận",
            db,
            telegram_user_id="654321",
        )
        assert "trạng thái hiện tại: CONFIRMED" in reply


def test_daily_report_uses_vietnam_day_and_sums_confirmed_orders():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all(
            [
                P2POrder(
                    order_code="REPORT001",
                    fiat_amount=Decimal("100000"),
                    counterparty_name="BUYER A",
                    expected_bank="MB",
                    status="CONFIRMED",
                    created_at=datetime(2026, 9, 20, 2, 0, tzinfo=timezone.utc),
                ),
                P2POrder(
                    order_code="REPORT002",
                    fiat_amount=Decimal("200000"),
                    counterparty_name="BUYER B",
                    expected_bank="MB",
                    status="WAITING_PAYMENT",
                    created_at=datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc),
                ),
                P2POrder(
                    order_code="YESTERDAY001",
                    fiat_amount=Decimal("900000"),
                    counterparty_name="BUYER C",
                    expected_bank="MB",
                    status="CONFIRMED",
                    created_at=datetime(2026, 9, 19, 16, 59, tzinfo=timezone.utc),
                ),
            ]
        )
        db.commit()

        report = build_daily_report(
            db,
            now=datetime(2026, 9, 20, 13, 0, tzinfo=timezone.utc),
        )
        assert "BÁO CÁO P2P NGÀY 20/09/2026" in report
        assert "Tổng đơn tạo trong ngày: 2" in report
        assert "Chờ thanh toán: 1" in report
        assert "Đã xác nhận: 1" in report
        assert "Tổng tiền đã xác nhận: 100,000 VND" in report

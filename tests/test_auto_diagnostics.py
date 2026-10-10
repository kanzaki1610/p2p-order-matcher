import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.diagnostics import error_detail, safe_status
from app.matching import match_transaction
from app.models import BankTransaction, P2POrder
from app.notifications import notify_match


def test_one_credit_two_same_buyer_orders_lists_conflicts_without_releasing(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        orders = [P2POrder(side="SELL", order_code=code, counterparty_name=name,
            fiat_amount=Decimal("1000000"), created_at=now, status="WAITING_PAYMENT")
            for code, name in [("CODE1", "NGUYEN VAN BA"), ("CODE2", "BA VAN NGUYEN")]]
        tx = BankTransaction(bank="MB", transaction_id="TEST-ONE-CREDIT", direction="CREDIT",
            amount=Decimal("1000000"), description="NGUYEN VAN BA", occurred_at=now)
        db.add_all([*orders, tx]); db.commit()
        decision, order, score, reasons = match_transaction(db, tx)
        message = " ".join(reasons)
        assert decision == "REVIEW_REQUIRED"
        assert all(item.status == "WAITING_PAYMENT" for item in orders)
        for expected in ["2 lệnh", "cùng một người mua", "CODE1", "CODE2", "TEST-ONE-CREDIT",
                         "1,000,000", "Không dùng một giao dịch cho hai lệnh"]:
            assert expected in message
        notifier = AsyncMock(return_value=True)
        monkeypatch.setattr("app.notifications.notify_event", notifier)
        asyncio.run(notify_match(decision, order, tx, score, reasons))
        assert notifier.call_args.args[0] == "REVIEW_REQUIRED"
        assert "CODE2" in notifier.call_args.args[1]
    engine.dispose()


def test_different_buyers_full_code_still_resolves_one_order():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        orders = [P2POrder(side="SELL", order_code=code, counterparty_name=name,
            fiat_amount=Decimal("1000000"), created_at=now, status="WAITING_PAYMENT")
            for code, name in [("CODE1", "NGUYEN VAN BA"), ("CODE2", "TRAN THI LAN")]]
        tx = BankTransaction(bank="MB", transaction_id="TEST-UNIQUE", direction="CREDIT",
            amount=Decimal("1000000"), description="CODE1", occurred_at=now)
        db.add_all([*orders, tx]); db.commit()
        decision, order, _, reasons = match_transaction(db, tx)
        assert decision == "AUTO_MATCHED" and order.order_code == "CODE1"
        assert orders[1].status == "WAITING_PAYMENT"
        assert "CODE1" in " ".join(match_transaction(db, tx)[3])
    engine.dispose()


def test_http_error_reports_code_without_signed_url_or_api_message():
    response = httpx.Response(403, json={"code": 10072, "msg": "SECRET-KEY signed-url"},
        request=httpx.Request("GET", "https://example.com/?signature=SECRET-SIGNATURE"))
    cause = httpx.HTTPStatusError("SECRET-ERROR", request=response.request, response=response)
    error = RuntimeError("SECRET-WRAPPER"); error.__cause__ = cause
    message = error_detail(error)
    assert "403" in message and "10072" in message
    assert "SECRET" not in message and "https" not in message


@pytest.mark.parametrize("error,text", [(httpx.ReadTimeout("SECRET"), "Hết thời gian"),
    (httpx.ConnectError("SECRET"), "kết nối mạng"), (ValueError("SECRET"), "sai định dạng")])
def test_error_categories_are_specific_and_safe(error, text):
    assert text in error_detail(error)
    assert "SECRET" not in error_detail(error)


def test_status_does_not_echo_arbitrary_payload():
    assert safe_status("WAIT_PROCESS") == "WAIT_PROCESS"
    assert safe_status("https://example.com/?token=SECRET") == "thiếu/sai định dạng"


def test_okx_reader_error_preserves_only_numeric_code():
    assert "50110" in error_detail(RuntimeError("OKX API code 50110"))
    assert "403" in error_detail(RuntimeError("OKX HTTP 403"))
    assert "SECRET" not in error_detail(RuntimeError("OKX API code SECRET"))

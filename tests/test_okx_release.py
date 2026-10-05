import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import settings
from app.database import Base
from app.models import BankTransaction, P2POrder, PaymentMatch
from app.okx_release import OKXReleaseAttempt, OKXReleaseDiagnostic, process_releases
from app.okx_sync import OKXOrderReceipt


@pytest.fixture
def setup(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = Session(engine, expire_on_commit=False)
    order = P2POrder(order_code="TEST001", side="SELL", fiat_amount=Decimal("100000"),
        crypto_amount=Decimal("4"), counterparty_name="Đặng Văn An", status="PAYMENT_DETECTED")
    tx = BankTransaction(bank="MB", transaction_id="FAKE001", amount=Decimal("100000"),
        direction="CREDIT", description="DANG VAN AN chuyen tien", occurred_at=datetime.now(timezone.utc))
    db.add_all([order, tx, OKXOrderReceipt(order_id="TEST001")])
    db.commit()
    db.add(PaymentMatch(order_id=order.id, transaction_id=tx.id, score=100,
        decision="AUTO_MATCHED", reasons="Test"))
    db.commit()
    monkeypatch.setattr(settings, "okx_auto_release_enabled", True)
    monkeypatch.setattr(settings, "okx_api_key", "FAKE")
    monkeypatch.setattr(settings, "okx_api_secret", "FAKE")
    monkeypatch.setattr(settings, "okx_api_passphrase", "FAKE")
    monkeypatch.setattr("app.okx_release.notify_event", AsyncMock(return_value=True))
    row = {"orderId": "TEST001", "side": "sell", "isOwner": True,
        "fiatAmount": "100000", "cryptoAmount": "4", "cryptoCurrency": "USDT", "fiatCurrency": "VND",
        "orderStatus": "new", "paymentStatus": "paid", "isFrozen": False, "disputeStatus": "0",
        "counterpartyDetail": {"realName": "DANG VAN AN"}}
    yield db, order, row
    db.close()
    engine.dispose()


def run(db, handler):
    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await process_releases(db, client)
    asyncio.run(execute())


def test_disabled_flag_makes_no_api_calls(setup, monkeypatch):
    db, order, row = setup
    monkeypatch.setattr(settings, "okx_auto_release_enabled", False)
    def handler(request):
        pytest.fail("Disabled release must not call any API")
    run(db, handler)
    assert db.get(OKXReleaseAttempt, order.id) is None


def test_release_only_after_exchange_confirms_and_no_duplicate(setup):
    db, order, row = setup
    calls = []
    def handler(request):
        calls.append(request.method)
        if request.method == "POST":
            assert request.url.path == "/api/v5/p2p/order/release-crypto"
            assert json.loads(request.content) == {"orderId": "TEST001", "verificationType": "2", "amount": "100000"}
            row.update(orderStatus="completed", paymentStatus="confirmed")
            return httpx.Response(200, json={"code": "0", "data": [{"orderId": "TEST001"}]})
        return httpx.Response(200, json={"code": "0", "data": [row]})
    run(db, handler)
    run(db, handler)
    assert calls.count("POST") == 1
    assert order.status == "RELEASED"
    assert db.get(OKXReleaseAttempt, order.id).state == "RELEASED"


@pytest.mark.parametrize("field,value", [("isFrozen", True), ("disputeStatus", "1"),
    ("orderStatus", "cancelled"), ("paymentStatus", "unpaid"),
    ("fiatAmount", "99999"), ("cryptoAmount", "5")])
def test_unsafe_exchange_state_never_posts(setup, field, value):
    db, order, row = setup
    row[field] = value
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"code": "0", "data": [row]})
    run(db, handler)
    assert db.get(OKXReleaseAttempt, order.id).state == "REVIEW_REQUIRED"
    assert order.status != "RELEASED"


def test_timeout_is_not_automatically_resubmitted(setup):
    db, order, row = setup
    posts = []
    def handler(request):
        if request.method == "POST":
            posts.append(request)
            raise httpx.ReadTimeout("Simulated timeout")
        return httpx.Response(200, json={"code": "0", "data": [row]})
    run(db, handler)
    run(db, handler)
    assert len(posts) == 1
    assert db.get(OKXReleaseAttempt, order.id).state == "UNKNOWN"
    assert order.status == "PAYMENT_DETECTED"
    assert "Hết thời gian" in db.get(OKXReleaseDiagnostic, order.id).reason


@pytest.mark.parametrize("status,payload,expected", [
    (200, {"code": "50120", "msg": "FAKE_SECRET private response"}, "50120"),
    (403, {"code": "50101", "msg": "FAKE_SECRET"}, "HTTP 403"),
    (200, {"code": "FAKE_SECRET", "msg": "private"}, "không có mã hợp lệ"),
])
def test_release_failure_is_persisted_and_not_retried(setup, monkeypatch, status, payload, expected):
    db, order, row = setup
    notifier = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr("app.okx_release.notify_event", notifier)
    posts = []
    def handler(request):
        if request.method == "POST":
            posts.append(request)
            return httpx.Response(status, json=payload)
        return httpx.Response(200, json={"code": "0", "data": [row]})
    run(db, handler)
    db.expire_all()
    run(db, handler)
    assert len(posts) == 1
    assert db.get(OKXReleaseAttempt, order.id).state == "UNKNOWN"
    assert expected in db.get(OKXReleaseDiagnostic, order.id).reason
    for call in notifier.call_args_list:
        assert expected in call.args[1]
        assert "FAKE_SECRET" not in call.args[1]
        assert "private" not in call.args[1]


def test_api_acceptance_alone_does_not_mark_released(setup):
    db, order, row = setup
    def handler(request):
        data = {"orderId": "TEST001"} if request.method == "POST" else row
        return httpx.Response(200, json={"code": "0", "data": [data]})
    run(db, handler)
    assert db.get(OKXReleaseAttempt, order.id).state == "SUBMITTED"
    assert order.status != "RELEASED"


@pytest.mark.parametrize("ownership", [None, False])
def test_documented_order_detail_does_not_require_ad_ownership(setup, ownership):
    db, order, row = setup
    if ownership is None:
        row.pop("isOwner")
    else:
        row["isOwner"] = ownership
    calls = []
    def handler(request):
        calls.append(request.method)
        data = {"orderId": "TEST001"} if request.method == "POST" else row
        return httpx.Response(200, json={"code": "0", "data": [data]})
    run(db, handler)
    assert calls.count("POST") == 1


@pytest.mark.parametrize("crypto,fiat", [("usdt", "vnd"), ("UsDt", "VnD")])
def test_detail_currency_case_and_decimal_format_are_normalized(setup, crypto, fiat):
    db, order, row = setup
    row.update(cryptoCurrency=crypto, fiatCurrency=fiat, cryptoAmount="4.00000000")
    calls = []
    def handler(request):
        calls.append(request.method)
        if request.method == "POST":
            row.update(orderStatus="completed", paymentStatus="confirmed")
            data = {"orderId": "TEST001"}
        else:
            data = row
        return httpx.Response(200, json={"code": "0", "data": [data]})
    run(db, handler)
    run(db, handler)
    assert calls.count("POST") == 1
    assert order.status == "RELEASED"


@pytest.mark.parametrize("field,value", [("cryptoCurrency", "btc"), ("fiatCurrency", "usd"),
    ("cryptoCurrency", None), ("fiatCurrency", "")])
def test_currency_normalization_still_rejects_wrong_or_missing_currency(setup, field, value):
    db, order, row = setup
    row[field] = value
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"code": "0", "data": [row]})
    run(db, handler)
    assert db.get(OKXReleaseAttempt, order.id).state == "REVIEW_REQUIRED"


@pytest.mark.parametrize("field", ["isFrozen", "disputeStatus", "paymentStatus", "fiatAmount", "counterpartyDetail"])
def test_missing_required_detail_blocks_and_explains_without_post(setup, monkeypatch, field):
    db, order, row = setup
    row.pop(field)
    notifier = AsyncMock(return_value=True)
    monkeypatch.setattr("app.okx_release.notify_event", notifier)
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"code": "0", "data": [row]})
    run(db, handler)
    run(db, handler)
    assert db.get(OKXReleaseAttempt, order.id).state == "REVIEW_REQUIRED"
    assert "Lý do:" in notifier.call_args.args[1]
    assert notifier.call_count == 1

import asyncio
from unittest.mock import AsyncMock

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.okx_sync import OKXOrderReceipt, items, sync_orders


ROW = {"orderId": "OKX-TEST", "side": "sell", "orderStatus": "new",
       "paymentStatus": "paid", "cryptoCurrency": "USDT", "fiatCurrency": "VND",
       "fiatAmount": "100000", "cryptoAmount": "4", "createdTimestamp": "1780000000000",
       "counterpartyDetail": {"realName": "Test Buyer"}}


def test_wrapped_order_results():
    assert items([{"orders": [ROW]}]) == [ROW]
    assert items({"orderList": [ROW]}) == [ROW]


def test_persistent_notification_receipt_and_retry(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    reader = AsyncMock(return_value=[ROW])
    sender = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr("app.okx_sync.read_orders", reader)
    monkeypatch.setattr("app.okx_sync.notify_event", sender)
    with Session(engine, expire_on_commit=False) as db:
        assert asyncio.run(sync_orders(db, object())) == 0
        assert db.get(OKXOrderReceipt, ROW["orderId"]).notified_state is None
        assert asyncio.run(sync_orders(db, object())) == 1
    with Session(engine, expire_on_commit=False) as db:
        assert asyncio.run(sync_orders(db, object())) == 0
    assert sender.await_count == 2

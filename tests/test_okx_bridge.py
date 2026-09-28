from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.dashboard import BrowserBridgeIn, OfferIn, ingest_browser_bridge_offers, ingest_exchange_market_offers
from app.database import Base
from app.models import ExchangeMarketSnapshot, OKXMarketSnapshot


def bridge_payload(page_url: str = "https://www.okx.com/p2p-markets/vnd/buy-usdt") -> BrowserBridgeIn:
    return BrowserBridgeIn(
        side="BUY",
        offers=[
            OfferIn(
                nickname="Merchant A",
                price=Decimal("25700"),
                min_amount=Decimal("1000000"),
                max_amount=Decimal("200000000"),
                available_usdt=Decimal("750.5"),
            )
        ],
        page_url=page_url,
        captured_at=datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc),
    )


def test_browser_bridge_stores_read_only_snapshot_and_computes_slots():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        result = ingest_browser_bridge_offers(bridge_payload(), db, None)
        assert result["success"] is True
        assert result["mode"] == "READ_ONLY"
        assert result["live_action_performed"] is False
        assert result["snapshot"]["offer_count"] == 1
        assert len(result["snapshot"]["suggestions"]) == 2
        assert db.scalar(select(OKXMarketSnapshot)).side == "BUY"


def test_browser_bridge_rejects_non_okx_page_url():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db, pytest.raises(HTTPException) as error:
        ingest_browser_bridge_offers(bridge_payload("https://example.com/fake"), db, None)
    assert error.value.status_code == 422


def test_multi_exchange_bridge_accepts_official_binance_domain():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    payload = bridge_payload("https://p2p.binance.com/en/trade/buy/USDT?fiat=VND")
    payload.exchange = "BINANCE"
    with Session(engine) as db:
        result = ingest_exchange_market_offers(payload, db, None)
        assert result["success"] is True
        assert result["snapshot"]["exchange"] == "BINANCE"
        assert result["snapshot"]["offers"][0]["available_usdt"] == 750.5
        assert result["live_action_performed"] is False
        assert db.scalar(select(ExchangeMarketSnapshot)).side == "BUY"


def test_multi_exchange_bridge_rejects_mismatched_domain():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    payload = bridge_payload("https://p2p.binance.com/en/trade/buy/USDT")
    payload.exchange = "BITGET"
    with Session(engine) as db, pytest.raises(HTTPException) as error:
        ingest_exchange_market_offers(payload, db, None)
    assert error.value.status_code == 422


def test_multi_exchange_bridge_accepts_autop2p_source():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    payload = bridge_payload("https://autop2p.biz/")
    payload.exchange = "MEXC"
    payload.side = "SELL"
    with Session(engine) as db:
        result = ingest_exchange_market_offers(payload, db, None)
        assert result["success"] is True
        assert result["snapshot"]["exchange"] == "MEXC"
        assert result["snapshot"]["side"] == "SELL"
        assert result["snapshot"]["page_url"] == "https://autop2p.biz/"

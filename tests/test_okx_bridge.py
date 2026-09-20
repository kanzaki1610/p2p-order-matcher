from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.dashboard import BrowserBridgeIn, OfferIn, ingest_browser_bridge_offers
from app.database import Base
from app.models import OKXMarketSnapshot


def bridge_payload(page_url: str = "https://www.okx.com/p2p-markets/vnd/buy-usdt") -> BrowserBridgeIn:
    return BrowserBridgeIn(
        side="BUY",
        offers=[
            OfferIn(
                nickname="Merchant A",
                price=Decimal("25700"),
                min_amount=Decimal("1000000"),
                max_amount=Decimal("200000000"),
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

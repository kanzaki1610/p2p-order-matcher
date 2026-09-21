import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .bitget import BitgetAPIError, BitgetP2PClient, normalize_bitget_offers
from .config import settings
from .database import SessionLocal
from .models import ExchangeMarketSnapshot, OKXAuditLog


BITGET_AD_LIST_URL = "https://api.bitget.com/api/v3/p2p/ad-list"


def _upsert_snapshot(db: Session, side: str, offers: list[dict[str, Any]], captured_at: datetime) -> bool:
    serialized = json.dumps(offers, ensure_ascii=False, sort_keys=True)
    snapshot = db.scalar(
        select(ExchangeMarketSnapshot).where(
            ExchangeMarketSnapshot.exchange == "BITGET",
            ExchangeMarketSnapshot.side == side,
        )
    )
    changed = not snapshot or snapshot.offers_json != serialized
    if not snapshot:
        snapshot = ExchangeMarketSnapshot(
            exchange="BITGET",
            side=side,
            offers_json=serialized,
            page_url=BITGET_AD_LIST_URL,
            captured_at=captured_at,
        )
        db.add(snapshot)
    else:
        snapshot.offers_json = serialized
        snapshot.page_url = BITGET_AD_LIST_URL
        snapshot.captured_at = captured_at
        snapshot.received_at = datetime.now(timezone.utc)
    return changed


async def sync_bitget_p2p(db: Session, client: BitgetP2PClient | None = None) -> dict[str, Any]:
    client = client or BitgetP2PClient()
    if not settings.bitget_p2p_enabled:
        raise BitgetAPIError("BITGET_P2P_ENABLED đang tắt")
    if settings.bitget_p2p_live_writes:
        raise BitgetAPIError("Bản này chỉ hỗ trợ đọc; hãy đặt BITGET_P2P_LIVE_WRITES=false")

    currencies = await client.get_currencies()
    fiat_list = currencies.get("fiatDetailList", []) if isinstance(currencies, dict) else []
    vnd_supported = any(str(item.get("fiat", "")).upper() == "VND" for item in fiat_list)
    if not vnd_supported:
        raise BitgetAPIError("Tài khoản/API Bitget hiện không trả về thị trường VND")

    user_info, balance = await asyncio.gather(
        client.get_user_info(),
        client.get_balance("USDT"),
        return_exceptions=True,
    )
    now = datetime.now(timezone.utc)
    counts: dict[str, int] = {}
    changed_any = False
    for side in ("BUY", "SELL"):
        raw = await client.get_ad_list(
            dashboard_side=side,
            token="USDT",
            fiat="VND",
            limit=settings.bitget_p2p_ad_limit,
        )
        offers = normalize_bitget_offers(raw)
        counts[side] = len(offers)
        changed_any = _upsert_snapshot(db, side, offers, now) or changed_any

    if changed_any:
        db.add(
            OKXAuditLog(
                action="BITGET_API_SYNCED",
                detail=f"Bitget API nhận BUY {counts['BUY']} · SELL {counts['SELL']} quảng cáo USDT/VND",
                actor="bitget-api",
            )
        )
    db.commit()
    return {
        "success": True,
        "mode": "READ_ONLY",
        "live_action_performed": False,
        "vnd_supported": True,
        "account_level": str(user_info.get("accountLevel", "unknown")) if isinstance(user_info, dict) else "unavailable",
        "nickname": str(user_info.get("nickName", "")) if isinstance(user_info, dict) else "",
        "available_usdt": str(balance.get("availableBalance", "0")) if isinstance(balance, dict) else "unavailable",
        "offers": counts,
        "synced_at": now.isoformat(),
    }


async def bitget_sync_loop() -> None:
    interval = max(15, settings.bitget_p2p_sync_seconds)
    await asyncio.sleep(5)
    while True:
        try:
            with SessionLocal() as db:
                await sync_bitget_p2p(db)
        except Exception:
            # The manual sync endpoint exposes a safe error. The background worker keeps retrying
            # without logging credentials or stopping the payment matcher.
            pass
        await asyncio.sleep(interval)

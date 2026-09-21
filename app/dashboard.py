import json
import secrets
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from .bitget import BitgetAPIError
from .bitget_sync import sync_bitget_p2p
from .arbitrage import EXCHANGES, compare_locked_trade, find_opportunities
from .models import (
    ArbitrageConfig,
    ExchangeMarketSnapshot,
    LockedP2PTrade,
    OKXAuditLog,
    OKXDashboardConfig,
    OKXMarketSnapshot,
    OKXSlot,
)
from .okx_pricing import CompetitorOffer, parse_name_list, propose_price


router = APIRouter()
DASHBOARD_FILE = Path(__file__).with_name("dashboard.html")


class ConfigUpdate(BaseModel):
    dry_run: bool = True
    buy_target_min: Decimal = Field(default=0, ge=0)
    buy_target_max: Decimal = Field(default=0, ge=0)
    sell_target_min: Decimal = Field(default=0, ge=0)
    sell_target_max: Decimal = Field(default=0, ge=0)
    scan_buy_seconds: int = Field(default=10, ge=5, le=3600)
    scan_sell_seconds: int = Field(default=10, ge=5, le=3600)
    price_step: Decimal = Field(default=1, ge=1)
    special_filter_enabled: bool = True
    min_account_days: int = Field(default=0, ge=0)
    min_completed_orders: int = Field(default=0, ge=0)
    max_total_orders: int = Field(default=0, ge=0)
    buy_auto_reply: str = Field(default="", max_length=3000)
    sell_auto_reply: str = Field(default="", max_length=3000)
    blacklist: str = Field(default="", max_length=10000)
    friendly_list: str = Field(default="", max_length=10000)


class SlotUpdate(BaseModel):
    auto_enabled: bool = False
    current_price: Decimal = Field(default=0, ge=0)
    min_amount: Decimal = Field(default=0, ge=0)
    max_amount: Decimal = Field(default=0, ge=0)
    adv_id: str = Field(default="", max_length=150)
    target_price: Decimal = Field(default=0, ge=0)


class OfferIn(BaseModel):
    nickname: str = Field(min_length=1, max_length=100)
    price: Decimal = Field(gt=0)
    min_amount: Decimal = Field(default=0, ge=0)
    max_amount: Decimal = Field(default=0, ge=0)
    available_usdt: Decimal | None = Field(default=None, ge=0)
    account_days: int = Field(default=0, ge=0)
    completed_orders: int = Field(default=0, ge=0)
    total_orders: int = Field(default=0, ge=0)


class SimulationIn(BaseModel):
    side: Literal["BUY", "SELL"]
    slot_number: int = Field(default=1, ge=1, le=2)
    offers: list[OfferIn] = Field(min_length=1, max_length=100)


class BrowserBridgeIn(BaseModel):
    exchange: Literal["OKX", "BINANCE", "MEXC", "BITGET"] = "OKX"
    side: Literal["BUY", "SELL"]
    offers: list[OfferIn] = Field(min_length=1, max_length=100)
    page_url: str = Field(default="", max_length=1000)
    captured_at: datetime


class ArbitrageConfigUpdate(BaseModel):
    enabled: bool = True
    min_spread_vnd: Decimal = Field(default=100, ge=0)
    min_spread_percent: Decimal = Field(default=Decimal("0.20"), ge=0, le=100)
    min_trade_vnd: Decimal = Field(default=1000000, gt=0)
    max_trade_vnd: Decimal = Field(default=10000000, gt=0)
    max_trade_usdt: Decimal = Field(default=500, gt=0)
    target_trade_vnd: Decimal = Field(default=0, ge=0)
    min_available_usdt: Decimal = Field(default=0, ge=0)
    allow_same_exchange: bool = False


class LockedTradeCreate(BaseModel):
    exchange: Literal["OKX", "BINANCE", "MEXC", "BITGET"]
    side: Literal["BUY", "SELL"]
    price: Decimal = Field(gt=0)
    amount_usdt: Decimal = Field(gt=0)
    fixed_fee_vnd: Decimal = Field(default=0, ge=0)
    transfer_fee_usdt: Decimal = Field(default=0, ge=0)
    note: str = Field(default="", max_length=500)


def verify_dashboard_key(x_admin_key: str = Header(default="")) -> None:
    if not settings.dashboard_admin_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DASHBOARD_ADMIN_KEY chưa được cấu hình trên Render",
        )
    if not secrets.compare_digest(x_admin_key, settings.dashboard_admin_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Khóa quản trị không hợp lệ")


def verify_browser_bridge_key(x_bridge_key: str = Header(default="")) -> None:
    expected = settings.okx_browser_bridge_secret
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OKX_BROWSER_BRIDGE_SECRET chưa được cấu hình trên Render",
        )
    if not secrets.compare_digest(x_bridge_key, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bridge secret không hợp lệ")


def get_or_create_config(db: Session) -> OKXDashboardConfig:
    config = db.get(OKXDashboardConfig, 1)
    if not config:
        config = OKXDashboardConfig(id=1)
        db.add(config)
        db.commit()
        db.refresh(config)
    return config


def get_or_create_slots(db: Session) -> list[OKXSlot]:
    existing = db.scalars(select(OKXSlot).order_by(OKXSlot.side, OKXSlot.slot_number)).all()
    keys = {(slot.side, slot.slot_number) for slot in existing}
    for side in ("BUY", "SELL"):
        for number in (1, 2):
            if (side, number) not in keys:
                db.add(OKXSlot(side=side, slot_number=number))
    db.commit()
    return db.scalars(select(OKXSlot).order_by(OKXSlot.side, OKXSlot.slot_number)).all()


def get_or_create_arbitrage_config(db: Session) -> ArbitrageConfig:
    config = db.get(ArbitrageConfig, 1)
    if not config:
        config = ArbitrageConfig(id=1)
        db.add(config)
        db.commit()
        db.refresh(config)
    return config


def decimal_value(value: Decimal | None) -> int | float:
    if value is None:
        return 0
    return int(value) if value == value.to_integral() else float(value)


def config_dict(config: OKXDashboardConfig) -> dict:
    return {
        "dry_run": config.dry_run,
        "buy_target_min": decimal_value(config.buy_target_min),
        "buy_target_max": decimal_value(config.buy_target_max),
        "sell_target_min": decimal_value(config.sell_target_min),
        "sell_target_max": decimal_value(config.sell_target_max),
        "scan_buy_seconds": config.scan_buy_seconds,
        "scan_sell_seconds": config.scan_sell_seconds,
        "price_step": decimal_value(config.price_step),
        "special_filter_enabled": config.special_filter_enabled,
        "min_account_days": config.min_account_days,
        "min_completed_orders": config.min_completed_orders,
        "max_total_orders": config.max_total_orders,
        "buy_auto_reply": config.buy_auto_reply,
        "sell_auto_reply": config.sell_auto_reply,
        "blacklist": config.blacklist,
        "friendly_list": config.friendly_list,
    }


def slot_dict(slot: OKXSlot) -> dict:
    return {
        "side": slot.side,
        "slot_number": slot.slot_number,
        "auto_enabled": slot.auto_enabled,
        "current_price": decimal_value(slot.current_price),
        "min_amount": decimal_value(slot.min_amount),
        "max_amount": decimal_value(slot.max_amount),
        "adv_id": slot.adv_id,
        "target_price": decimal_value(slot.target_price),
    }


def add_audit(db: Session, action: str, detail: str, actor: str = "dashboard") -> None:
    db.add(OKXAuditLog(action=action, detail=detail, actor=actor))


def compute_proposal(config: OKXDashboardConfig, slot: OKXSlot, offers: list[CompetitorOffer]) -> dict:
    competitor_min_amount = config.buy_target_min if slot.side == "BUY" else config.sell_target_min
    competitor_max_amount = config.buy_target_max if slot.side == "BUY" else config.sell_target_max
    return propose_price(
        side=slot.side,
        offers=offers,
        price_step=config.price_step,
        competitor_min_amount=competitor_min_amount,
        competitor_max_amount=competitor_max_amount,
        price_limit=slot.target_price,
        blacklist=parse_name_list(config.blacklist),
        friendly_list=parse_name_list(config.friendly_list),
        special_filter_enabled=config.special_filter_enabled,
        min_account_days=config.min_account_days,
        min_completed_orders=config.min_completed_orders,
        max_total_orders=config.max_total_orders,
    )


def proposal_dict(result: dict, slot_number: int) -> dict:
    output = {**result, "slot_number": slot_number, "live_action_performed": False}
    if output["proposed_price"] is not None:
        output["proposed_price"] = decimal_value(output["proposed_price"])
    if output["competitor"]:
        for key in ("price", "min_amount", "max_amount"):
            output["competitor"][key] = decimal_value(output["competitor"][key])
    return output


def snapshot_dict(
    snapshot: OKXMarketSnapshot,
    config: OKXDashboardConfig,
    slots: list[OKXSlot],
) -> dict:
    raw_offers = json.loads(snapshot.offers_json)
    offers = [
        CompetitorOffer(
            nickname=item["nickname"],
            price=Decimal(str(item["price"])),
            min_amount=Decimal(str(item.get("min_amount", 0))),
            max_amount=Decimal(str(item.get("max_amount", 0))),
            account_days=int(item.get("account_days", 0)),
            completed_orders=int(item.get("completed_orders", 0)),
            total_orders=int(item.get("total_orders", 0)),
        )
        for item in raw_offers
    ]
    suggestions = [
        proposal_dict(compute_proposal(config, slot, offers), slot.slot_number)
        for slot in slots
        if slot.side == snapshot.side
    ]
    return {
        "side": snapshot.side,
        "offer_count": len(raw_offers),
        "offers": raw_offers,
        "page_url": snapshot.page_url,
        "captured_at": snapshot.captured_at.isoformat(),
        "received_at": snapshot.received_at.isoformat(),
        "suggestions": suggestions,
    }


def exchange_snapshot_dict(snapshot: ExchangeMarketSnapshot) -> dict:
    offers = json.loads(snapshot.offers_json)
    return {
        "exchange": snapshot.exchange,
        "side": snapshot.side,
        "offer_count": len(offers),
        "offers": offers,
        "page_url": snapshot.page_url,
        "captured_at": snapshot.captured_at.isoformat(),
        "received_at": snapshot.received_at.isoformat(),
    }


def arbitrage_config_dict(config: ArbitrageConfig) -> dict:
    return {
        "enabled": config.enabled,
        "min_spread_vnd": decimal_value(config.min_spread_vnd),
        "min_spread_percent": decimal_value(config.min_spread_percent),
        "min_trade_vnd": decimal_value(config.min_trade_vnd),
        "max_trade_vnd": decimal_value(config.max_trade_vnd),
        "max_trade_usdt": decimal_value(config.max_trade_usdt),
        "target_trade_vnd": decimal_value(config.target_trade_vnd),
        "min_available_usdt": decimal_value(config.min_available_usdt),
        "allow_same_exchange": config.allow_same_exchange,
    }


def opportunity_dict(item: dict) -> dict:
    output = dict(item)
    for key in (
        "buy_price",
        "sell_price",
        "spread_vnd",
        "spread_percent",
        "trade_vnd",
        "trade_usdt",
        "gross_profit_vnd",
        "buy_available_usdt",
        "sell_available_usdt",
    ):
        output[key] = decimal_value(output[key]) if output[key] is not None else None
    return output


def locked_trade_dict(trade: LockedP2PTrade) -> dict:
    return {
        "id": trade.id,
        "exchange": trade.exchange,
        "side": trade.side,
        "price": decimal_value(trade.price),
        "amount_usdt": decimal_value(trade.amount_usdt),
        "fixed_fee_vnd": decimal_value(trade.fixed_fee_vnd),
        "transfer_fee_usdt": decimal_value(trade.transfer_fee_usdt),
        "note": trade.note,
        "status": trade.status,
        "created_at": trade.created_at.isoformat(),
        "closed_at": trade.closed_at.isoformat() if trade.closed_at else None,
    }


def locked_comparison_dict(item: dict) -> dict:
    output = dict(item)
    for key in (
        "target_price",
        "amount_usdt",
        "target_value_vnd",
        "price_difference",
        "gross_difference_vnd",
        "estimated_net_vnd",
    ):
        output[key] = decimal_value(output[key])
    return output


def compute_arbitrage_state(db: Session) -> dict:
    config = get_or_create_arbitrage_config(db)
    snapshots = db.scalars(
        select(ExchangeMarketSnapshot).order_by(ExchangeMarketSnapshot.exchange, ExchangeMarketSnapshot.side)
    ).all()
    snapshot_data = [exchange_snapshot_dict(item) for item in snapshots]
    opportunities = find_opportunities(
        snapshot_data,
        min_spread_vnd=config.min_spread_vnd,
        min_spread_percent=config.min_spread_percent,
        min_trade_vnd=config.min_trade_vnd,
        max_trade_vnd=config.max_trade_vnd,
        max_trade_usdt=config.max_trade_usdt,
        allow_same_exchange=config.allow_same_exchange,
        target_trade_vnd=config.target_trade_vnd,
        min_available_usdt=config.min_available_usdt,
    ) if config.enabled else []
    locked_trades = db.scalars(
        select(LockedP2PTrade).order_by(LockedP2PTrade.id.desc()).limit(100)
    ).all()
    locked_results = []
    for trade in locked_trades:
        comparisons = compare_locked_trade(locked_trade_dict(trade), snapshot_data) if trade.status == "OPEN" else []
        locked_results.append(
            {
                "trade": locked_trade_dict(trade),
                "comparisons": [locked_comparison_dict(item) for item in comparisons[:20]],
                "best_comparison": locked_comparison_dict(comparisons[0]) if comparisons else None,
            }
        )
    return {
        "mode": "READ_ONLY_AUTO_PICK",
        "live_trading_enabled": False,
        "supported_exchanges": list(EXCHANGES),
        "config": arbitrage_config_dict(config),
        "snapshots": snapshot_data,
        "opportunities": [opportunity_dict(item) for item in opportunities[:20]],
        "best_opportunity": opportunity_dict(opportunities[0]) if opportunities else None,
        "locked_trades": locked_results,
    }


@router.get("/dashboard", include_in_schema=False)
def dashboard_page():
    return FileResponse(DASHBOARD_FILE)


@router.get("/dashboard/api/state")
def dashboard_state(
    db: Session = Depends(get_db),
    _: None = Depends(verify_dashboard_key),
):
    config = get_or_create_config(db)
    slots = get_or_create_slots(db)
    snapshots = db.scalars(select(OKXMarketSnapshot).order_by(OKXMarketSnapshot.side)).all()
    logs = db.scalars(select(OKXAuditLog).order_by(OKXAuditLog.id.desc()).limit(100)).all()
    return {
        "exchange": "OKX",
        "mode": "DRY_RUN",
        "live_trading_enabled": False,
        "api": {
            "base_url": settings.okx_base_url,
            "key_configured": bool(settings.okx_api_key),
            "secret_configured": bool(settings.okx_api_secret),
            "passphrase_configured": bool(settings.okx_api_passphrase),
            "p2p_connector": "NOT_CONNECTED",
        },
        "bitget": {
            "enabled": settings.bitget_p2p_enabled,
            "base_url": settings.bitget_p2p_base_url,
            "key_configured": bool(settings.bitget_p2p_api_key),
            "secret_configured": bool(settings.bitget_p2p_api_secret),
            "passphrase_configured": bool(settings.bitget_p2p_api_passphrase),
            "live_writes": False,
            "sync_seconds": max(15, settings.bitget_p2p_sync_seconds),
            "connector": "READ_ONLY" if settings.bitget_p2p_enabled else "DISABLED",
        },
        "bridge": {
            "configured": bool(settings.okx_browser_bridge_secret),
            "mode": "READ_ONLY",
            "snapshots": [snapshot_dict(snapshot, config, slots) for snapshot in snapshots],
        },
        "arbitrage": compute_arbitrage_state(db),
        "config": config_dict(config),
        "slots": [slot_dict(slot) for slot in slots],
        "history": [
            {
                "action": log.action,
                "detail": log.detail,
                "actor": log.actor,
                "created_at": log.created_at.isoformat(),
            }
            for log in logs
        ],
    }


@router.post("/dashboard/api/bitget/sync")
async def sync_bitget_market(
    db: Session = Depends(get_db),
    _: None = Depends(verify_dashboard_key),
):
    try:
        result = await sync_bitget_p2p(db)
    except BitgetAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    result["arbitrage"] = compute_arbitrage_state(db)
    return result


@router.put("/dashboard/api/arbitrage/config")
def update_arbitrage_config(
    payload: ArbitrageConfigUpdate,
    db: Session = Depends(get_db),
    _: None = Depends(verify_dashboard_key),
):
    if payload.min_trade_vnd > payload.max_trade_vnd:
        raise HTTPException(status_code=422, detail="Giới hạn VND tối thiểu không được lớn hơn tối đa")
    if payload.target_trade_vnd and not payload.min_trade_vnd <= payload.target_trade_vnd <= payload.max_trade_vnd:
        raise HTTPException(status_code=422, detail="Số tiền muốn giao dịch phải nằm trong giới hạn VND")
    config = get_or_create_arbitrage_config(db)
    for key, value in payload.model_dump().items():
        setattr(config, key, value)
    add_audit(
        db,
        "ARBITRAGE_CONFIG_UPDATED",
        "Đã cập nhật giới hạn auto pick 4 sàn; giao dịch LIVE vẫn khóa",
    )
    db.commit()
    db.refresh(config)
    return {"success": True, "config": arbitrage_config_dict(config), "live_trading_enabled": False}


@router.post("/dashboard/api/locked-trades")
def create_locked_trade(
    payload: LockedTradeCreate,
    db: Session = Depends(get_db),
    _: None = Depends(verify_dashboard_key),
):
    trade = LockedP2PTrade(**payload.model_dump(), status="OPEN")
    db.add(trade)
    add_audit(
        db,
        "PRICE_LOCKED",
        f"Đã chốt {payload.side} {payload.amount_usdt} USDT tại {payload.exchange}, giá {payload.price} VND",
    )
    db.commit()
    db.refresh(trade)
    return {
        "success": True,
        "trade": locked_trade_dict(trade),
        "arbitrage": compute_arbitrage_state(db),
        "live_action_performed": False,
    }


@router.post("/dashboard/api/locked-trades/{trade_id}/close")
def close_locked_trade(
    trade_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(verify_dashboard_key),
):
    trade = db.get(LockedP2PTrade, trade_id)
    if not trade:
        raise HTTPException(status_code=404, detail="Không tìm thấy giá đã chốt")
    if trade.status != "CLOSED":
        trade.status = "CLOSED"
        trade.closed_at = datetime.now(timezone.utc)
        add_audit(db, "PRICE_LOCK_CLOSED", f"Đã đóng theo dõi giá chốt #{trade.id}")
        db.commit()
        db.refresh(trade)
    return {"success": True, "trade": locked_trade_dict(trade)}


@router.put("/dashboard/api/config")
def update_config(
    payload: ConfigUpdate,
    db: Session = Depends(get_db),
    _: None = Depends(verify_dashboard_key),
):
    config = get_or_create_config(db)
    data = payload.model_dump()
    if data["buy_target_min"] and data["buy_target_max"] and data["buy_target_min"] > data["buy_target_max"]:
        raise HTTPException(status_code=422, detail="Target BUY tối thiểu không được lớn hơn tối đa")
    if data["sell_target_min"] and data["sell_target_max"] and data["sell_target_min"] > data["sell_target_max"]:
        raise HTTPException(status_code=422, detail="Target SELL tối thiểu không được lớn hơn tối đa")
    # LIVE mode is intentionally disabled until an authorized OKX P2P connector exists.
    data["dry_run"] = True
    changed = any(getattr(config, key) != value for key, value in data.items())
    for key, value in data.items():
        setattr(config, key, value)
    if changed:
        add_audit(db, "CONFIG_UPDATED", "Đã cập nhật cấu hình OKX DRY RUN")
    db.commit()
    db.refresh(config)
    return {"success": True, "changed": changed, "config": config_dict(config)}


@router.put("/dashboard/api/slots/{side}/{slot_number}")
def update_slot(
    side: str,
    slot_number: int,
    payload: SlotUpdate,
    db: Session = Depends(get_db),
    _: None = Depends(verify_dashboard_key),
):
    side = side.upper()
    if side not in {"BUY", "SELL"} or slot_number not in {1, 2}:
        raise HTTPException(status_code=422, detail="Slot không hợp lệ")
    if payload.min_amount and payload.max_amount and payload.min_amount > payload.max_amount:
        raise HTTPException(status_code=422, detail="Min giao dịch không được lớn hơn Max giao dịch")
    get_or_create_slots(db)
    slot = db.scalar(select(OKXSlot).where(OKXSlot.side == side, OKXSlot.slot_number == slot_number))
    for key, value in payload.model_dump().items():
        setattr(slot, key, value)
    add_audit(db, "SLOT_UPDATED", f"Đã lưu {side} Slot {slot_number} ở chế độ DRY RUN")
    db.commit()
    db.refresh(slot)
    return {"success": True, "slot": slot_dict(slot)}


@router.post("/dashboard/api/bridge/offers")
def ingest_browser_bridge_offers(
    payload: BrowserBridgeIn,
    db: Session = Depends(get_db),
    _: None = Depends(verify_browser_bridge_key),
):
    # This endpoint only accepts public ad fields visible on the P2P page.
    # It never receives cookies, session tokens, account data, or trade actions.
    if payload.page_url and not (
        payload.page_url.startswith("https://okx.com/")
        or payload.page_url.startswith("https://www.okx.com/")
        or (payload.page_url.startswith("https://") and ".okx.com/" in payload.page_url)
    ):
        raise HTTPException(status_code=422, detail="Bridge chỉ nhận dữ liệu từ trang HTTPS của OKX")
    offers_data = [
        {
            "nickname": offer.nickname,
            "price": decimal_value(offer.price),
            "min_amount": decimal_value(offer.min_amount),
            "max_amount": decimal_value(offer.max_amount),
            "available_usdt": decimal_value(offer.available_usdt) if offer.available_usdt is not None else None,
            "account_days": offer.account_days,
            "completed_orders": offer.completed_orders,
            "total_orders": offer.total_orders,
        }
        for offer in payload.offers
    ]
    offers_json = json.dumps(offers_data, ensure_ascii=False, sort_keys=True)
    snapshot = db.scalar(select(OKXMarketSnapshot).where(OKXMarketSnapshot.side == payload.side))
    changed = not snapshot or snapshot.offers_json != offers_json
    if not snapshot:
        snapshot = OKXMarketSnapshot(
            side=payload.side,
            offers_json=offers_json,
            page_url=payload.page_url,
            captured_at=payload.captured_at,
        )
        db.add(snapshot)
    else:
        snapshot.offers_json = offers_json
        snapshot.page_url = payload.page_url
        snapshot.captured_at = payload.captured_at
        snapshot.received_at = datetime.now(timezone.utc)

    if changed:
        best_price = max(offer.price for offer in payload.offers) if payload.side == "BUY" else min(
            offer.price for offer in payload.offers
        )
        add_audit(
            db,
            "MARKET_SCANNED",
            f"Extension nhận {len(payload.offers)} quảng cáo {payload.side}; giá tham chiếu {best_price:,.0f} VND",
            actor="okx-extension",
        )
    db.commit()
    db.refresh(snapshot)

    # Mirror legacy OKX snapshots into the multi-exchange comparison table.
    store_exchange_snapshot(db, payload)

    config = get_or_create_config(db)
    slots = get_or_create_slots(db)
    return {
        "success": True,
        "changed": changed,
        "mode": "READ_ONLY",
        "live_action_performed": False,
        "snapshot": snapshot_dict(snapshot, config, slots),
    }


EXCHANGE_HOSTS = {
    "OKX": ("okx.com",),
    "BINANCE": ("binance.com",),
    "MEXC": ("mexc.com",),
    "BITGET": ("bitget.com",),
}


def valid_exchange_page_url(exchange: str, page_url: str) -> bool:
    if not page_url:
        return True
    try:
        from urllib.parse import urlparse

        parsed = urlparse(page_url)
        hostname = (parsed.hostname or "").lower()
        return parsed.scheme == "https" and any(
            hostname == domain or hostname.endswith(f".{domain}")
            for domain in EXCHANGE_HOSTS[exchange]
        )
    except (KeyError, ValueError):
        return False


def offer_data(payload: BrowserBridgeIn) -> list[dict]:
    return [
        {
            "nickname": offer.nickname,
            "price": decimal_value(offer.price),
            "min_amount": decimal_value(offer.min_amount),
            "max_amount": decimal_value(offer.max_amount),
            "available_usdt": decimal_value(offer.available_usdt) if offer.available_usdt is not None else None,
            "account_days": offer.account_days,
            "completed_orders": offer.completed_orders,
            "total_orders": offer.total_orders,
        }
        for offer in payload.offers
    ]


def store_exchange_snapshot(db: Session, payload: BrowserBridgeIn) -> tuple[ExchangeMarketSnapshot, bool]:
    data = offer_data(payload)
    serialized = json.dumps(data, ensure_ascii=False, sort_keys=True)
    snapshot = db.scalar(
        select(ExchangeMarketSnapshot).where(
            ExchangeMarketSnapshot.exchange == payload.exchange,
            ExchangeMarketSnapshot.side == payload.side,
        )
    )
    changed = not snapshot or snapshot.offers_json != serialized
    if not snapshot:
        snapshot = ExchangeMarketSnapshot(
            exchange=payload.exchange,
            side=payload.side,
            offers_json=serialized,
            page_url=payload.page_url,
            captured_at=payload.captured_at,
        )
        db.add(snapshot)
    else:
        snapshot.offers_json = serialized
        snapshot.page_url = payload.page_url
        snapshot.captured_at = payload.captured_at
        snapshot.received_at = datetime.now(timezone.utc)
    if changed:
        add_audit(
            db,
            "MARKET_SCANNED",
            f"Extension nhận {len(data)} quảng cáo {payload.side} từ {payload.exchange}",
            actor="multi-exchange-extension",
        )
    db.commit()
    db.refresh(snapshot)
    return snapshot, changed


@router.post("/dashboard/api/market/offers")
def ingest_exchange_market_offers(
    payload: BrowserBridgeIn,
    db: Session = Depends(get_db),
    _: None = Depends(verify_browser_bridge_key),
):
    if not valid_exchange_page_url(payload.exchange, payload.page_url):
        raise HTTPException(
            status_code=422,
            detail=f"URL không thuộc tên miền chính thức của {payload.exchange}",
        )
    snapshot, changed = store_exchange_snapshot(db, payload)
    return {
        "success": True,
        "changed": changed,
        "mode": "READ_ONLY_AUTO_PICK",
        "live_action_performed": False,
        "snapshot": exchange_snapshot_dict(snapshot),
        "arbitrage": compute_arbitrage_state(db),
    }


@router.post("/dashboard/api/simulate")
def simulate_pricing(
    payload: SimulationIn,
    db: Session = Depends(get_db),
    _: None = Depends(verify_dashboard_key),
):
    config = get_or_create_config(db)
    get_or_create_slots(db)
    slot = db.scalar(
        select(OKXSlot).where(OKXSlot.side == payload.side, OKXSlot.slot_number == payload.slot_number)
    )
    result = compute_proposal(
        config,
        slot,
        [CompetitorOffer(**offer.model_dump()) for offer in payload.offers],
    )
    if result["proposed_price"] is not None:
        slot.current_price = result["proposed_price"]
    add_audit(
        db,
        "PRICE_SIMULATED",
        f"{payload.side} Slot {payload.slot_number}: {result['reason']}",
    )
    db.commit()
    result = proposal_dict(result, payload.slot_number)
    result["simulated_at"] = datetime.now(timezone.utc).isoformat()
    return result

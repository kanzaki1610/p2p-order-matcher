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
from .models import OKXAuditLog, OKXDashboardConfig, OKXMarketSnapshot, OKXSlot
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
    account_days: int = Field(default=0, ge=0)
    completed_orders: int = Field(default=0, ge=0)
    total_orders: int = Field(default=0, ge=0)


class SimulationIn(BaseModel):
    side: Literal["BUY", "SELL"]
    slot_number: int = Field(default=1, ge=1, le=2)
    offers: list[OfferIn] = Field(min_length=1, max_length=100)


class BrowserBridgeIn(BaseModel):
    side: Literal["BUY", "SELL"]
    offers: list[OfferIn] = Field(min_length=1, max_length=100)
    page_url: str = Field(default="", max_length=1000)
    captured_at: datetime


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
        "bridge": {
            "configured": bool(settings.okx_browser_bridge_secret),
            "mode": "READ_ONLY",
            "snapshots": [snapshot_dict(snapshot, config, slots) for snapshot in snapshots],
        },
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

    config = get_or_create_config(db)
    slots = get_or_create_slots(db)
    return {
        "success": True,
        "changed": changed,
        "mode": "READ_ONLY",
        "live_action_performed": False,
        "snapshot": snapshot_dict(snapshot, config, slots),
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

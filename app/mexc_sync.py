import asyncio
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .database import SessionLocal
from .matching import match_transaction
from .mexc import MexcAPIError, MexcP2PClient
from .models import BankTransaction, OKXAuditLog, P2POrder
from .telegram import notify_match


OPEN_STATES = {"NOT_PAID", "PAID", "WAIT_PROCESS", "PROCESSING"}
RECONCILABLE_STATES = OPEN_STATES | {"DONE"}
CANCELLED_STATES = {"CANCEL", "CANCELLED", "INVALID", "REFUSE", "TIMEOUT"}
QUERY_STATES = ("NOT_PAID", "PAID", "WAIT_PROCESS", "PROCESSING", "DONE")


def _items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("items", "list", "records", "rows", "result", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _milliseconds(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _bank_name(detail: dict[str, Any]) -> str | None:
    selected = detail.get("confirmPaymentInfo")
    if not isinstance(selected, dict):
        payments = detail.get("paymentInfo") or []
        selected = payments[0] if payments and isinstance(payments[0], dict) else {}
    name = str(selected.get("bankName") or "").strip().upper().replace(" ", "")
    return name or None


def _last_five(order_code: str) -> str | None:
    digits = re.sub(r"\D", "", order_code)
    return digits[-5:] if len(digits) >= 5 else None


def _decimal(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _upsert_order(db: Session, detail: dict[str, Any]) -> tuple[P2POrder, bool]:
    order_code = str(detail.get("advOrderNo") or "").strip()
    if not order_code:
        raise MexcAPIError("Chi tiết lệnh MEXC thiếu advOrderNo")
    order = db.scalar(select(P2POrder).where(P2POrder.order_code == order_code))
    created = order is None
    user_info = detail.get("userInfo") if isinstance(detail.get("userInfo"), dict) else {}
    counterparty = str(
        user_info.get("realName")
        or user_info.get("nickName")
        or user_info.get("account")
        or "Không có dữ liệu"
    ).strip()
    api_state = str(detail.get("state") or "NOT_PAID").upper()
    if created:
        order = P2POrder(
            order_code=order_code,
            side=str(detail.get("side") or "SELL").upper(),
            fiat_amount=_decimal(detail.get("amount")),
            crypto_amount=_decimal(detail.get("tradableQuantity")),
            counterparty_name=counterparty,
            expected_bank=_bank_name(detail),
            payment_note=_last_five(order_code),
            # DONE can be discovered after SePay has already delivered the bank
            # transaction. Keep it eligible for read-only reconciliation.
            status="WAITING_PAYMENT" if api_state in RECONCILABLE_STATES else "CANCELLED",
            created_at=_milliseconds(detail.get("createTime")) or datetime.now(timezone.utc),
            expires_at=_milliseconds(detail.get("payTimeLimit")),
        )
        db.add(order)
    else:
        order.side = str(detail.get("side") or order.side).upper()
        order.fiat_amount = _decimal(detail.get("amount"), str(order.fiat_amount))
        order.crypto_amount = _decimal(detail.get("tradableQuantity"), str(order.crypto_amount or 0))
        order.counterparty_name = counterparty
        order.expected_bank = _bank_name(detail) or order.expected_bank
        order.payment_note = _last_five(order_code) or order.payment_note
        order.expires_at = _milliseconds(detail.get("payTimeLimit")) or order.expires_at
        if order.status == "WAITING_PAYMENT" and api_state in CANCELLED_STATES:
            order.status = "CANCELLED"
    return order, created


async def sync_mexc_orders(db: Session, client: MexcP2PClient | None = None) -> dict[str, Any]:
    if not settings.mexc_p2p_enabled:
        raise MexcAPIError("MEXC_P2P_ENABLED đang tắt")
    if settings.mexc_p2p_live_writes:
        raise MexcAPIError("Bản này chỉ hỗ trợ đọc; hãy đặt MEXC_P2P_LIVE_WRITES=false")
    client = client or MexcP2PClient()
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=max(30, settings.mexc_p2p_lookback_minutes))
    # MEXC accepts one order state per query. A comma-separated value can
    # succeed with an empty result, which caused completed orders to be missed.
    summaries_by_code: dict[str, dict[str, Any]] = {}
    for state in QUERY_STATES:
        raw = await client.get_orders(
            start_time=int(start.timestamp() * 1000),
            end_time=int(now.timestamp() * 1000),
            side=settings.mexc_p2p_incoming_side.upper(),
            states=state,
            limit=settings.mexc_p2p_order_limit,
        )
        for summary in _items(raw):
            code = str(summary.get("advOrderNo") or "").strip()
            if code:
                summaries_by_code[code] = summary
    summaries = list(summaries_by_code.values())
    created_count = 0
    updated_count = 0
    reconciled_count = 0
    for summary in summaries:
        code = str(summary.get("advOrderNo") or "").strip()
        if not code:
            continue
        detail = await client.get_order_detail(code)
        if str(detail.get("coinName") or "USDT").upper() != "USDT":
            continue
        if str(detail.get("fiatUnit") or "VND").upper() != "VND":
            continue
        order, created = _upsert_order(db, detail)
        created_count += int(created)
        updated_count += int(not created)
        if created and order.status == "WAITING_PAYMENT":
            unmatched_transactions = db.scalars(
                select(BankTransaction).where(
                    BankTransaction.status == "UNMATCHED",
                    BankTransaction.amount == order.fiat_amount,
                    BankTransaction.occurred_at >= start,
                    BankTransaction.occurred_at <= now + timedelta(minutes=15),
                )
            ).all()
            for tx in unmatched_transactions:
                decision, matched_order, score, reasons = match_transaction(db, tx)
                if matched_order is not None:
                    reconciled_count += 1
                    await notify_match(decision, matched_order, tx, score, reasons)
    if summaries:
        db.add(
            OKXAuditLog(
                action="MEXC_ORDERS_SYNCED",
                detail=(
                    f"MEXC P2P nhận {len(summaries)} lệnh · mới {created_count} · "
                    f"cập nhật {updated_count} · đối soát lại {reconciled_count}"
                ),
                actor="mexc-p2p-api",
            )
        )
    db.commit()
    return {
        "success": True,
        "mode": "READ_ONLY",
        "live_action_performed": False,
        "orders_received": len(summaries),
        "orders_created": created_count,
        "orders_updated": updated_count,
        "transactions_reconciled": reconciled_count,
        "synced_at": now.isoformat(),
    }


async def mexc_sync_loop() -> None:
    interval = max(10, settings.mexc_p2p_sync_seconds)
    await asyncio.sleep(3)
    while True:
        try:
            with SessionLocal() as db:
                await sync_mexc_orders(db)
        except Exception:
            pass
        await asyncio.sleep(interval)

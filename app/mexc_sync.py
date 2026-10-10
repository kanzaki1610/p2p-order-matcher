import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .database import SessionLocal
from .matching import match_transaction
from .mexc import MexcAPIError, MexcP2PClient
from .models import BankTransaction, OKXAuditLog, P2POrder, MexcNotification
from .notifications import notify_match, notify_mexc_new_order, notify_system_alert
from .mexc_release import MexcOrderReceipt, process_mexc_releases


logger = logging.getLogger("uvicorn.error.mexc_sync")

OPEN_STATES = {"NOT_PAID", "PAID", "WAIT_PROCESS", "PROCESSING"}
RECONCILABLE_STATES = OPEN_STATES | {"DONE"}
CANCELLED_STATES = {"CANCEL", "CANCELLED", "INVALID", "REFUSE", "TIMEOUT"}
QUERY_STATES = "NOT_PAID,PAID,WAIT_PROCESS,PROCESSING,DONE"

# Total attempts for MEXC order detail: 3.
# Retry delays are 1s then 2s.
MEXC_DETAIL_MAX_ATTEMPTS = 3


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


def _is_transient_mexc_error(exc: BaseException) -> bool:
    """Retry only temporary network/timeout errors."""
    current: BaseException | None = exc
    seen: set[int] = set()

    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (httpx.TimeoutException, httpx.TransportError)):
            return True
        current = current.__cause__ or current.__context__

    return False


async def _get_order_detail_with_retry(
    client: MexcP2PClient,
    order_code: str,
    *,
    max_attempts: int = MEXC_DETAIL_MAX_ATTEMPTS,
) -> dict[str, Any]:
    attempts = max(1, int(max_attempts))

    for attempt in range(1, attempts + 1):
        try:
            detail = await client.get_order_detail(order_code)
            if attempt > 1:
                logger.info(
                    "MEXC order detail recovered | order=%s | attempt=%s/%s",
                    order_code,
                    attempt,
                    attempts,
                )
            return detail
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            transient = _is_transient_mexc_error(exc)

            if not transient or attempt >= attempts:
                logger.error(
                    "MEXC order detail failed | order=%s | attempt=%s/%s | transient=%s | error=%s",
                    order_code,
                    attempt,
                    attempts,
                    transient,
                    str(exc)[:300],
                )
                raise

            delay = 2 ** (attempt - 1)  # 1s, 2s
            logger.warning(
                "MEXC order detail retry | order=%s | attempt=%s/%s | wait=%ss | error=%s",
                order_code,
                attempt,
                attempts,
                delay,
                str(exc)[:300],
            )
            await asyncio.sleep(delay)

    raise MexcAPIError(f"Không thể lấy chi tiết lệnh MEXC {order_code}")


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
            status="WAITING_PAYMENT" if api_state in RECONCILABLE_STATES else "CANCELLED",
            created_at=_milliseconds(detail.get("createTime")) or datetime.now(timezone.utc),
            expires_at=_milliseconds(detail.get("payTimeLimit")),
        )
        db.add(order)
    elif order.status == "WAITING_PAYMENT":
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
    if client is None:
        async with MexcP2PClient() as owned_client:
            return await sync_mexc_orders(db, owned_client)
    if not settings.mexc_p2p_enabled:
        raise MexcAPIError("MEXC_P2P_ENABLED đang tắt")

    client = client or MexcP2PClient()
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=max(30, settings.mexc_p2p_lookback_minutes))

    raw = await client.get_orders(
        start_time=int(start.timestamp() * 1000),
        end_time=int(now.timestamp() * 1000),
        states=QUERY_STATES,
        limit=settings.mexc_p2p_order_limit,
        maker_view=True,
    )
    summaries = _items(raw)
    source_endpoint = "MAKER_VIEW"

    if not summaries:
        raw = await client.get_orders(
            start_time=int(start.timestamp() * 1000),
            end_time=int(now.timestamp() * 1000),
            limit=settings.mexc_p2p_order_limit,
            maker_view=False,
        )
        summaries = _items(raw)
        source_endpoint = "ALL_ORDERS_FALLBACK"

    created_count = 0
    updated_count = 0
    reconciled_count = 0
    telegram_new_order_count = 0
    detail_failed_count = 0
    skipped_filters = {"coin": 0, "fiat": 0, "side_buy": 0, "side_sell": 0, "side_missing": 0, "side_other": 0}
    expected_api_side = (settings.mexc_p2p_api_incoming_side or settings.mexc_p2p_incoming_side).upper()

    for summary in summaries:
        code = str(summary.get("advOrderNo") or "").strip()
        if not code:
            continue

        # A timeout on one order must not abort the entire sync batch.
        try:
            detail = await _get_order_detail_with_retry(
                client, code, max_attempts=1 if isinstance(client, MexcP2PClient) else MEXC_DETAIL_MAX_ATTEMPTS,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            detail_failed_count += 1
            logger.error("MEXC order skipped after detail failure | order=%s", code)
            continue

        if str(detail.get("coinName") or "USDT").upper() != "USDT":
            skipped_filters["coin"] += 1
            continue
        if str(detail.get("fiatUnit") or "VND").upper() != "VND":
            skipped_filters["fiat"] += 1
            continue
        if str(detail.get("side") or "").upper() != expected_api_side:
            side = str(detail.get("side") or "").upper()
            reason = {"BUY": "side_buy", "SELL": "side_sell", "": "side_missing"}.get(side, "side_other")
            skipped_filters[reason] += 1
            continue

        # The API-side convention is configured independently of our accounting side.
        from .okx_sync import OKXOrderReceipt
        if db.get(OKXOrderReceipt, code) is not None:
            continue
        receipt = db.get(MexcOrderReceipt, code)
        if receipt is None:
            db.add(MexcOrderReceipt(order_code=code, api_side=str(detail.get("side"))))
        elif receipt.api_side != str(detail.get("side")):
            continue
        detail = {**detail, "side": settings.mexc_p2p_incoming_side.upper()}
        order, created = _upsert_order(db, detail)
        created_count += int(created)
        updated_count += int(not created)

        if created:
            db.flush()
            if str(detail.get("state") or "NOT_PAID").upper() in OPEN_STATES:
                db.add(MexcNotification(order_code=order.order_code))
            # Persist pending delivery before contacting the notification provider.
            db.commit()
        pending = db.get(MexcNotification, order.order_code)
        if pending is not None and pending.sent_at is None:
            if order.status == "WAITING_PAYMENT" and str(detail.get("state") or "NOT_PAID").upper() in OPEN_STATES:
                sent = await notify_mexc_new_order(order)
                telegram_new_order_count += int(sent)
                if sent:
                    pending.sent_at = datetime.now(timezone.utc)
                    db.commit()
                    logger.info(
                        "MEXC new order notification sent | order=%s | amount=%s | side=%s",
                        order.order_code,
                        order.fiat_amount,
                        order.side,
                    )
                else:
                    logger.warning(
                        "MEXC new order notification not sent | order=%s",
                        order.order_code,
                    )

        if created and order.status == "WAITING_PAYMENT":
            unmatched_transactions = db.scalars(
                select(BankTransaction).where(
                    BankTransaction.status == "UNMATCHED",
                    BankTransaction.amount >= order.fiat_amount,
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
                    f"cập nhật {updated_count} · lỗi detail {detail_failed_count} · "
                    f"Thông báo mới {telegram_new_order_count} · đối soát lại {reconciled_count}"
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
        "order_details_failed": detail_failed_count,
        "orders_skipped_filters": skipped_filters,
        "api_incoming_side": expected_api_side,
        "telegram_new_orders_sent": telegram_new_order_count,
        "notifications_new_orders_sent": telegram_new_order_count,
        "transactions_reconciled": reconciled_count,
        "source_endpoint": source_endpoint,
        "synced_at": now.isoformat(),
    }


async def mexc_sync_loop() -> None:
    interval = max(10, settings.mexc_p2p_sync_seconds)
    await asyncio.sleep(3)
    logger.info(
        "MEXC sync loop started | interval=%ss | enabled=%s | incoming_side=%s",
        interval,
        settings.mexc_p2p_enabled,
        settings.mexc_p2p_incoming_side,
    )

    empty_polls = 0
    failed = False
    while True:
        try:
            with SessionLocal() as db:
                result = await sync_mexc_orders(db)
                async with MexcP2PClient() as release_client:
                    await process_mexc_releases(db, release_client)
            if failed:
                await notify_system_alert("MEXC sync đã hoạt động trở lại")
            failed = False

            if (
                result["orders_received"] > 0
                or result["orders_created"] > 0
                or result["order_details_failed"] > 0
            ):
                logger.info("MEXC sync result: %s", result)
                empty_polls = 0
            else:
                empty_polls += 1
                if empty_polls >= 6:
                    logger.info(
                        "MEXC sync heartbeat | received=0 | source=%s",
                        result["source_endpoint"],
                    )
                    empty_polls = 0
        except asyncio.CancelledError:
            logger.info("MEXC sync loop stopped")
            raise
        except Exception as exc:
            # Never log chained HTTP exceptions: they may include signed URLs.
            logger.error("MEXC sync failed | error=%s", type(exc).__name__)
            if not failed:
                from .diagnostics import error_detail
                await notify_system_alert("MEXC đồng bộ/đối chiếu lỗi: " + error_detail(exc)
                    + " Hệ thống sẽ kiểm tra lại; kiểm tra lệnh chưa được đồng bộ trên sàn.")
            failed = True

        await asyncio.sleep(interval)

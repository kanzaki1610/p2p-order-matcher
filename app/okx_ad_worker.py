"""Original-account ad repricing, disabled until the user activates its flag."""
import asyncio
import hashlib
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal

import httpx
from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, String, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from .config import settings
from .database import Base, SessionLocal
from .diagnostics import error_detail
from .discord_notifications import send_discord_message, valid_webhook
from .models import OKXSlot, OKXAuditLog
from .okx_ads import read_ad, number
from .okx_cost import read_last_buy_cost, read_pricing_cost, cost_summary, cost_revision, save_cost
from .okx_price_controls import selected_rank, pricing_active
from .okx_rank import read_rank_plan
from .okx_release import request, failure_reason
from .okx_ad_limits import OKXAutoLimits, OKXPendingLimits, price_limits, limits_match
from . import okx_buy_budget as buy_budget
from .telegram_commands import format_vietnam_time

logger = logging.getLogger("uvicorn.error.okx_ad_pricing")


class OKXAdPriceAttempt(Base):
    __tablename__ = "okx_ad_price_attempts"
    slot_id: Mapped[int] = mapped_column(ForeignKey("okx_slots.id"), primary_key=True)
    state: Mapped[str] = mapped_column(String(30), default="IDLE")
    action: Mapped[str | None] = mapped_column(String(30), nullable=True)
    old_ad_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    result_ad_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    hidden: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    paused_by_floor: Mapped[bool] = mapped_column(Boolean, default=False)
    last_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    notified_key: Mapped[str | None] = mapped_column(String(64), nullable=True)


def safe_failure(error):
    # Do not expose arbitrary exception text, signed URLs or API response messages.
    from .okx_release import OKXRequestError
    if isinstance(error, buy_budget.BuyBudgetError): return str(error)
    return failure_reason(error) if isinstance(error, OKXRequestError) else error_detail(error)


async def notice(db, attempt, slot, kind, reason, plan=None):
    key = hashlib.sha256((kind + reason).encode()).hexdigest()
    if attempt.notified_key == key:
        return
    text = (f"OKX · VNTrader · cảnh báo giá {kind}\nMã quảng cáo: {slot.adv_id}\n"
            f"Chiều: {slot.side}\nThời gian VN: {format_vietnam_time(datetime.now(timezone.utc))}\nLý do: {reason}")
    if plan:
        text += f"\nGiá vị trí {plan.get('target_rank', 10)}: {plan.get('proposed_price')}đ/USDT"
        if plan.get("cost_source"):
            text += '\nNguồn giá vốn: '+('nhập thủ công' if plan['cost_source'].get('mode')=='MANUAL' else 'lệnh mua OKX '+plan['cost_source']['order_id'])
        if plan.get("minimum_sell_price"):
            text += f"\nGiá vốn: {plan['cost_basis']}đ · giá bán tối thiểu: {plan['minimum_sell_price']}đ"
    if await send_discord_message("PRICING_ALERT", text):
        attempt.notified_key = key
        db.commit()


async def confirm(db, client, attempt, slot, plan=None):
    db.refresh(slot)
    if slot.adv_id != attempt.old_ad_id:
        await notice(db, attempt, slot, "REVIEW_REQUIRED", "Slot đã được đổi quảng cáo trong lúc chờ kết quả; không ghi đè cấu hình mới.")
        return False
    if not attempt.result_ad_id:
        await notice(db, attempt, slot, "UNKNOWN", pending_reason(attempt), plan)
        return False
    row = await read_ad(client, attempt.result_ad_id)
    if not limits_match(db.get(OKXPendingLimits, slot.id), attempt, row):
        await notice(db, attempt, slot, 'REVIEW_REQUIRED', 'OKX chưa xác nhận đúng Min/Max đã gửi; không gửi lại yêu cầu.', plan)
        return False
    if not buy_budget.pending_matches(db, slot.id, attempt, row):
        await notice(db, attempt, slot, 'REVIEW_REQUIRED', 'Lượng USDT của quảng cáo mới chưa khớp phần còn lại; không gửi lại yêu cầu.', plan)
        return False
    db.refresh(slot)
    if slot.adv_id != attempt.old_ad_id:
        await notice(db, attempt, slot, 'REVIEW_REQUIRED', 'Slot đã đổi ID trong lúc đọc kết quả; không ghi đè quảng cáo mới.')
        return False
    collision = db.scalar(select(OKXSlot.id).where(OKXSlot.id != slot.id,
        OKXSlot.adv_id == attempt.result_ad_id, OKXSlot.auto_enabled.is_(True)))
    if collision is not None:
        await notice(db, attempt, slot, "REVIEW_REQUIRED", "ID quảng cáo mới đang gán cho slot khác; chưa cập nhật slot.")
        return False
    if (row.get("side") != slot.side.lower() or row.get("type") != "limit" or row.get("status") != "new"
            or row.get("isHidden") is not attempt.hidden or number(row.get("unitPrice")) != attempt.price):
        await notice(db, attempt, slot, "REVIEW_REQUIRED", "Quảng cáo sau yêu cầu chưa khớp giá, chiều hoặc trạng thái dự kiến; chỉ kiểm tra lại, không gửi lại.")
        return False
    buy_budget.bind_result(db, slot.id, attempt.old_ad_id, attempt.result_ad_id)
    was_paused = attempt.paused_by_floor
    slot.adv_id = attempt.result_ad_id
    slot.current_price = attempt.price
    attempt.state = "IDLE"
    attempt.reason = None
    pending = db.get(OKXPendingLimits, slot.id)
    if pending and attempt.action == 'UPDATE_PRICE':
        slot.min_amount, slot.max_amount = pending.minimum, pending.maximum
        db.execute(update(OKXAutoLimits).where(OKXAutoLimits.slot_id == slot.id,
            OKXAutoLimits.ad_id == attempt.old_ad_id).values(ad_id=attempt.result_ad_id))
        db.delete(pending)
    if attempt.action == "HIDE_AD":
        attempt.paused_by_floor = slot.side == 'SELL'
    elif was_paused:
        attempt.paused_by_floor = False
    db.add(OKXAuditLog(action="OKX_AD_PRICE_CONFIRMED", actor="okx-ad-pricer",
        detail=f"{slot.side} {attempt.old_ad_id} -> {slot.adv_id}; action={attempt.action}; price={attempt.price}; hidden={attempt.hidden}"))
    db.commit()
    if attempt.action == "HIDE_AD":
        await notice(db, attempt, slot, "HIDDEN", "Đã ẩn quảng cáo mua vì ngân sách/lượng USDT còn lại không đủ Min." if slot.side == "BUY" else "Đã xác minh quảng cáo tạm ẩn vì giá bán vị trí đã chọn dưới giá vốn +10đ.", plan)
    elif was_paused:
        await notice(db, attempt, slot, "RESUMED", "Đã xác minh giá mới đạt giá vốn +10đ và quảng cáo do bot tạm ẩn đã hiển thị lại.", plan)
    return True


def pending_reason(attempt):
    """Keep the original diagnostic stable across polling and restarts."""
    detail = f" Chi tiết: {attempt.reason}" if attempt.reason else ""
    return ("Chưa xác minh được kết quả sửa/ẩn quảng cáo." + detail
            + " Bot đã dừng bám giá cho slot này; không tự gửi lại. Cần kiểm tra trên OKX.")


async def process_ad_pricing(db, client):
    if not pricing_active(db) or settings.database_schema == "kayhap":
        return
    try:
        if not valid_webhook(settings.discord_webhook_pricing_alerts):
            logger.error("Ad pricing blocked: dedicated price-alert webhook missing/invalid")
            return
    except ValueError:
        return
    slots = db.scalars(select(OKXSlot).where(OKXSlot.auto_enabled.is_(True))).all()
    used_ids = {slot.adv_id for slot in slots if slot.adv_id}
    duplicate_ids = {code for code in used_ids if sum(slot.adv_id == code for slot in slots) > 1}
    cost = None
    cost_error = None
    for slot in slots:
        attempt = db.get(OKXAdPriceAttempt, slot.id)
        if attempt is None:
            attempt = OKXAdPriceAttempt(slot_id=slot.id)
            db.add(attempt)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                attempt = db.get(OKXAdPriceAttempt, slot.id)
        plan = None
        buy = None
        try:
            if attempt.state in {"SUBMITTING", "UNKNOWN", "CONFIRMING"}:
                await confirm(db, client, attempt, slot)
                continue
            if attempt.paused_by_floor and attempt.result_ad_id != slot.adv_id:
                attempt.paused_by_floor = False
                db.commit()
                await notice(db, attempt, slot, "CONFIG_CHANGED", "Slot đã đổi ID; không dùng trạng thái bot tạm ẩn của quảng cáo cũ để mở quảng cáo mới.")
            if not re.fullmatch(r"[0-9]{1,30}", slot.adv_id or "") or slot.adv_id in duplicate_ids:
                await notice(db, attempt, slot, "BLOCKED", "ID quảng cáo thiếu/sai hoặc được gán cho nhiều slot; chưa chỉnh giá.")
                continue
            row = await read_ad(client, slot.adv_id)
            if (row.get("side") != slot.side.lower() or row.get("type") != "limit"
                    or row.get("status") != "new" or type(row.get("isHidden")) is not bool):
                await notice(db, attempt, slot, "BLOCKED", "Quảng cáo sai chiều, không còn hoạt động, không phải giá cố định hoặc thiếu trạng thái hiển thị.")
                continue
            if slot.side == "SELL":
                if cost is None and cost_error is None:
                    try:
                        cost = await read_pricing_cost(db,client,reader=read_last_buy_cost)
                    except Exception as error:
                        cost_error = safe_failure(error)
                if cost_error:
                    await notice(db, attempt, slot, "COST_UNVERIFIED", "Chưa xác minh giá vốn nhập thủ công: " + cost_error)
                    continue
            rank = selected_rank(db, slot.side)
            plan = await read_rank_plan(client, slot.side, Decimal(cost["unit_price"]) if slot.side == "SELL" else None, target_rank=rank)
            if slot.side == "SELL":
                plan["cost_source"] = cost
            if plan["action"] == "BLOCKED":
                await notice(db, attempt, slot, "BLOCKED", plan["reason"], plan)
                continue
            current = number(row.get("unitPrice"))
            hidden = row["isHidden"]
            if plan["action"] == "HIDE_AD" and hidden:
                await notice(db, attempt, slot, "ALREADY_HIDDEN", "Quảng cáo đang ẩn và giá vị trí đã chọn chưa đạt giá vốn +10đ; giữ ẩn.", plan)
                continue
            proposed = Decimal(plan["proposed_price"])
            terms = price_limits(db, slot, row, proposed) if plan['action'] == 'UPDATE_PRICE' else {}
            if slot.side == 'BUY' and plan['action'] == 'UPDATE_PRICE':
                buy = await buy_budget.prepare_buy(db, client, slot, row, proposed)
                if buy['exhausted']:
                    if hidden:
                        await notice(db, attempt, slot, 'BUDGET_EXHAUSTED', 'Ngân sách/lượng USDT còn lại không đủ Min; giữ ẩn quảng cáo mua.', plan)
                        continue
                    plan['action'] = 'HIDE_AD'
                terms = buy['terms']
            resume = attempt.paused_by_floor and plan["action"] == "UPDATE_PRICE"
            terms_changed = bool(terms) and (number(row.get('minOrderSize')) != Decimal(terms['minOrderLimit']) or number(row.get('maxOrderSize')) != Decimal(terms['maxOrderLimit']))
            if buy and terms:
                terms_changed = terms_changed or buy_budget.stock_total(row) != Decimal(terms['cryptoAmount'])
            if plan["action"] == "UPDATE_PRICE" and current == proposed and not resume and not terms_changed:
                continue
            now = datetime.now(timezone.utc)
            # Share the write interval across BUY/SELL slots and worker restarts.
            # A protective hide is not delayed by ordinary price changes.
            if plan["action"] == "UPDATE_PRICE":
                latest = db.scalar(select(func.max(OKXAdPriceAttempt.last_action_at)))
                latest_utc = (latest.astimezone(timezone.utc) if latest.tzinfo else latest.replace(tzinfo=timezone.utc)) if latest else None
                if latest_utc and (now - latest_utc).total_seconds() < max(30, settings.okx_ad_min_update_seconds):
                    continue
            last = attempt.last_action_at
            last_utc = (last.astimezone(timezone.utc) if last.tzinfo else last.replace(tzinfo=timezone.utc)) if last else None
            if last_utc and (now - last_utc).total_seconds() < max(5, settings.okx_ad_pricing_seconds):
                continue
            is_hide = plan["action"] == "HIDE_AD"
            wanted_price = current if is_hide else proposed
            wanted_hidden = True if is_hide else (False if resume else hidden)
            config = db.get(OKXAutoLimits, slot.id)
            config_revision = (config.ad_id, config.minimum, config.maximum) if config else None
            claim = db.execute(update(OKXAdPriceAttempt).where(
                OKXAdPriceAttempt.slot_id == slot.id, OKXAdPriceAttempt.state == "IDLE").values(
                    state="SUBMITTING", old_ad_id=slot.adv_id, result_ad_id=None, action=plan["action"],
                    price=wanted_price, hidden=wanted_hidden, last_action_at=now, reason=None))
            db.commit()
            if claim.rowcount != 1:
                continue
            db.refresh(attempt)
            db.refresh(slot)
            db.expire_all()
            fresh_config = db.get(OKXAutoLimits, slot.id)
            if config_revision != ((fresh_config.ad_id, fresh_config.minimum, fresh_config.maximum) if fresh_config else None):
                attempt.state = 'IDLE'
                db.commit()
                continue
            if selected_rank(db, slot.side) != rank:
                attempt.state = "IDLE"
                db.commit()
                continue
            if not pricing_active(db) or not slot.auto_enabled or slot.adv_id != attempt.old_ad_id:
                attempt.state = "IDLE"
                db.commit()
                await notice(db, attempt, slot, "BLOCKED", "Cấu hình slot đã thay đổi trước khi gửi; chưa sửa quảng cáo.")
                continue
            if slot.side == 'SELL' and cost_revision(db) != cost.get('settings_revision'):
                attempt.state = 'IDLE';db.commit()
                await notice(db,attempt,slot,'BLOCKED','Giá vốn vừa thay đổi; chờ tính lại giá bán, chưa gửi OKX.')
                continue
            if buy:
                try:
                    fresh_ad = await read_ad(client, slot.adv_id)
                except Exception:
                    attempt.state = 'IDLE'
                    db.commit()
                    raise
                db.refresh(slot)
                if (buy_budget.fingerprint(fresh_ad) != buy['fingerprint'] or not slot.auto_enabled
                        or slot.adv_id != attempt.old_ad_id or not pricing_active(db)
                        or selected_rank(db, slot.side) != rank):
                    attempt.state = 'IDLE'
                    db.commit()
                    continue
            attempt.state = "UNKNOWN"
            old_budget = db.get(buy_budget.OKXPendingBuyBudget, slot.id)
            if old_budget:
                db.delete(old_budget)
                db.flush()
            if buy and not is_hide:
                db.add(buy_budget.OKXPendingBuyBudget(slot_id=slot.id, root_ad_id=buy['root'],
                    old_ad_id=slot.adv_id, quantity=Decimal(terms['cryptoAmount'])))
            previous = db.get(OKXPendingLimits, slot.id)
            if previous:
                db.delete(previous)
                db.flush()
            if terms:
                db.add(OKXPendingLimits(slot_id=slot.id, ad_id=slot.adv_id, minimum=Decimal(terms['minOrderLimit']), maximum=Decimal(terms['maxOrderLimit'])))
            db.commit()  # A crash/timeout never authorizes automatic resubmission.
            if is_hide:
                result = await request(client, "POST", "/api/v5/p2p/ad/update-active-status", {"adId": slot.adv_id, "status": "hidden"})
                result_id = str(result.get("adId") or "")
                if result_id != slot.adv_id:
                    raise ValueError("Sai mã quảng cáo sau yêu cầu ẩn")
            else:
                body = {"adId": slot.adv_id, "unitPrice": str(proposed), **terms}
                if resume:
                    body["online"] = "true"
                result = await request(client, "POST", "/api/v5/p2p/ad/update", body)
                if str(result.get("oldAdId")) != slot.adv_id:
                    raise ValueError("Sai mã quảng cáo cũ trong phản hồi sửa")
                result_id = str(result.get("newAdId") or "")
                if not re.fullmatch(r"[0-9]{1,30}", result_id):
                    raise ValueError("Chưa có ID quảng cáo thay thế hợp lệ")
            attempt.result_ad_id = result_id
            attempt.state = "CONFIRMING"
            db.commit()
            await confirm(db, client, attempt, slot, plan)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            db.rollback()
            attempt = db.get(OKXAdPriceAttempt, slot.id)
            if attempt:
                attempt.reason = safe_failure(error)[:500]
                db.commit()
                kind = "UNKNOWN" if attempt.state in {"SUBMITTING", "UNKNOWN", "CONFIRMING"} else "ERROR"
                reason = pending_reason(attempt) if kind == "UNKNOWN" else "Chỉnh giá gặp lỗi: " + attempt.reason
                await notice(db, attempt, slot, kind, reason, plan)


async def pricing_loop():
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        while True:
            started = asyncio.get_running_loop().time()
            try:
                with SessionLocal() as db:
                    await process_ad_pricing(db, client)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error("OKX ad pricing cycle failed: %s", type(error).__name__)
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(.1, max(5, settings.okx_ad_pricing_seconds) - elapsed))

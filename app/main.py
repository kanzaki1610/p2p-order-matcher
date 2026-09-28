import asyncio
import secrets

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings
from .bitget_sync import bitget_sync_loop
from .mexc_sync import mexc_sync_loop
from .database import Base, engine, ensure_runtime_schema, get_db
from .dashboard import router as dashboard_router
from .matching import match_transaction
from .models import BankTransaction, P2POrder
from .schemas import BankTransactionIn, MatchResult, OrderCreate, OrderOut, SePayWebhookIn, SePayWebhookOut
from .sepay import combined_description, normalize_bank, parse_sepay_datetime, transaction_id
from .telegram import notify_match, register_telegram_webhook, send_telegram_message
from .telegram_commands import handle_command

Base.metadata.create_all(bind=engine)
ensure_runtime_schema()
app = FastAPI(title="P2P Multi-Exchange Matcher", version="1.5.0")
app.include_router(dashboard_router)
bitget_task: asyncio.Task | None = None
mexc_task: asyncio.Task | None = None


@app.on_event("startup")
async def start_bitget_reader() -> None:
    global bitget_task
    configured = bool(
        settings.bitget_p2p_enabled
        and settings.bitget_p2p_api_key
        and settings.bitget_p2p_api_secret
        and settings.bitget_p2p_api_passphrase
    )
    if configured and not settings.bitget_p2p_live_writes:
        bitget_task = asyncio.create_task(bitget_sync_loop())


@app.on_event("shutdown")
async def stop_bitget_reader() -> None:
    global bitget_task
    if bitget_task:
        bitget_task.cancel()
        try:
            await bitget_task
        except asyncio.CancelledError:
            pass
        bitget_task = None


@app.on_event("startup")
async def start_mexc_reader() -> None:
    global mexc_task
    configured = bool(
        settings.mexc_p2p_enabled
        and settings.mexc_p2p_api_key
        and settings.mexc_p2p_api_secret
    )
    if configured and not settings.mexc_p2p_live_writes:
        mexc_task = asyncio.create_task(mexc_sync_loop())


@app.on_event("shutdown")
async def stop_mexc_reader() -> None:
    global mexc_task
    if mexc_task:
        mexc_task.cancel()
        try:
            await mexc_task
        except asyncio.CancelledError:
            pass
        mexc_task = None


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/dashboard")


def verify_ingest_key(x_api_key: str = Header(default="")) -> None:
    if not settings.ingest_api_key or not secrets.compare_digest(x_api_key, settings.ingest_api_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API key không hợp lệ")


def verify_sepay_key(
    x_api_key: str = Header(default=""),
    authorization: str = Header(default=""),
) -> None:
    candidates = [x_api_key]
    if authorization:
        candidates.append(authorization.removeprefix("Bearer ").removeprefix("Apikey ").strip())
    valid = any(
        secrets.compare_digest(candidate, settings.sepay_webhook_secret)
        for candidate in candidates
        if candidate
    )
    if not settings.sepay_webhook_secret or not valid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Webhook secret không hợp lệ")


def verify_telegram_key(x_telegram_bot_api_secret_token: str = Header(default="")) -> None:
    expected = settings.telegram_webhook_secret
    if not expected or not secrets.compare_digest(x_telegram_bot_api_secret_token, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Telegram webhook secret không hợp lệ")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "auto_release": False,
        "telegram_configured": bool(settings.telegram_bot_token and settings.telegram_chat_id),
        "bitget_p2p_enabled": settings.bitget_p2p_enabled,
        "bitget_p2p_read_only": not settings.bitget_p2p_live_writes,
        "mexc_p2p_enabled": settings.mexc_p2p_enabled,
        "mexc_p2p_read_only": not settings.mexc_p2p_live_writes,
    }


@app.post("/telegram/setup-webhook")
async def setup_telegram_webhook(_: None = Depends(verify_ingest_key)):
    success, message = await register_telegram_webhook()
    if not success:
        raise HTTPException(status_code=502, detail=message)
    return {"success": True, "message": message}


@app.post("/webhooks/telegram")
async def telegram_webhook(
    update: dict,
    db: Session = Depends(get_db),
    _: None = Depends(verify_telegram_key),
):
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    text = str(message.get("text", "")).strip()
    if not chat_id or not text:
        return {"ok": True, "ignored": True}
    if not secrets.compare_digest(chat_id, settings.telegram_chat_id):
        return {"ok": True, "ignored": True}
    sender = message.get("from") or {}
    first_name = str(sender.get("first_name", "")).strip()
    last_name = str(sender.get("last_name", "")).strip()
    display_name = " ".join(part for part in (first_name, last_name) if part) or None
    reply = handle_command(
        text,
        db,
        telegram_user_id=str(sender.get("id", "unknown")),
        telegram_username=sender.get("username"),
        telegram_display_name=display_name,
    )
    await send_telegram_message(chat_id, reply)
    return {"ok": True}


@app.post("/orders", response_model=OrderOut, status_code=201)
def create_order(payload: OrderCreate, db: Session = Depends(get_db), _: None = Depends(verify_ingest_key)):
    order = P2POrder(**payload.model_dump())
    db.add(order)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="order_code đã tồn tại")
    db.refresh(order)
    return order


@app.get("/orders", response_model=list[OrderOut])
def list_orders(db: Session = Depends(get_db), _: None = Depends(verify_ingest_key)):
    return db.scalars(select(P2POrder).order_by(P2POrder.id.desc()).limit(200)).all()


@app.post("/bank-transactions/{bank}", response_model=MatchResult, status_code=201)
async def ingest_transaction(
    bank: str,
    payload: BankTransactionIn,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _: None = Depends(verify_ingest_key),
):
    bank = bank.upper()
    if bank not in {"MB", "VIB", "VPBANK", "ACB"}:
        raise HTTPException(status_code=422, detail="Chỉ hỗ trợ MB, VIB, VPBANK hoặc ACB")
    tx = BankTransaction(bank=bank, **payload.model_dump())
    db.add(tx)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Giao dịch ngân hàng đã được tiếp nhận trước đó")
    db.refresh(tx)
    decision, order, score, reasons = match_transaction(db, tx)
    background_tasks.add_task(notify_match, decision, order, tx, score, reasons)
    return MatchResult(
        bank_transaction_id=tx.id,
        decision=decision,
        order_code=order.order_code if order else None,
        score=score,
        reasons=reasons,
    )


@app.post("/webhooks/sepay", response_model=SePayWebhookOut)
async def sepay_webhook(
    payload: SePayWebhookIn,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _: None = Depends(verify_sepay_key),
):
    if payload.transferType.lower() not in {"in", "credit"}:
        return SePayWebhookOut(message="Bỏ qua giao dịch tiền ra", decision="IGNORED")

    bank = normalize_bank(payload.gateway)
    tx_id = transaction_id(payload)
    duplicate = db.scalar(
        select(BankTransaction).where(
            BankTransaction.bank == bank,
            BankTransaction.transaction_id == tx_id,
        )
    )
    if duplicate:
        return SePayWebhookOut(message="Giao dịch đã được tiếp nhận trước đó", decision="DUPLICATE")

    tx = BankTransaction(
        bank=bank,
        transaction_id=tx_id,
        amount=payload.transferAmount,
        direction="CREDIT",
        sender_name=None,
        sender_account=None,
        description=combined_description(payload),
        occurred_at=parse_sepay_datetime(payload.transactionDate),
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)
    decision, order, score, reasons = match_transaction(db, tx)
    background_tasks.add_task(notify_match, decision, order, tx, score, reasons)
    return SePayWebhookOut(
        message="Đã tiếp nhận giao dịch SePay",
        decision=decision,
        order_code=order.order_code if order else None,
        score=score,
    )

import secrets

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings
from .database import Base, engine, get_db
from .matching import match_transaction
from .models import BankTransaction, P2POrder
from .schemas import BankTransactionIn, MatchResult, OrderCreate, OrderOut, SePayWebhookIn, SePayWebhookOut
from .sepay import combined_description, normalize_bank, parse_sepay_datetime, transaction_id
from .telegram import notify_match, register_telegram_webhook, send_telegram_message
from .telegram_commands import handle_command

Base.metadata.create_all(bind=engine)
app = FastAPI(title="P2P Order Matcher", version="0.3.0")


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
    reply = handle_command(text, db)
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
    if bank not in {"MB", "VIB", "VPBANK"}:
        raise HTTPException(status_code=422, detail="Chỉ hỗ trợ MB, VIB hoặc VPBANK")
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

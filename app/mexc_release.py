"""MEXC release coordination: authenticated receipts and at-most-once POST."""
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import ForeignKey, String, UniqueConstraint, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from .config import settings
from .database import Base
from .matching import same_name, satisfies_payment_rules
from .models import BankTransaction, P2POrder, PaymentMatch
from .notifications import notify_event
from .sepay import normalize_bank


class MexcOrderReceipt(Base):
    __tablename__ = "mexc_order_receipts"
    order_code: Mapped[str] = mapped_column(String(100), primary_key=True)
    api_side: Mapped[str] = mapped_column(String(10))


class MexcReleaseAttempt(Base):
    __tablename__ = "mexc_release_attempts"
    __table_args__ = (UniqueConstraint("transaction_id", name="uq_mexc_release_tx"),)
    order_id: Mapped[int] = mapped_column(ForeignKey("p2p_orders.id"), primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("bank_transactions.id"))
    state: Mapped[str] = mapped_column(String(30), default="READY")
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    notified_state: Mapped[str | None] = mapped_column(String(30), nullable=True)


def consistent(order, receipt, row):
    return (str(row.get("advOrderNo")) == order.order_code
        and row.get("side") == receipt.api_side
        and row.get("coinName") == "USDT" and row.get("fiatUnit") == "VND"
        and Decimal(str(row.get("amount"))) == order.fiat_amount
        and Decimal(str(row.get("tradableQuantity"))) == order.crypto_amount
        and same_name((row.get("userInfo") or {}).get("realName"), order.counterparty_name))


def preflight_reason(order, tx, receipt, row):
    if not satisfies_payment_rules(order, tx):
        return "Tiền ngân hàng không đáp ứng quy tắc đối chiếu."
    try:
        if not consistent(order, receipt, row):
            return "Chi tiết MEXC khác mã lệnh, tiền tệ, số tiền hoặc họ tên."
        if row.get("complained") is not False or row.get("blockUser") is not False:
            return "Lệnh có tranh chấp, bị chặn hoặc thiếu trạng thái an toàn."
        payment = row.get("confirmPaymentInfo")
        bank = normalize_bank(payment.get("bankName")) if isinstance(payment, dict) else None
        if not bank or bank != normalize_bank(tx.bank):
            return "Ngân hàng thanh toán trên MEXC chưa xác nhận hoặc khác tiền nhận."
        # Official error 60029 documents the API-side state convention.
        expected = {"BUY": "PAID", "SELL": "PROCESSING"}.get(receipt.api_side)
        if row.get("state") != expected:
            return "MEXC chưa ở trạng thái cho phép mở khóa."
    except (ValueError, TypeError, ArithmeticError, AttributeError):
        return "Chi tiết MEXC thiếu hoặc sai dữ liệu bắt buộc."
    return None


async def notify_attempt(db, attempt, order, tx):
    if attempt.state == attempt.notified_state or attempt.state == "READY":
        return
    event = {"RELEASED": "CONFIRMED", "SUBMITTED": "AUTO_MATCHED",
        "WAITING_BUYER_PAYMENT": "PAYMENT_DETECTED"}.get(attempt.state, "REVIEW_REQUIRED")
    now = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Ho_Chi_Minh"))
    text = (f"MEXC P2P\nMã lệnh: {order.order_code}\nNgân hàng: {tx.bank}"
        f"\nSố tiền: {tx.amount:,.0f} VND\nNội dung: {tx.description or ''}"
        f"\nTrạng thái mở khóa: {attempt.state}\nThời gian VN: {now:%d/%m/%Y %H:%M:%S}")
    if attempt.reason:
        text += "\nLý do: " + attempt.reason
    if attempt.state in {"UNKNOWN", "SUBMITTING", "REVIEW_REQUIRED"}:
        text += "\nKiểm tra thủ công trên MEXC. Không tự gửi lại yêu cầu mở khóa."
    if await notify_event(event, text):
        attempt.notified_state = attempt.state
        db.commit()


async def process_mexc_releases(db, client):
    if not (settings.mexc_auto_release_enabled and settings.mexc_p2p_live_writes):
        return
    from .okx_sync import OKXOrderReceipt
    from .okx_release import OKXReleaseAttempt
    for match in db.scalars(select(PaymentMatch).where(PaymentMatch.decision == "AUTO_MATCHED")).all():
        order, tx = db.get(P2POrder, match.order_id), db.get(BankTransaction, match.transaction_id)
        receipt = db.get(MexcOrderReceipt, order.order_code)
        if receipt is None or db.get(OKXOrderReceipt, order.order_code) is not None:
            continue
        if db.scalar(select(OKXReleaseAttempt).where(OKXReleaseAttempt.transaction_id == tx.id)):
            continue
        # A receipt is mandatory even for historical matches; syncing backfills it.
        attempt = db.get(MexcReleaseAttempt, order.id)
        if attempt is None:
            if order.status != "PAYMENT_DETECTED" or not satisfies_payment_rules(order, tx):
                continue
            attempt = MexcReleaseAttempt(order_id=order.id, transaction_id=tx.id, state="READY")
            db.add(attempt)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                continue
        if attempt.state in {"READY", "WAITING_BUYER_PAYMENT"}:
            claimed = db.execute(update(MexcReleaseAttempt).where(
                MexcReleaseAttempt.order_id == order.id,
                MexcReleaseAttempt.state.in_(["READY", "WAITING_BUYER_PAYMENT"])
            ).values(state="SUBMITTING"))
            db.commit()
            if claimed.rowcount != 1:
                continue
            db.refresh(attempt)
            try:
                row = await client.get_order_detail(order.order_code)
                reason = preflight_reason(order, tx, receipt, row)
                expected = {"BUY": "PAID", "SELL": "PROCESSING"}.get(receipt.api_side)
                if row.get("state") in {"NOT_PAID", "WAIT_PROCESS", "PAID"} and reason and preflight_reason(
                    order, tx, receipt, dict(row, state=expected)) is None:
                    attempt.state = "WAITING_BUYER_PAYMENT"
                    attempt.reason = "Đã khớp tiền; chờ MEXC ghi nhận trạng thái thanh toán cho phép mở khóa."
                    db.commit()
                    await notify_attempt(db, attempt, order, tx)
                    continue
                if reason:
                    attempt.state, attempt.reason = "REVIEW_REQUIRED", reason
                    db.commit()
                    await notify_attempt(db, attempt, order, tx)
                    continue
            except Exception:
                attempt.state, attempt.reason = "REVIEW_REQUIRED", "Không đọc được chi tiết MEXC trước mở khóa."
                db.commit()
                await notify_attempt(db, attempt, order, tx)
                continue
            attempt.state, attempt.reason = "UNKNOWN", "Chưa xác định kết quả yêu cầu mở khóa."
            db.commit()
            try:
                await client.release_coin(order.order_code)
                attempt.state, attempt.reason = "SUBMITTED", "MEXC tiếp nhận; chờ xác nhận DONE."
                db.commit()
            except Exception:
                # Never expose signed URLs, credentials or untrusted API messages.
                pass
        if attempt.state in {"SUBMITTING", "SUBMITTED", "UNKNOWN"}:
            try:
                row = await client.get_order_detail(order.order_code)
                if consistent(order, receipt, row) and row.get("state") == "DONE":
                    attempt.state, attempt.reason = "RELEASED", None
                    order.status = "RELEASED"
                    db.commit()
            except Exception:
                pass
        await notify_attempt(db, attempt, order, tx)

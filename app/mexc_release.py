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
from .diagnostics import error_detail, safe_status

LEGACY_BLOCK_REASON = "Lệnh có tranh chấp, bị chặn hoặc thiếu trạng thái an toàn."
LEGACY_BANK_REASON = "Ngân hàng thanh toán trên MEXC chưa xác nhận hoặc khác tiền nhận."
MISSING_BANK_REASON = "MEXC chưa trả ngân hàng thanh toán đã chọn."
WRONG_BANK_REASON = "Ngân hàng thanh toán đã chọn trên MEXC khác tiền nhận."


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


def preflight_reason(order, tx, receipt, row, *, allow_missing_bank=False):
    if order.status != "PAYMENT_DETECTED":
        return "Trạng thái nội bộ của lệnh đã thay đổi; cần kiểm tra thủ công."
    if not satisfies_payment_rules(order, tx):
        return "Tiền ngân hàng không đáp ứng quy tắc đối chiếu."
    try:
        if not consistent(order, receipt, row):
            return "Chi tiết MEXC khác mã lệnh, tiền tệ, số tiền hoặc họ tên."
        if row.get("complained") is not False:
            return "Lệnh có tranh chấp hoặc thiếu xác nhận complained=false."
        # Live merchant detail omits blockUser (null) for unblocked users.
        # This is a user-block indicator, not the order's appeal status.
        if row.get("blockUser") is not None and row.get("blockUser") is not False:
            return "MEXC báo người dùng bị chặn hoặc blockUser sai định dạng."
        payment = row.get("confirmPaymentInfo")
        raw_bank = payment.get("bankName") if isinstance(payment, dict) else None
        bank = normalize_bank(raw_bank) if isinstance(raw_bank, str) and raw_bank.strip() else None
        if not bank and not allow_missing_bank:
            return MISSING_BANK_REASON
        if bank and bank != normalize_bank(tx.bank):
            return WRONG_BANK_REASON
        # Official error 60029 documents the API-side state convention.
        expected = {"BUY": "PAID", "SELL": "PROCESSING"}.get(receipt.api_side)
        if row.get("state") != expected:
            return (f"MEXC chưa ở trạng thái cho phép mở khóa: state={safe_status(row.get('state'))}; "
                    f"cần {expected}. Kiểm tra người mua đã bấm Đã thanh toán và trạng thái lệnh trên sàn.")
    except (ValueError, TypeError, ArithmeticError, AttributeError):
        return "Chi tiết MEXC thiếu hoặc sai dữ liệu bắt buộc."
    return None


async def notify_attempt(db, attempt, order, tx):
    if attempt.state == attempt.notified_state or attempt.state == "READY":
        return
    event = {"RELEASED": "CONFIRMED", "SUBMITTED": "AUTO_MATCHED",
        "WAITING_BUYER_PAYMENT": "PAYMENT_DETECTED",
        "WAITING_PAYMENT_INFO": "PAYMENT_DETECTED"}.get(attempt.state, "REVIEW_REQUIRED")
    now = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Ho_Chi_Minh"))
    text = (f"MEXC P2P\nMã lệnh: {order.order_code}\nNgân hàng: {tx.bank}"
        f"\nMã GD ngân hàng: {tx.transaction_id}\nNgười mua: {order.counterparty_name or 'Thiếu họ tên'}"
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
        if attempt is not None and attempt.transaction_id != tx.id:
            continue
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
        legacy_reasons = [LEGACY_BLOCK_REASON, LEGACY_BANK_REASON]
        retry_legacy_preflight = attempt.state == "REVIEW_REQUIRED" and attempt.reason in legacy_reasons
        if attempt.state in {"READY", "WAITING_BUYER_PAYMENT", "WAITING_PAYMENT_INFO"} or retry_legacy_preflight:
            claimed = db.execute(update(MexcReleaseAttempt).where(
                MexcReleaseAttempt.order_id == order.id,
                ((MexcReleaseAttempt.state.in_(["READY", "WAITING_BUYER_PAYMENT", "WAITING_PAYMENT_INFO"])) |
                 ((MexcReleaseAttempt.state == "REVIEW_REQUIRED") &
                  (MexcReleaseAttempt.reason.in_(legacy_reasons))))
            ).values(state="SUBMITTING"))
            db.commit()
            if claimed.rowcount != 1:
                continue
            db.refresh(attempt)
            try:
                matches = db.scalars(select(PaymentMatch).where(
                    PaymentMatch.decision == "AUTO_MATCHED",
                    (PaymentMatch.transaction_id == tx.id) | (PaymentMatch.order_id == order.id)
                )).all()
                if len(matches) != 1:
                    attempt.state, attempt.reason = "REVIEW_REQUIRED", "Tiền hoặc lệnh có nhiều kết quả đối chiếu; cần kiểm tra thủ công."
                    db.commit()
                    await notify_attempt(db, attempt, order, tx)
                    continue
                row = await client.get_order_detail(order.order_code)
                if satisfies_payment_rules(order, tx) and consistent(order, receipt, row) and row.get("state") == "DONE":
                    # Completed manually/on exchange; do not issue a release POST.
                    attempt.state = "RELEASED"
                    attempt.reason = "Lệnh đã DONE trên MEXC; đồng bộ hoàn tất, không gửi yêu cầu mở khóa."
                    order.status = "RELEASED"
                    db.commit()
                    await notify_attempt(db, attempt, order, tx)
                    continue
                reason = preflight_reason(order, tx, receipt, row)
                expected = {"BUY": "PAID", "SELL": "PROCESSING"}.get(receipt.api_side)
                if (reason == MISSING_BANK_REASON
                    and expected is not None
                    and row.get("state") in {"NOT_PAID", "WAIT_PROCESS", "PAID", expected}
                    and preflight_reason(order, tx, receipt, dict(row, state=expected),
                        allow_missing_bank=True) is None):
                    # Missing buyer selection is temporary; it never authorizes a POST.
                    attempt.state, attempt.reason = "WAITING_PAYMENT_INFO", MISSING_BANK_REASON
                    db.commit()
                    await notify_attempt(db, attempt, order, tx)
                    continue
                if row.get("state") in {"NOT_PAID", "WAIT_PROCESS", "PAID"} and reason and preflight_reason(
                    order, tx, receipt, dict(row, state=expected)) is None:
                    attempt.state = "WAITING_BUYER_PAYMENT"
                    attempt.reason = ("Đã khớp tiền; chờ MEXC ghi nhận trạng thái thanh toán cho phép mở khóa. "
                        f"Hiện tại state={safe_status(row.get('state'))}; cần {expected}. "
                        "Chưa gửi yêu cầu mở khóa; kiểm tra người mua đã bấm Đã thanh toán.")
                    db.commit()
                    await notify_attempt(db, attempt, order, tx)
                    continue
                if reason:
                    attempt.state, attempt.reason = "REVIEW_REQUIRED", reason
                    db.commit()
                    await notify_attempt(db, attempt, order, tx)
                    continue
            except Exception as error:
                attempt.state, attempt.reason = "REVIEW_REQUIRED", ("Không đọc được chi tiết MEXC trước mở khóa. "
                    + error_detail(error))
                db.commit()
                await notify_attempt(db, attempt, order, tx)
                continue
            attempt.state, attempt.reason = "UNKNOWN", "Chưa xác định kết quả yêu cầu mở khóa."
            db.commit()
            try:
                await client.release_coin(order.order_code)
                attempt.state, attempt.reason = "SUBMITTED", "MEXC tiếp nhận; chờ xác nhận DONE."
                db.commit()
            except Exception as error:
                # Never expose signed URLs, credentials or untrusted API messages.
                attempt.reason = "Yêu cầu mở khóa MEXC chưa được xác nhận: " + error_detail(error)
                db.commit()
        if attempt.state in {"SUBMITTING", "SUBMITTED", "UNKNOWN"}:
            try:
                row = await client.get_order_detail(order.order_code)
                if consistent(order, receipt, row) and row.get("state") == "DONE":
                    attempt.state, attempt.reason = "RELEASED", None
                    order.status = "RELEASED"
                    db.commit()
            except Exception as error:
                attempt.reason = ((attempt.reason or "Chưa xác định kết quả mở khóa.")[:250]
                    + " Kiểm tra trạng thái sau yêu cầu thất bại: " + error_detail(error))[:500]
                db.commit()
        await notify_attempt(db, attempt, order, tx)

"""Release coordinator. Disabled by default; test with an injected mock transport."""
import base64
import hashlib
import hmac
import json
import re
import httpx
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlencode

from sqlalchemy import ForeignKey, String, UniqueConstraint, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from .config import settings
from .database import Base
from .matching import normalize_text, same_name, satisfies_payment_rules
from .models import BankTransaction, P2POrder, PaymentMatch
from .notifications import notify_event
from .okx_sync import OKXOrderReceipt


class OKXReleaseAttempt(Base):
    __tablename__ = "okx_release_attempts"
    __table_args__ = (UniqueConstraint("transaction_id", name="uq_okx_release_tx"),)
    order_id: Mapped[int] = mapped_column(ForeignKey("p2p_orders.id"), primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("bank_transactions.id"))
    state: Mapped[str] = mapped_column(String(30), default="READY")
    notified_state: Mapped[str | None] = mapped_column(String(30), nullable=True)


class OKXReleaseDiagnostic(Base):
    __tablename__ = "okx_release_diagnostics"
    order_id: Mapped[int] = mapped_column(ForeignKey("p2p_orders.id"), primary_key=True)
    reason: Mapped[str] = mapped_column(String(500))


class OKXRequestError(RuntimeError):
    pass


def safe_code(value):
    value = str(value)
    return value if re.fullmatch(r"[0-9]{1,12}", value) else "không có mã hợp lệ"


def failure_reason(error):
    if isinstance(error, OKXRequestError):
        return str(error)
    if isinstance(error, httpx.TimeoutException):
        return "Hết thời gian chờ OKX; chưa xác định yêu cầu đã được xử lý hay chưa."
    if isinstance(error, httpx.RequestError):
        return "Lỗi kết nối OKX; chưa xác định kết quả yêu cầu."
    return "Phản hồi OKX không đọc được hoặc không đúng định dạng."


def save_diagnostic(db, attempt, reason):
    diagnostic = db.get(OKXReleaseDiagnostic, attempt.order_id)
    if diagnostic is None:
        diagnostic = OKXReleaseDiagnostic(order_id=attempt.order_id, reason=reason)
        db.add(diagnostic)
    else:
        diagnostic.reason = reason
    db.commit()


async def request(client, method, path, body=None):
    timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    encoded = json.dumps(body, separators=(",", ":")) if body is not None else ""
    signature = base64.b64encode(hmac.new(settings.okx_api_secret.encode(),
        (timestamp + method + path + encoded).encode(), hashlib.sha256).digest()).decode()
    response = await client.request(method, settings.okx_base_url.rstrip("/") + path,
        content=encoded or None, headers={"OK-ACCESS-KEY": settings.okx_api_key,
        "OK-ACCESS-SIGN": signature, "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": settings.okx_api_passphrase, "Content-Type": "application/json"})
    try:
        payload = response.json()
    except (ValueError, TypeError):
        raise OKXRequestError(f"OKX HTTP {response.status_code}; phản hồi không phải JSON.") from None
    if not isinstance(payload, dict):
        raise OKXRequestError(f"OKX HTTP {response.status_code}; cấu trúc phản hồi không hợp lệ.")
    if response.status_code != 200:
        raise OKXRequestError(f"OKX HTTP {response.status_code}; mã API: {safe_code(payload.get('code'))}.")
    if str(payload.get("code")) != "0":
        raise OKXRequestError(f"OKX từ chối yêu cầu; mã API: {safe_code(payload.get('code'))}.")
    data = payload.get("data")
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
        return data[0]
    if isinstance(data, dict):
        return data
    raise RuntimeError("Unexpected OKX response")


async def detail(client, code):
    return await request(client, "GET", "/api/v5/p2p/order?" + urlencode({"orderId": code}))


def consistent(order, row):
    party = row.get("counterpartyDetail") or {}
    return (str(row.get("orderId")) == order.order_code
        # Get Order does not document isOwner; ad ownership is not order ownership.
        # The authenticated detail response and imported receipt identify the order.
        and row.get("side") == "sell"
        and str(row.get("cryptoCurrency") or "").strip().upper() == "USDT"
        and str(row.get("fiatCurrency") or "").strip().upper() == "VND"
        and Decimal(str(row.get("fiatAmount"))) == order.fiat_amount
        and Decimal(str(row.get("cryptoAmount"))) == order.crypto_amount
        and bool(normalize_text(party.get("realName")))
        and same_name(party.get("realName"), order.counterparty_name))


def releasable(order, row):
    return (consistent(order, row) and row.get("orderStatus") == "new"
        and row.get("paymentStatus") in {"paid", "unreceived"}
        and row.get("isFrozen") is False and str(row.get("disputeStatus")) == "0")


def preflight_reason(order, tx, row):
    if not satisfies_payment_rules(order, tx):
        return "Giao dịch ngân hàng không còn đáp ứng quy tắc đối chiếu."
    try:
        if not consistent(order, row):
            return "Chi tiết OKX thiếu hoặc khác mã lệnh, bên bán, tiền tệ, số tiền hoặc họ tên đã nhập."
    except (ValueError, TypeError, ArithmeticError, AttributeError):
        return "Chi tiết OKX thiếu hoặc sai định dạng dữ liệu bắt buộc."
    if row.get("orderStatus") != "new":
        return "Lệnh OKX không còn ở trạng thái new."
    if row.get("paymentStatus") not in {"paid", "unreceived"}:
        return "OKX chưa ghi nhận trạng thái paid/unreceived."
    if row.get("isFrozen") is not False:
        return "Lệnh bị đóng băng hoặc API chưa xác nhận isFrozen=false."
    if str(row.get("disputeStatus")) != "0":
        return "Lệnh có tranh chấp hoặc API chưa xác nhận disputeStatus=0."
    return None


async def notify_attempt(db, attempt, order, reason=None):
    if reason:
        save_diagnostic(db, attempt, reason)
    if attempt.state == attempt.notified_state or attempt.state == "READY":
        return
    diagnostic = db.get(OKXReleaseDiagnostic, attempt.order_id)
    if reason is None and diagnostic and attempt.state != "RELEASED":
        reason = diagnostic.reason
    event = {"RELEASED": "CONFIRMED", "WAITING_BUYER_PAYMENT": "PAYMENT_DETECTED",
        "SUBMITTED": "AUTO_MATCHED"}.get(attempt.state, "REVIEW_REQUIRED")
    text = f"OKX P2P\nMã lệnh: {order.order_code}\nTrạng thái mở khóa: {attempt.state}"
    if reason:
        text += "\nLý do: " + reason
    if attempt.state == "WAITING_BUYER_PAYMENT":
        text += "\nChưa gửi yêu cầu mở khóa."
    elif attempt.state == "SUBMITTED":
        text += "\nOKX đã tiếp nhận yêu cầu; hệ thống đang chờ xác nhận hoàn tất."
    elif attempt.state != "RELEASED":
        text += "\nCần kiểm tra trên OKX. Không tự gửi lại yêu cầu mở khóa."
    if await notify_event(event, text):
        attempt.notified_state = attempt.state
        db.commit()


async def process_releases(db, client):
    # The false flag prevents even preflight requests and notification side effects.
    if not settings.okx_auto_release_enabled:
        return
    for match in db.scalars(select(PaymentMatch).where(PaymentMatch.decision == "AUTO_MATCHED")).all():
        order, tx = db.get(P2POrder, match.order_id), db.get(BankTransaction, match.transaction_id)
        if db.get(OKXOrderReceipt, order.order_code) is None:
            continue  # Only orders obtained from authenticated OKX API.
        attempt = db.get(OKXReleaseAttempt, order.id)
        if attempt is None:
            if order.status != "PAYMENT_DETECTED" or not satisfies_payment_rules(order, tx):
                continue
            attempt = OKXReleaseAttempt(order_id=order.id, transaction_id=tx.id, state="READY")
            db.add(attempt)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                continue
        if attempt.state in {"READY", "WAITING_BUYER_PAYMENT"}:
            # Claim before any network request; a second worker cannot POST this order.
            claimed = db.execute(update(OKXReleaseAttempt).where(
                OKXReleaseAttempt.order_id == order.id,
                OKXReleaseAttempt.state.in_(["READY", "WAITING_BUYER_PAYMENT"])
            ).values(state="SUBMITTING"))
            db.commit()
            if claimed.rowcount != 1:
                continue
            db.refresh(attempt)
            try:
                row = await detail(client, order.order_code)
                # Waiting is allowed only when unpaid is the sole blocking condition.
                if row.get("paymentStatus") == "unpaid" and preflight_reason(
                    order, tx, dict(row, paymentStatus="paid")
                ) is None:
                    attempt.state = "WAITING_BUYER_PAYMENT"
                    db.commit()
                    await notify_attempt(db, attempt, order,
                        "Đã khớp tiền ngân hàng; đang chờ người mua bấm Đã thanh toán trên OKX. Hệ thống sẽ kiểm tra lại.")
                    continue
                reason = preflight_reason(order, tx, row)
                if reason:
                    attempt.state = "REVIEW_REQUIRED"
                    db.commit()
                    await notify_attempt(db, attempt, order, reason)
                    continue
            except Exception as error:
                attempt.state = "REVIEW_REQUIRED"
                db.commit()
                await notify_attempt(db, attempt, order,
                    "Không đọc được chi tiết lệnh: " + failure_reason(error))
                continue
            # Persist uncertainty before POST. Crash/timeout cannot cause automatic resubmission.
            diagnostic = db.get(OKXReleaseDiagnostic, attempt.order_id)
            if diagnostic:
                db.delete(diagnostic)
            attempt.state = "UNKNOWN"
            db.commit()
            try:
                result = await request(client, "POST", "/api/v5/p2p/order/release-crypto",
                    {"orderId": order.order_code, "verificationType": "2", "amount": str(tx.amount)})
                if str(result.get("orderId")) == order.order_code:
                    attempt.state = "SUBMITTED"
                    db.commit()
                else:
                    save_diagnostic(db, attempt, "OKX trả mã lệnh khác hoặc thiếu mã lệnh; chưa xác định kết quả mở khóa.")
            except Exception as error:
                save_diagnostic(db, attempt, failure_reason(error))
        if attempt.state in {"SUBMITTING", "SUBMITTED", "UNKNOWN"}:
            try:
                row = await detail(client, order.order_code)
                if consistent(order, row) and row.get("orderStatus") == "completed" and row.get("paymentStatus") == "confirmed":
                    attempt.state = "RELEASED"
                    order.status = "RELEASED"
                    db.commit()
            except Exception:
                pass
        await notify_attempt(db, attempt, order)

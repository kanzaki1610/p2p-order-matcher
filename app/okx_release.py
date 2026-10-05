"""Release coordinator. Disabled by default; test with an injected mock transport."""
import base64
import hashlib
import hmac
import json
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


async def request(client, method, path, body=None):
    timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    encoded = json.dumps(body, separators=(",", ":")) if body is not None else ""
    signature = base64.b64encode(hmac.new(settings.okx_api_secret.encode(),
        (timestamp + method + path + encoded).encode(), hashlib.sha256).digest()).decode()
    response = await client.request(method, settings.okx_base_url.rstrip("/") + path,
        content=encoded or None, headers={"OK-ACCESS-KEY": settings.okx_api_key,
        "OK-ACCESS-SIGN": signature, "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": settings.okx_api_passphrase, "Content-Type": "application/json"})
    if response.status_code != 200:
        raise RuntimeError("OKX request rejected")
    payload = response.json()
    if str(payload.get("code")) != "0":
        raise RuntimeError("OKX API rejected request")
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
    if attempt.state == attempt.notified_state or attempt.state == "READY":
        return
    event = "CONFIRMED" if attempt.state == "RELEASED" else "REVIEW_REQUIRED"
    text = f"OKX P2P\nMã lệnh: {order.order_code}\nTrạng thái mở khóa: {attempt.state}"
    if reason:
        text += "\nLý do: " + reason
    if attempt.state != "RELEASED":
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
        if attempt.state == "READY":
            # Claim before any network request; a second worker cannot POST this order.
            claimed = db.execute(update(OKXReleaseAttempt).where(
                OKXReleaseAttempt.order_id == order.id, OKXReleaseAttempt.state == "READY"
            ).values(state="SUBMITTING"))
            db.commit()
            if claimed.rowcount != 1:
                continue
            db.refresh(attempt)
            try:
                row = await detail(client, order.order_code)
                reason = preflight_reason(order, tx, row)
                if reason:
                    attempt.state = "REVIEW_REQUIRED"
                    db.commit()
                    await notify_attempt(db, attempt, order, reason)
                    continue
            except Exception:
                attempt.state = "REVIEW_REQUIRED"
                db.commit()
                await notify_attempt(db, attempt, order,
                    "Không đọc được chi tiết lệnh OKX; cần kiểm tra kết nối hoặc quyền API.")
                continue
            # Persist uncertainty before POST. Crash/timeout cannot cause automatic resubmission.
            attempt.state = "UNKNOWN"
            db.commit()
            try:
                result = await request(client, "POST", "/api/v5/p2p/order/release-crypto",
                    {"orderId": order.order_code, "verificationType": "2", "amount": str(tx.amount)})
                if str(result.get("orderId")) == order.order_code:
                    attempt.state = "SUBMITTED"
                    db.commit()
            except Exception:
                pass
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

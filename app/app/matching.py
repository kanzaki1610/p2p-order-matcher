import re
import unicodedata
from datetime import timedelta, timezone
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import BankTransaction, P2POrder, PaymentMatch


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFD", value.upper())
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^A-Z0-9]", "", value)


def name_similarity(left: str | None, right: str | None) -> float:
    a, b = normalize_text(left), normalize_text(right)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def contains_five_digit_reference(description: str | None, reference: str | None) -> bool:
    if not description or not reference or not re.fullmatch(r"\d{5}", reference):
        return False
    return re.search(rf"(?<!\d){re.escape(reference)}(?!\d)", description) is not None


def evaluate(order: P2POrder, tx: BankTransaction) -> tuple[int, list[str]]:
    score, reasons = 0, []
    if order.fiat_amount == tx.amount:
        score += 50
        reasons.append("Số tiền khớp chính xác +50")

    similarity = name_similarity(order.counterparty_name, tx.sender_name)
    if similarity >= 0.92:
        score += 25
        reasons.append(f"Tên người chuyển khớp {similarity:.0%} +25")
    elif similarity >= 0.75:
        score += 18
        reasons.append(f"Tên người chuyển gần khớp {similarity:.0%} +18")
    elif tx.sender_name:
        reasons.append(f"Tên người chuyển chỉ khớp {similarity:.0%}")
    else:
        reasons.append("Ngân hàng không trả tên người chuyển")

    description = normalize_text(tx.description)
    normalized_name = normalize_text(order.counterparty_name)
    if normalized_name and normalized_name in description:
        score += 25
        reasons.append("Tên người thanh toán có trong nội dung +25")

    if normalize_text(order.order_code) in description:
        score += 40
        reasons.append("Nội dung có đầy đủ ID lệnh +40")
    elif contains_five_digit_reference(tx.description, order.payment_note):
        score += 30
        reasons.append("Nội dung có đúng 5 số cuối ID lệnh +30")

    if order.expected_bank and order.expected_bank == tx.bank:
        score += 5
        reasons.append("Đúng ngân hàng dự kiến +5")

    return min(score, 100), reasons


def match_transaction(db: Session, tx: BankTransaction) -> tuple[str, P2POrder | None, int, list[str]]:
    occurred = tx.occurred_at
    if occurred.tzinfo is not None:
        occurred = occurred.astimezone(timezone.utc).replace(tzinfo=None)
    window_start = occurred - timedelta(minutes=settings.match_window_minutes)
    window_end = occurred + timedelta(minutes=15)
    candidates = db.scalars(
        select(P2POrder).where(
            P2POrder.status == "WAITING_PAYMENT",
            P2POrder.fiat_amount == tx.amount,
            P2POrder.created_at >= window_start,
            P2POrder.created_at <= window_end,
        )
    ).all()

    if not candidates:
        tx.status = "UNMATCHED"
        db.commit()
        return "UNMATCHED", None, 0, ["Không có đơn chờ nào cùng số tiền trong cửa sổ thời gian"]

    ranked = sorted(((*evaluate(order, tx), order) for order in candidates), key=lambda x: x[0], reverse=True)
    best_score, reasons, best_order = ranked[0]
    ambiguous = len(ranked) > 1 and ranked[1][0] >= best_score - 5

    if best_score >= settings.auto_match_threshold and not ambiguous:
        decision = "AUTO_MATCHED"
        best_order.status = "PAYMENT_DETECTED"
        tx.status = decision
    elif best_score >= settings.review_threshold:
        decision = "REVIEW_REQUIRED"
        tx.status = decision
        if ambiguous:
            reasons.append("Có nhiều đơn có điểm gần bằng nhau; bắt buộc kiểm tra thủ công")
    else:
        decision = "UNMATCHED"
        tx.status = decision

    db.add(PaymentMatch(order=best_order, transaction=tx, score=best_score, decision=decision, reasons=" | ".join(reasons)))
    db.commit()
    return decision, best_order, best_score, reasons

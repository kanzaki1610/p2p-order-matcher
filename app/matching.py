import re
import unicodedata
from collections import Counter
from datetime import timedelta, timezone
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import BankTransaction, P2POrder, PaymentMatch


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFD", value.upper().replace("Đ", "D"))
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


def word_tokens(value: str | None) -> list[str]:
    value = unicodedata.normalize("NFD", (value or "").upper().replace("Đ", "D"))
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
    return re.findall(r"[A-Z0-9]+", value)


def same_name(left: str | None, right: str | None) -> bool:
    tokens = word_tokens(left)
    return bool(tokens) and Counter(tokens) == Counter(word_tokens(right))


def memo_contains_name(description: str | None, name: str | None) -> bool:
    expected = Counter(word_tokens(name))
    actual = Counter(word_tokens(description))
    return bool(expected) and all(actual[word] >= count for word, count in expected.items())


def memo_contains_order_code(description: str | None, code: str | None) -> bool:
    parts = word_tokens(code)
    if not parts:
        return False
    text = " ".join(word_tokens(description))
    if len(parts) == 1 and parts[0].isdigit():
        return re.search(r"(?<!\d)" + re.escape(parts[0]) + r"(?!\d)", text) is not None
    # Permit spaces/punctuation in a code, but never a substring of a longer identifier.
    pattern = r"(?<![A-Z0-9])" + r"\s*".join(map(re.escape, parts)) + r"(?![A-Z0-9])"
    return re.search(pattern, text) is not None


def satisfies_payment_rules(order: P2POrder, tx: BankTransaction) -> bool:
    return bool(
        order.fiat_amount == tx.amount
        and (memo_contains_order_code(tx.description, order.order_code)
             or same_name(order.counterparty_name, tx.sender_name)
             or memo_contains_name(tx.description, order.counterparty_name))
        and tx.direction == "CREDIT"
        and order.side == "SELL"
    )


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
    if memo_contains_name(tx.description, order.counterparty_name):
        score += 25
        reasons.append("Tên người thanh toán có trong nội dung +25")

    if memo_contains_order_code(tx.description, order.order_code):
        score += 40
        reasons.append("Nội dung có đầy đủ ID lệnh +40")
    elif contains_five_digit_reference(tx.description, order.payment_note):
        score += 30
        reasons.append("Nội dung có đúng 5 số cuối ID lệnh +30")

    if order.expected_bank and order.expected_bank == tx.bank:
        score += 5
        reasons.append("Đúng ngân hàng dự kiến +5")

    if satisfies_payment_rules(order, tx):
        reasons.append("Đạt quy tắc: số tiền chính xác và mã lệnh hoặc đủ họ tên (cho phép đảo thứ tự), tiền ghi có")
        return 100, reasons
    return min(score, 89), reasons


def match_transaction(db: Session, tx: BankTransaction) -> tuple[str, P2POrder | None, int, list[str]]:
    previous = db.scalar(select(PaymentMatch).where(
        PaymentMatch.transaction_id == tx.id,
        PaymentMatch.decision == "AUTO_MATCHED",
    )) if tx.id is not None else None
    if previous is not None:
        return previous.decision, previous.order, previous.score, [
            f"Giao dịch {tx.transaction_id} đã dùng cho lệnh {previous.order.order_code}; "
            "không được dùng một lần chuyển tiền để mở khóa thêm lệnh khác."]
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
        return "UNMATCHED", None, 0, [
            f"Không có đơn WAITING_PAYMENT cùng số tiền {tx.amount:,.0f} VND trong cửa sổ "
            f"{settings.match_window_minutes} phút trước / 15 phút sau giao dịch. "
            "Kiểm tra số tiền, thời gian, đồng bộ lệnh và lệnh đã được thanh toán trước đó."]

    ranked = sorted(((*evaluate(order, tx), order) for order in candidates), key=lambda x: x[0], reverse=True)
    best_score, reasons, best_order = ranked[0]
    ambiguous = len(ranked) > 1 and ranked[1][0] >= best_score - 5

    eligible = [order for _, _, order in ranked if satisfies_payment_rules(order, tx)]
    if len(eligible) == 1:
        best_order = eligible[0]
        best_score, reasons = evaluate(best_order, tx)
    ambiguous = len(eligible) > 1 or (not eligible and ambiguous)

    if ambiguous:
        conflicts = eligible or [item[2] for item in ranked if item[0] >= best_score - 5]
        same_buyer = all(same_name(conflicts[0].counterparty_name, item.counterparty_name)
                         for item in conflicts)
        labels = "; ".join(f"{item.order_code} ({item.counterparty_name or 'thiếu họ tên'})"
                           for item in conflicts[:10])
        reasons.append(
            f"Không auto: có {len(conflicts)} lệnh chờ cùng số tiền {tx.amount:,.0f} VND"
            + (" của cùng một người mua" if same_buyer else " có kết quả đối chiếu tương đương")
            + f". Mã lệnh liên quan: {labels}. Giao dịch đang xét {tx.transaction_id} "
            f"chỉ ghi có {tx.amount:,.0f} VND một lần; chưa đủ căn cứ xác định tiền thuộc lệnh nào. "
            "Không dùng một giao dịch cho hai lệnh. Kiểm tra từng mã lệnh và lần chuyển tiền riêng.")
    elif not eligible:
        reasons.append("Không auto: số tiền khớp nhưng chưa khớp đầy đủ mã lệnh hoặc họ tên người mua; "
                       "5 số cuối mã lệnh/tên gần giống không đủ điều kiện mở khóa.")

    if eligible and not ambiguous and best_score >= settings.auto_match_threshold:
        decision = "AUTO_MATCHED"
        best_order.status = "PAYMENT_DETECTED"
        tx.status = decision
    elif best_score >= settings.review_threshold:
        decision = "REVIEW_REQUIRED"
        tx.status = decision
        if ambiguous:
            reasons.append("Có nhiều đơn có điểm gần bằng nhau; bắt buộc kiểm tra thủ công")
        elif eligible:
            reasons.append(f"Không auto: điểm {best_score}/100 thấp hơn ngưỡng auto "
                           f"{settings.auto_match_threshold}; kiểm tra cấu hình ngưỡng.")
    else:
        decision = "UNMATCHED"
        tx.status = decision

    db.add(PaymentMatch(order=best_order, transaction=tx, score=best_score, decision=decision, reasons=" | ".join(reasons)))
    db.commit()
    return decision, best_order, best_score, reasons

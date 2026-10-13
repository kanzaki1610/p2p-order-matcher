import re
import unicodedata
from collections import Counter
from datetime import timedelta, timezone
from difflib import SequenceMatcher

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from decimal import Decimal
from types import SimpleNamespace
from sqlalchemy.orm import Session

from .config import settings
from .models import BankTransaction, P2POrder, PaymentMatch, PaymentAllocation


def payment_identity(order, tx):
    return bool(tx.direction == 'CREDIT' and tx.amount > 0 and order.side == 'SELL'
        and (not order.expected_bank or order.expected_bank == tx.bank)
        and (memo_contains_order_code(tx.description, order.order_code)
            or same_name(order.counterparty_name, tx.sender_name)
            or memo_contains_name(tx.description, order.counterparty_name)))


def funding_parts(db, order):
    ids=db.scalars(select(PaymentAllocation.transaction_id).where(PaymentAllocation.order_id==order.id)).all()
    return db.scalars(select(BankTransaction).where(BankTransaction.id.in_(ids)).order_by(BankTransaction.id)).all() if ids else []


def payment_for_order(db, order, tx):
    parts=funding_parts(db,order)
    if not parts: return tx
    values={column.name:getattr(tx,column.name) for column in BankTransaction.__table__.columns}
    values['amount']=Decimal(0)
    if tx.id not in {part.id for part in parts} or len({part.bank for part in parts})!=1:
        return SimpleNamespace(**values)
    for part in parts:
        occurred=part.occurred_at.replace(tzinfo=None) if part.occurred_at.tzinfo is None else part.occurred_at.astimezone(timezone.utc).replace(tzinfo=None)
        created=order.created_at.replace(tzinfo=None) if order.created_at.tzinfo is None else order.created_at.astimezone(timezone.utc).replace(tzinfo=None)
        if not payment_identity(order,part) or not created-timedelta(minutes=15)<=occurred<=created+timedelta(minutes=settings.match_window_minutes):
            return SimpleNamespace(**values)
        if db.scalar(select(PaymentMatch.id).where(PaymentMatch.transaction_id==part.id,PaymentMatch.decision=='AUTO_MATCHED',PaymentMatch.order_id!=order.id)):
            return SimpleNamespace(**values)
    values['amount']=sum((part.amount for part in parts),Decimal(0))
    return SimpleNamespace(**values)


def reserve_payment(db, order, tx):
    allocation=db.get(PaymentAllocation,tx.id)
    if allocation is not None and allocation.order_id!=order.id:
        return 'REVIEW_REQUIRED',order,0,['Khoản chuyển này đã được giữ cho lệnh khác; không cộng lại.']
    parts=funding_parts(db,order)
    if parts and any(part.bank!=tx.bank for part in parts):
        tx.status='REVIEW_REQUIRED';db.commit()
        return 'REVIEW_REQUIRED',order,0,['Các khoản chuyển khác ngân hàng nhận; cần kiểm tra trước khi cộng tiền.']
    if allocation is None:
        db.add(PaymentAllocation(transaction_id=tx.id,order_id=order.id))
        try: db.flush()
        except IntegrityError:
            db.rollback()
            return match_transaction(db,tx)
    payment=payment_for_order(db,order,tx)
    total=payment.amount
    if total<=0:
        tx.status='REVIEW_REQUIRED';db.commit()
        return 'REVIEW_REQUIRED',order,0,['Chưa xác minh được đầy đủ các khoản chuyển thuộc cùng lệnh.']
    if total<order.fiat_amount:
        decision='PARTIAL_PAYMENT';score=0
        reasons=[f'Đã nhận {total:,.0f}/{order.fiat_amount:,.0f} VND; còn thiếu {order.fiat_amount-total:,.0f} VND. Chờ chuyển thêm.']
    elif settings.auto_match_threshold>100:
        decision='REVIEW_REQUIRED';score=100
        reasons=['Cấu hình ngưỡng auto vượt giới hạn; chưa cho tự động mở khóa.']
    else:
        claimed=db.execute(update(P2POrder).where(P2POrder.id==order.id,P2POrder.status=='WAITING_PAYMENT').values(status='PAYMENT_DETECTED'))
        if claimed.rowcount!=1:
            db.commit();db.refresh(order)
            existing=db.scalar(select(PaymentMatch).where(PaymentMatch.order_id==order.id,PaymentMatch.decision=='AUTO_MATCHED'))
            if existing: return 'AUTO_MATCHED',order,100,['Đã đối chiếu đủ tiền cho lệnh này; không tạo thêm kết quả khớp.']
            return 'REVIEW_REQUIRED',order,0,['Lệnh đã thay đổi trạng thái; cần kiểm tra thủ công.']
        decision='AUTO_MATCHED';score=100
        ids=db.scalars(select(PaymentAllocation.transaction_id).where(PaymentAllocation.order_id==order.id)).all()
        db.execute(update(BankTransaction).where(BankTransaction.id.in_(ids)).values(status='AUTO_MATCHED'))
        reasons=[f'Số tiền đủ: nhận {total:,.0f}/{order.fiat_amount:,.0f} VND từ {len(ids)} khoản chuyển; dư {total-order.fiat_amount:,.0f} VND. Mã lệnh hoặc họ tên đầy đủ khớp.']
        db.refresh(order)
    tx.status=decision
    record=db.scalar(select(PaymentMatch).where(PaymentMatch.transaction_id==tx.id,PaymentMatch.order_id==order.id).order_by(PaymentMatch.id.desc()))
    if record is None: record=PaymentMatch(order=order,transaction=tx,score=score,decision=decision,reasons=' | '.join(reasons));db.add(record)
    else: record.score=score;record.decision=decision;record.reasons=' | '.join(reasons)
    db.commit()
    return decision,order,score,reasons


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
        tx.amount >= order.fiat_amount
        and (memo_contains_order_code(tx.description, order.order_code)
             or same_name(order.counterparty_name, tx.sender_name)
             or memo_contains_name(tx.description, order.counterparty_name))
        and tx.direction == "CREDIT"
        and order.side == "SELL"
        and (not order.expected_bank or order.expected_bank == tx.bank)
    )


def evaluate(order: P2POrder, tx: BankTransaction) -> tuple[int, list[str]]:
    score, reasons = 0, []
    if tx.amount >= order.fiat_amount:
        score += 50
        if tx.amount == order.fiat_amount:
            reasons.append("Số tiền khớp chính xác +50")
        else:
            reasons.append(f"Số tiền đủ: nhận {tx.amount:,.0f} VND ≥ lệnh {order.fiat_amount:,.0f} VND; "
                           f"dư {tx.amount - order.fiat_amount:,.0f} VND +50")

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
        reasons.append("Đạt quy tắc: tiền nhận ≥ tiền lệnh và mã lệnh hoặc đủ họ tên (cho phép đảo thứ tự), tiền ghi có")
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
    allocated=db.get(PaymentAllocation,tx.id) if tx.id is not None else None
    if allocated:
        assigned=db.get(P2POrder,allocated.order_id)
        if assigned.status=='WAITING_PAYMENT': return reserve_payment(db,assigned,tx)
        return tx.status,assigned,100 if tx.status=='AUTO_MATCHED' else 0,['Khoản chuyển đã gắn với lệnh này; không dùng cho lệnh khác.']
    occurred = tx.occurred_at
    if occurred.tzinfo is not None:
        occurred = occurred.astimezone(timezone.utc).replace(tzinfo=None)
    window_start = occurred - timedelta(minutes=settings.match_window_minutes)
    window_end = occurred + timedelta(minutes=15)
    candidates = db.scalars(
        select(P2POrder).where(
            P2POrder.status == "WAITING_PAYMENT",
            P2POrder.side == 'SELL',
            P2POrder.created_at >= window_start,
            P2POrder.created_at <= window_end,
        )
    ).all()

    identified=[order for order in candidates if payment_identity(order,tx)]
    coded=[order for order in identified if memo_contains_order_code(tx.description,order.order_code)]
    identified=coded or identified
    if len(identified)==1: return reserve_payment(db,identified[0],tx)
    if len(identified)>1:
        tx.status='REVIEW_REQUIRED'
        same_buyer=all(same_name(identified[0].counterparty_name,order.counterparty_name) for order in identified)
        reason=f'Có {len(identified)} lệnh'+(' của cùng một người mua' if same_buyer else '')+' có thể nhận khoản tiền này: '+', '.join(order.order_code for order in identified[:10])+f'. Giao dịch {tx.transaction_id}: {tx.amount:,.0f} VND; chưa xác định thuộc lệnh nào. Không dùng một giao dịch cho hai lệnh.'
        if all(order.fiat_amount<=tx.amount for order in identified):reason+=' Các lệnh đều có số tiền không vượt quá tiền nhận.'
        record=db.scalar(select(PaymentMatch).where(PaymentMatch.transaction_id==tx.id).order_by(PaymentMatch.id.desc()))
        if record is None:db.add(PaymentMatch(order=identified[0],transaction=tx,score=89,decision='REVIEW_REQUIRED',reasons=reason))
        else: record.decision='REVIEW_REQUIRED';record.reasons=reason
        db.commit();return 'REVIEW_REQUIRED',identified[0],89,[reason]

    if not candidates:
        tx.status = "UNMATCHED"
        db.commit()
        return "UNMATCHED", None, 0, [
            f"Không có lệnh bán đang chờ trong cửa sổ "
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
        labels = "; ".join(f"{item.order_code} ({item.counterparty_name or 'thiếu họ tên'}, {item.fiat_amount:,.0f} VND)"
                           for item in conflicts[:10])
        amount_label = (f"cùng số tiền {conflicts[0].fiat_amount:,.0f} VND"
                       if all(item.fiat_amount == conflicts[0].fiat_amount for item in conflicts)
                       else f"có số tiền không vượt quá tiền nhận {tx.amount:,.0f} VND")
        reasons.append(
            f"Không auto: có {len(conflicts)} lệnh chờ {amount_label}"
            + (" của cùng một người mua" if same_buyer else " có kết quả đối chiếu tương đương")
            + f". Mã lệnh liên quan: {labels}. Giao dịch đang xét {tx.transaction_id} "
            f"chỉ ghi có {tx.amount:,.0f} VND một lần; chưa đủ căn cứ xác định tiền thuộc lệnh nào. "
            "Không dùng một giao dịch cho hai lệnh. Kiểm tra từng mã lệnh và lần chuyển tiền riêng.")
    elif not eligible:
        if best_order.expected_bank and best_order.expected_bank!=tx.bank:
            reasons.append(f'Không auto: ngân hàng nhận {tx.bank} khác ngân hàng lệnh {best_order.expected_bank}.')
        else:
            reasons.append((f'Không auto: còn thiếu {best_order.fiat_amount-tx.amount:,.0f} VND; ' if tx.amount<best_order.fiat_amount else 'Không auto: ')+"chưa khớp đầy đủ mã lệnh hoặc họ tên người mua; "
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

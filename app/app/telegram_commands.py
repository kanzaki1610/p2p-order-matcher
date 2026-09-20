import re
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import P2POrder


HELP_TEXT = """LỆNH BOT P2P

/don MÃ_ĐƠN | SỐ_TIỀN | TÊN_NGƯỜI_MUA | NGÂN_HÀNG
Ví dụ: /don P2P003 | 1000000 | NGUYEN VAN A | MB

/danhsach - 10 đơn gần nhất
/chitiet MÃ_ĐƠN - xem chi tiết
/huy MÃ_ĐƠN - hủy đơn đang chờ
/help - xem hướng dẫn

Ngân hàng hỗ trợ: MB, VIB, VPBANK
Bot chỉ khớp và báo; không tự release USDT."""


def normalize_amount(value: str) -> Decimal:
    clean = re.sub(r"[.,\s]", "", value)
    if not clean.isdigit():
        raise ValueError("Số tiền không hợp lệ")
    try:
        amount = Decimal(clean)
    except InvalidOperation as exc:
        raise ValueError("Số tiền không hợp lệ") from exc
    if amount <= 0:
        raise ValueError("Số tiền phải lớn hơn 0")
    return amount


def parse_create_order(text: str) -> tuple[str, Decimal, str, str]:
    _, _, arguments = text.partition(" ")
    parts = [part.strip() for part in arguments.split("|")]
    if len(parts) != 4:
        raise ValueError("Sai cú pháp. Ví dụ: /don P2P003 | 1000000 | NGUYEN VAN A | MB")
    order_code, amount_text, counterparty_name, bank = parts
    order_code = order_code.upper()
    bank = bank.upper()
    if not re.fullmatch(r"[A-Z0-9_-]{3,100}", order_code):
        raise ValueError("Mã đơn chỉ dùng chữ, số, dấu _ hoặc -")
    if len(counterparty_name) < 2:
        raise ValueError("Tên người mua quá ngắn")
    if bank not in {"MB", "VIB", "VPBANK"}:
        raise ValueError("Ngân hàng chỉ nhận MB, VIB hoặc VPBANK")
    return order_code, normalize_amount(amount_text), counterparty_name, bank


def format_order(order: P2POrder) -> str:
    return (
        f"Mã đơn: {order.order_code}\n"
        f"Số tiền: {order.fiat_amount:,.0f} VND\n"
        f"Người mua: {order.counterparty_name}\n"
        f"Ngân hàng: {order.expected_bank or 'Không chỉ định'}\n"
        f"Trạng thái: {order.status}\n"
        f"Tạo lúc: {order.created_at.isoformat()}"
    )


def handle_command(text: str, db: Session) -> str:
    command = text.strip().split(maxsplit=1)[0].split("@", 1)[0].lower()
    if command in {"/start", "/help"}:
        return HELP_TEXT

    if command == "/don":
        try:
            order_code, amount, counterparty_name, bank = parse_create_order(text)
        except ValueError as exc:
            return f"❌ {exc}"
        order = P2POrder(
            order_code=order_code,
            fiat_amount=amount,
            counterparty_name=counterparty_name,
            expected_bank=bank,
            payment_note=order_code,
        )
        db.add(order)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return f"❌ Mã đơn {order_code} đã tồn tại"
        db.refresh(order)
        return "✅ ĐÃ TẠO ĐƠN\n" + format_order(order)

    if command == "/danhsach":
        orders = db.scalars(select(P2POrder).order_by(P2POrder.id.desc()).limit(10)).all()
        if not orders:
            return "Chưa có đơn nào."
        lines = ["10 ĐƠN GẦN NHẤT"]
        lines.extend(
            f"{order.order_code} | {order.fiat_amount:,.0f} | {order.status}"
            for order in orders
        )
        return "\n".join(lines)

    if command in {"/chitiet", "/huy"}:
        parts = text.strip().split(maxsplit=1)
        if len(parts) != 2:
            return f"❌ Thiếu mã đơn. Ví dụ: {command} P2P003"
        order_code = parts[1].strip().upper()
        order = db.scalar(select(P2POrder).where(P2POrder.order_code == order_code))
        if not order:
            return f"❌ Không tìm thấy đơn {order_code}"
        if command == "/chitiet":
            return format_order(order)
        if order.status != "WAITING_PAYMENT":
            return f"❌ Không thể hủy đơn đang ở trạng thái {order.status}"
        order.status = "CANCELLED"
        db.commit()
        return f"✅ Đã hủy đơn {order_code}"

    return "❌ Lệnh không hợp lệ. Gửi /help để xem hướng dẫn."

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import OrderConfirmation, OrderRejection, P2POrder


VIETNAM_TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")


HELP_TEXT = """LỆNH BOT P2P

/don MÃ_ĐƠN | SỐ_TIỀN | TÊN_NGƯỜI_MUA | NGÂN_HÀNG
Ví dụ: /don P2P003 | 1000000 | NGUYEN VAN A | MB

/danhsach - 10 đơn gần nhất
/chitiet MÃ_ĐƠN - xem chi tiết
/xacnhan MÃ_ĐƠN - xác nhận đã kiểm tra tiền
/tuchoi MÃ_ĐƠN | LÝ_DO - từ chối giao dịch nghi vấn
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


def format_vietnam_time(value: datetime | None) -> str:
    if value is None:
        return "Không có dữ liệu"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(VIETNAM_TIMEZONE).strftime("%H:%M:%S %d/%m/%Y")


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


def format_order(
    order: P2POrder,
    confirmation: OrderConfirmation | None = None,
    rejection: OrderRejection | None = None,
) -> str:
    result = (
        f"Mã đơn: {order.order_code}\n"
        f"Số tiền: {order.fiat_amount:,.0f} VND\n"
        f"Người mua: {order.counterparty_name}\n"
        f"Ngân hàng: {order.expected_bank or 'Không chỉ định'}\n"
        f"Trạng thái: {order.status}\n"
        f"Tạo lúc: {format_vietnam_time(order.created_at)}"
    )
    if confirmation:
        operator = confirmation.telegram_username or confirmation.telegram_display_name
        operator = f"@{operator}" if confirmation.telegram_username else operator
        result += (
            f"\nXác nhận bởi: {operator or confirmation.telegram_user_id}"
            f"\nXác nhận lúc: {format_vietnam_time(confirmation.confirmed_at)}"
        )
    if rejection:
        operator = rejection.telegram_username or rejection.telegram_display_name
        operator = f"@{operator}" if rejection.telegram_username else operator
        result += (
            f"\nTừ chối bởi: {operator or rejection.telegram_user_id}"
            f"\nTừ chối lúc: {format_vietnam_time(rejection.rejected_at)}"
            f"\nLý do từ chối: {rejection.reason}"
        )
    return result


def handle_command(
    text: str,
    db: Session,
    telegram_user_id: str = "unknown",
    telegram_username: str | None = None,
    telegram_display_name: str | None = None,
) -> str:
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

    if command == "/tuchoi":
        _, _, arguments = text.strip().partition(" ")
        parts = [part.strip() for part in arguments.split("|", 1)]
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return "❌ Sai cú pháp. Ví dụ: /tuchoi P2P003 | Tên người chuyển không đúng"
        order_code = parts[0].upper()
        reason = parts[1]
        if len(reason) < 3:
            return "❌ Lý do từ chối quá ngắn"
        if len(reason) > 500:
            return "❌ Lý do từ chối không được vượt quá 500 ký tự"
        order = db.scalar(select(P2POrder).where(P2POrder.order_code == order_code))
        if not order:
            return f"❌ Không tìm thấy đơn {order_code}"
        if order.status == "REJECTED":
            rejection = db.scalar(
                select(OrderRejection).where(OrderRejection.order_id == order.id)
            )
            suffix = f": {rejection.reason}" if rejection else ""
            return f"ℹ️ Đơn {order_code} đã bị từ chối trước đó{suffix}"
        if order.status != "PAYMENT_DETECTED":
            return f"❌ Chỉ từ chối đơn PAYMENT_DETECTED; trạng thái hiện tại: {order.status}"
        rejection = OrderRejection(
            order_id=order.id,
            reason=reason,
            telegram_user_id=telegram_user_id,
            telegram_username=telegram_username,
            telegram_display_name=telegram_display_name,
        )
        order.status = "REJECTED"
        db.add(rejection)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return f"ℹ️ Đơn {order_code} đã bị từ chối trước đó"
        db.refresh(rejection)
        return (
            "⛔ ĐÃ TỪ CHỐI GIAO DỊCH\n"
            f"Mã đơn: {order_code}\n"
            f"Số tiền: {order.fiat_amount:,.0f} VND\n"
            f"Lý do: {reason}\n"
            f"Từ chối lúc: {format_vietnam_time(rejection.rejected_at)}\n"
            "Hành động: KIỂM TRA THỦ CÔNG — bot không release USDT."
        )

    if command in {"/chitiet", "/huy", "/xacnhan"}:
        parts = text.strip().split(maxsplit=1)
        if len(parts) != 2:
            return f"❌ Thiếu mã đơn. Ví dụ: {command} P2P003"
        order_code = parts[1].strip().upper()
        order = db.scalar(select(P2POrder).where(P2POrder.order_code == order_code))
        if not order:
            return f"❌ Không tìm thấy đơn {order_code}"
        if command == "/chitiet":
            confirmation = db.scalar(
                select(OrderConfirmation).where(OrderConfirmation.order_id == order.id)
            )
            rejection = db.scalar(
                select(OrderRejection).where(OrderRejection.order_id == order.id)
            )
            return format_order(order, confirmation, rejection)
        if command == "/xacnhan":
            if order.status == "CONFIRMED":
                confirmation = db.scalar(
                    select(OrderConfirmation).where(OrderConfirmation.order_id == order.id)
                )
                operator = confirmation.telegram_username if confirmation else None
                suffix = f" bởi @{operator}" if operator else ""
                return f"ℹ️ Đơn {order_code} đã được xác nhận{suffix}"
            if order.status != "PAYMENT_DETECTED":
                return f"❌ Chỉ xác nhận đơn PAYMENT_DETECTED; trạng thái hiện tại: {order.status}"
            confirmation = OrderConfirmation(
                order_id=order.id,
                telegram_user_id=telegram_user_id,
                telegram_username=telegram_username,
                telegram_display_name=telegram_display_name,
            )
            order.status = "CONFIRMED"
            db.add(confirmation)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                return f"ℹ️ Đơn {order_code} đã được xác nhận trước đó"
            db.refresh(confirmation)
            return (
                "✅ ĐÃ XÁC NHẬN THANH TOÁN\n"
                f"Mã đơn: {order_code}\n"
                f"Số tiền: {order.fiat_amount:,.0f} VND\n"
                f"Xác nhận lúc: {format_vietnam_time(confirmation.confirmed_at)}\n"
                "Lưu ý: thao tác này không tự release USDT."
            )
        if order.status != "WAITING_PAYMENT":
            return f"❌ Không thể hủy đơn đang ở trạng thái {order.status}"
        order.status = "CANCELLED"
        db.commit()
        return f"✅ Đã hủy đơn {order_code}"

    return "❌ Lệnh không hợp lệ. Gửi /help để xem hướng dẫn."

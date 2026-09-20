import httpx

from .config import settings
from .models import BankTransaction, P2POrder


async def notify_match(decision: str, order: P2POrder | None, tx: BankTransaction, score: int, reasons: list[str]) -> None:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return
    icon = {"AUTO_MATCHED": "✅", "REVIEW_REQUIRED": "⚠️", "UNMATCHED": "❌"}.get(decision, "ℹ️")
    lines = [
        f"{icon} P2P PAYMENT: {decision}",
        f"Ngân hàng: {tx.bank}",
        f"Mã GD: {tx.transaction_id}",
        f"Số tiền: {tx.amount:,.0f} VND",
        f"Người chuyển: {tx.sender_name or 'Không có dữ liệu'}",
        f"Mã đơn: {order.order_code if order else 'Chưa xác định'}",
        f"Điểm khớp: {score}/100",
        "Lý do: " + "; ".join(reasons),
        "Hành động: KIỂM TRA VÀ XÁC NHẬN THỦ CÔNG — bot không release USDT.",
    ]
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(url, json={"chat_id": settings.telegram_chat_id, "text": "\n".join(lines)})
                if response.is_error:
            # Không ghi URL chứa bot token vào log.
            print(
                f"Telegram gửi thất bại: HTTP {response.status_code} - "
                f"{response.text[:300]}"
            )


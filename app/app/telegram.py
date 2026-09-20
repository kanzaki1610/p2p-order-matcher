import httpx

from .config import settings
from .models import BankTransaction, P2POrder


async def send_telegram_message(chat_id: str, text: str) -> bool:
    if not settings.telegram_bot_token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            response = await client.post(url, json={"chat_id": chat_id, "text": text})
        except httpx.RequestError as exc:
            print(f"Telegram gửi thất bại do lỗi mạng: {type(exc).__name__}")
            return False
        if response.is_error:
            print(f"Telegram gửi thất bại: HTTP {response.status_code} - {response.text[:300]}")
            return False
    return True


async def register_telegram_webhook() -> tuple[bool, str]:
    if not settings.telegram_bot_token:
        return False, "Thiếu TELEGRAM_BOT_TOKEN"
    if not settings.telegram_webhook_secret:
        return False, "Thiếu TELEGRAM_WEBHOOK_SECRET"
    if not settings.public_base_url:
        return False, "Thiếu PUBLIC_BASE_URL"
    api_url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/setWebhook"
    webhook_url = f"{settings.public_base_url.rstrip('/')}/webhooks/telegram"
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            response = await client.post(
                api_url,
                json={
                    "url": webhook_url,
                    "secret_token": settings.telegram_webhook_secret,
                    "allowed_updates": ["message"],
                    "drop_pending_updates": True,
                },
            )
        except httpx.RequestError as exc:
            return False, f"Lỗi mạng: {type(exc).__name__}"
    if response.is_error:
        return False, f"Telegram HTTP {response.status_code}: {response.text[:300]}"
    return True, "Đã đăng ký Telegram webhook"


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
    await send_telegram_message(settings.telegram_chat_id, "\n".join(lines))

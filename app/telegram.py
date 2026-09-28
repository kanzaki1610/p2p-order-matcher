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


def format_payment_notification(tx: BankTransaction, score: int, reasons: list[str]) -> str:
    # Webhook descriptions can repeat the same bank memo after a "|".
    # Keep only the actual transfer memo so the Telegram alert stays concise.
    content = (tx.description or "Không có dữ liệu").split("|", 1)[0].strip()
    amount_matches = False
    name_matches = False
    for reason in reasons:
        normalized = reason.casefold()
        if "số tiền khớp chính xác" in normalized:
            amount_matches = True
        if (
            ("tên người thanh toán" in normalized or "tên người chuyển" in normalized)
            and ("khớp" in normalized or "có trong nội dung" in normalized)
        ):
            name_matches = True

    lines = [
        f"Ngân hàng: {tx.bank}",
        f"Số tiền: {tx.amount:,.0f} VND",
        f"Nội dung: {content[:300]}",
        f"Điểm khớp: {score}/100",
        "Lý do:",
        "Số tiền chính xác" if amount_matches else "Số tiền KHÔNG KHỚP",
        "Họ tên chính xác" if name_matches else "Họ tên KHÔNG KHỚP",
    ]
    return "\n".join(lines)


async def notify_match(decision: str, order: P2POrder | None, tx: BankTransaction, score: int, reasons: list[str]) -> None:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return
    await send_telegram_message(settings.telegram_chat_id, format_payment_notification(tx, score, reasons))

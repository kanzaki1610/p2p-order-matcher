import asyncio
import logging
import re
from urllib.parse import urlsplit

import httpx

from .config import settings

logger = logging.getLogger(__name__)
ROUTES = {
    "PAYMENT_DETECTED": "discord_webhook_payment_detected",
    "AUTO_MATCHED": "discord_webhook_orders",
    "ORDER_CREATED": "discord_webhook_orders",
    "UNMATCHED": "discord_webhook_review_required",
    "REVIEW_REQUIRED": "discord_webhook_review_required",
    "CONFIRMED": "discord_webhook_confirmed",
    "REJECTED": "discord_webhook_review_required",
    "SYSTEM_ALERT": "discord_webhook_system_alerts",
}


def webhook_url(event: str) -> str:
    return getattr(settings, ROUTES.get(event, "discord_webhook_system_alerts")) or settings.discord_webhook_url


def valid_webhook(url: str) -> bool:
    parsed = urlsplit(url)
    return (parsed.scheme == "https" and parsed.hostname == "discord.com"
            and parsed.port in {None, 443} and not parsed.username
            and bool(re.fullmatch(r"/api(?:/v\d+)?/webhooks/\d+/[A-Za-z0-9_-]+", parsed.path)))


def chunks(text: str):
    # Bound by UTF-16 units as well as Python characters; Discord counts emoji.
    current = ""
    units = 0
    for char in text:
        size = len(char.encode("utf-16-le")) // 2
        if units + size > 1900:
            yield current
            current, units = "", 0
        current += char
        units += size
    if current:
        yield current


async def send_discord_message(event: str, text: str) -> bool:
    url = webhook_url(event)
    try:
        valid = valid_webhook(url)
    except ValueError:
        valid = False
    if not valid:
        logger.warning("Discord webhook missing or invalid: event=%s", event)
        return False
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        for part in chunks(text):
            for attempt in range(3):
                try:
                    response = await client.post(url, params={"wait": "true"}, json={
                        "content": part, "username": "P2P Matcher", "allowed_mentions": {"parse": []},
                    })
                except httpx.RequestError:
                    logger.warning("Discord network failure: event=%s", event)
                    return False
                if response.status_code == 429 and attempt < 2:
                    try:
                        delay = min(10, max(0.1, float(response.json().get("retry_after", 1))))
                    except (ValueError, TypeError):
                        delay = 1
                    await asyncio.sleep(delay)
                    continue
                if not response.is_success:
                    # Never log URLs, exception strings, response bodies or tokens.
                    logger.warning("Discord delivery failure: event=%s HTTP=%s", event, response.status_code)
                    return False
                break
    return True

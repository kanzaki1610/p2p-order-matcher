"""Read-only market safety check immediately before authorizing USDT release."""
import importlib
import math
import time
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import httpx

from .config import settings
from .notifications import notify_once, notify_event
from .telegram_commands import format_vietnam_time

MAX_DISCOUNT = Decimal('500')
MAX_AGE = 30


def nickname(value):
    return unicodedata.normalize('NFKC', value).strip().casefold()


def positive(value):
    if isinstance(value, bool):
        raise ValueError('Invalid price')
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError('Invalid price') from None
    if not number.is_finite() or number <= 0:
        raise ValueError('Invalid price')
    return number


def evaluate(data, exchange, order_price, own_names, now=None):
    now = time.time() if now is None else now
    if exchange not in {'OKX', 'MEXC'}:
        raise ValueError('Unsupported exchange')
    names = {nickname(part) for part in own_names.split(',') if part.strip()}
    if not names:
        raise ValueError('Missing merchant nickname')
    expected = exchange.lower() + '_sell'
    if (not isinstance(data, dict) or data.get('ok') is not True or data.get('error')
            or data.get('exchange') != exchange.lower()
            or str(data.get('side')).lower() != 'sell' or data.get('panel') != expected):
        raise ValueError('Wrong market source')
    try:
        saved, age = float(data['saved_ts']), float(data['age_sec'])
        if (not math.isfinite(saved) or not math.isfinite(age)
                or not -5 <= now - saved <= MAX_AGE or not 0 <= age <= MAX_AGE
                or data.get('cache') not in ({'fresh', 'new'} if exchange == 'MEXC' else {'fresh'})):
            raise ValueError('Stale market')
    except (KeyError, TypeError, OverflowError):
        raise ValueError('Missing market timestamp') from None
    rows = data.get('rows')
    if not isinstance(rows, list) or not rows:
        raise ValueError('Empty market')
    reference = None
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('nick'), str) or not row['nick'].strip():
            raise ValueError('Invalid market row')
        price = positive(row.get('price'))
        if price != price.to_integral_value():
            raise ValueError('Invalid VND price')
        if nickname(row['nick']) not in names:
            reference = price
            break
    if reference is None:
        raise ValueError('No other seller')
    actual = positive(order_price)
    difference = reference - actual
    return {'allowed': difference < MAX_DISCOUNT, 'kind': 'SAFE' if difference < MAX_DISCOUNT else 'UNDERPRICED',
        'reference': reference, 'order_price': actual, 'difference': difference,
        'saved_ts': saved}


async def check(exchange, detail):
    if not settings.release_price_guard_enabled:
        return {'allowed': True, 'kind': 'DISABLED'}
    try:
        field = 'unitPrice' if exchange == 'OKX' else 'price'
        price = positive(detail.get(field))
        own_names = getattr(settings, exchange.lower() + '_merchant_nicknames')
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.get('https://autop2p.biz/',
                params={'api': 'panel', 'panel': exchange.lower() + '_sell'})
            response.raise_for_status()
            return evaluate(response.json(), exchange, price, own_names)
    except Exception:
        # Never use remembered/stale price or leak response bodies/URLs.
        return {'allowed': False, 'kind': 'SOURCE_UNAVAILABLE'}


def reason(result):
    if result['kind'] == 'UNDERPRICED':
        return (f"Giá bán lệnh {result['order_price']:,.2f}đ/USDT thấp hơn giá bán đầu tiên của người khác "
            f"{result['reference']:,.0f}đ; lệch {result['difference']:,.2f}đ ≥ 500đ. Đã chặn tự mở khóa lệnh này; cần kiểm tra thủ công.")
    return 'Chưa xác minh được giá bán tham chiếu mới hoặc đơn giá lệnh/biệt danh chính chủ. Tạm chờ kiểm tra giá; chưa gửi yêu cầu mở khóa.'


async def alert(db, exchange, order, result):
    text = f"⚠️ {exchange} · CẢNH BÁO GIÁ\nLệnh: {order.order_code}\n{reason(result)}\nVN: {format_vietnam_time(datetime.now(timezone.utc))}"
    async def send(event, body):
        root = importlib.import_module('app.config').settings if __package__ == 'app_kayhap' else settings
        if (root.discord_bot_token and settings.discord_release_channel_id
                and settings.discord_guild_id and settings.discord_release_allowed_user_ids):
            try:
                async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                    response = await client.post('https://discord.com/api/v10/channels/'
                        + settings.discord_release_channel_id + '/messages',
                        headers={'Authorization': 'Bot ' + root.discord_bot_token},
                        json={'content': body, 'allowed_mentions': {'parse': []}})
                if response.is_success:
                    return True
            except httpx.RequestError:
                pass
        return await notify_event('REVIEW_REQUIRED_' + exchange, body)
    await notify_once(db, f'release-price-guard:{exchange}:{order.id}', 'RELEASE_PRICE_GUARD', text,
        fingerprint=result['kind'], sender=send)

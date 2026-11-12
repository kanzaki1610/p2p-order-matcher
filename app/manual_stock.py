"""Read-only primary manual form stock; never substitutes ad stock for wallet funds."""
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from .okx_release import request


def quantity(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or result >= Decimal('1000000000000000'):
            raise ValueError()
        return result
    except (ValueError, InvalidOperation, TypeError):
        raise ValueError('Số USDT từ sàn thiếu hoặc không hợp lệ.') from None


def text(value):
    return format(value.normalize(), 'f')


async def wallet(client, exchange):
    if exchange != 'OKX':
        return {'supported':False, 'available':None,
                'reason':'Chưa có API số dư ví P2P MEXC được xác minh; không dùng số lượng quảng cáo thay số dư ví.'}
    row = await request(client, 'GET', '/api/v5/p2p/user/balance?currencySymbol=USDT')
    if not isinstance(row, dict) or row.get('currencySymbol') != 'USDT':
        raise ValueError('Chưa xác minh số dư ví P2P đúng USDT.')
    available = quantity(row.get('availableAmount'))
    held = quantity(row.get('onHoldAmount'))
    restricted = row.get('isSecurityLimitApplicable')
    if restricted is not None and type(restricted) is not bool:
        raise ValueError('Chưa xác minh giới hạn số dư P2P.')
    if restricted:
        available = min(available, quantity(row.get('sellOrTransferLimit')))
    return {'supported':True, 'available':text(available), 'held':text(held), 'reason':''}


def fill_quantity(ad, available):
    # Form/API quantity is lifetime ad total; wallet is the NEW remaining amount.
    # Keep completed/reserved history instead of accidentally reducing it again.
    total, remaining, held = (quantity(ad.get(k)) for k in ('quantity','available','frozen'))
    if total < remaining + held:
        raise ValueError('Tổng USDT quảng cáo không khớp lượng còn lại/đang giữ; chưa điền Tất cả.')
    available = quantity(available).quantize(Decimal('.00000001'), rounding=ROUND_DOWN)
    return {'quantity':text(total-remaining+available), 'sell_available':text(available)}

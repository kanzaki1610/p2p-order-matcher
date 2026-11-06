"""Automatic MEXC limits with a VND BUY budget and remaining-stock SELL Max."""
from decimal import Decimal, ROUND_DOWN, ROUND_UP

from .mexc_ads import update_body
from .mexc_buy_budget import remaining
from .mexc_price_state import MexcAutoLimits, amount


async def read_ad_limits(api, row):
    limits = await api.request('/api/v3/fiat/merchant/ads/limit', params={
        'coinId': row['coinId'], 'fiatUnit': 'VND', 'side': row['side']})
    if not isinstance(limits, dict):
        raise ValueError('MEXC chưa trả giới hạn quảng cáo hợp lệ.')
    required = ('minPrice', 'maxPrice', 'minQuantity', 'maxQuantity',
                'minSingleTransAmount', 'maxSingleTransAmount')
    result = {key: amount(limits.get(key)) for key in required}
    if (any(result[key] <= 0 for key in required)
            or any(result[a] > result[b] for a, b in (
                ('minPrice', 'maxPrice'), ('minQuantity', 'maxQuantity'),
                ('minSingleTransAmount', 'maxSingleTransAmount')))):
        raise ValueError('Giới hạn MEXC bị thiếu/mâu thuẫn; chưa gửi yêu cầu.')
    return result


async def prepare(db, api, slot, row, price):
    body = update_body(row, price)
    limits = await read_ad_limits(api, row)
    if not limits['minPrice'] <= price <= limits['maxPrice']:
        raise ValueError('Giá đề xuất nằm ngoài giới hạn quảng cáo MEXC.')
    available, total = amount(row.get('availableQuantity')), amount(row.get('quantity'))
    if available + amount(row.get('frozenQuantity')) > total:
        raise ValueError('Số dư khả dụng/đang giữ vượt số lượng quảng cáo; chưa đổi giá MEXC.')
    lower = amount(row.get('minSingleTransAmount'))
    left = None
    expected_extra = {}
    if slot.key.startswith('BUY:'):
        left = await remaining(db, api, slot, row)
        # A cent-USDT increment is deliberately conservative for BigDecimal input.
        # Round upward so the VND budget remains below the actual stock value.
        wanted = ((max(Decimal(0), left) + 1) / price).quantize(Decimal('.01'), rounding=ROUND_UP) if left > 0 else Decimal(0)
        delta = wanted - available
        if delta > 0:
            body['supplyQuantity'] = format(delta, 'f')
        elif delta < 0:
            body['reduceQuantity'] = format(-delta, 'f')
        next_total = total + delta
        maximum = min(left, limits['maxSingleTransAmount'],
                      (wanted * price).to_integral_value(rounding=ROUND_DOWN) - 1)
    else:
        # The edit form's quantity is remaining sellable stock, whereas the
        # pagination `quantity` includes previously sold/supplied crypto.
        # Send the remaining stock as initQuantity, with no relative stock
        # operations, and require the cumulative quantity to remain unchanged
        # on readback. Never replenish already sold crypto while repricing.
        if available > limits['maxQuantity']:
            raise ValueError('USDT còn bán vượt giới hạn số lượng MEXC; cần điều chỉnh trên sàn trước khi bám giá.')
        body['initQuantity'] = format(available, 'f')
        expected_extra = {'quantity':str(total), 'quantity_mode':'SELL_AVAILABLE',
            'stock_ceiling':str(available + amount(row.get('frozenQuantity')))}
        config = db.get(MexcAutoLimits, slot.key)
        if config and config.ad == slot.ad:
            lower = amount(config.minimum)
        maximum = min(limits['maxSingleTransAmount'],
                      (available * price).to_integral_value(rounding=ROUND_DOWN) - 1)
        next_total = total
    if lower < limits['minSingleTransAmount'] or lower != lower.to_integral_value():
        raise ValueError('Min giao dịch MEXC phải là VND nguyên và đạt mức tối thiểu của sàn.')
    exhausted = maximum < lower or (available if slot.key.startswith('SELL:') else next_total) < limits['minQuantity'] or lower <= 0
    if slot.key.startswith('BUY:') and next_total > limits['maxQuantity']:
        raise ValueError('Số lượng mua tính từ ngân sách vượt giới hạn MEXC; cần giảm ngân sách thủ công.')
    if not exhausted:
        body.update(minSingleTransAmount=format(lower, 'f'), maxSingleTransAmount=format(maximum, 'f'),
                    advStatus=row['advStatus'])
    return {'body': body, 'exhausted': exhausted, 'remaining_vnd': left, 'expected_extra':expected_extra}

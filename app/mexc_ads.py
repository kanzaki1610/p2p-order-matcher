"""Authenticated ad reads and single-attempt MEXC writes."""
from decimal import Decimal
import re

from .okx_ads import number

ADS_PATH = '/api/v3/fiat/merchant/ads/pagination'


def ad_id(value):
    if not re.fullmatch(r'a[0-9]{1,30}', str(value)):
        raise ValueError('ID quảng cáo MEXC phải bắt đầu bằng a và các chữ số.')
    return str(value)


async def my_ads(client, code=None):
    params = {'page': 1, 'limit': 10, 'fiatUnit': 'VND'}
    if code: params['advNo'] = ad_id(code)
    rows = await client.request(ADS_PATH, params=params)
    if not isinstance(rows, list) or any(not isinstance(x, dict) for x in rows):
        raise ValueError('Danh sách quảng cáo MEXC sai cấu trúc.')
    return rows


async def read_ad(client, code, side):
    rows = await my_ads(client, code)
    rows = [r for r in rows if str(r.get('advNo')) == code]
    if len(rows) != 1:
        raise ValueError('Chưa xác minh quảng cáo thuộc tài khoản MEXC này.')
    row = rows[0]
    if row.get('side') != side or row.get('coinName') != 'USDT' or row.get('fiatUnit') != 'VND':
        raise ValueError('Quảng cáo sai chiều hoặc không phải USDT/VND.')
    if row.get('advStatus') not in {'OPEN', 'CLOSE', 'LOW_STOCK'}:
        raise ValueError('Trạng thái quảng cáo MEXC chưa được hỗ trợ.')
    number(row.get('price'))
    return row


def update_body(row, price):
    # Reuse existing values; never add/reduce quantity or change payment accounts.
    required = ('payTimeLimit', 'quantity', 'coinId', 'side', 'fiatUnit',
                'minSingleTransAmount', 'maxSingleTransAmount',
                'userAllTradeCountMin', 'userAllTradeCountMax')
    if any(row.get(k) is None for k in required):
        raise ValueError('Thiếu cấu hình quảng cáo gốc; chưa gửi yêu cầu sửa giá.')
    if row.get('blockTrade') or row.get('requireMobile') or row.get('securityOrderPaymethodIds'):
        raise ValueError('Quảng cáo có điều kiện đặc biệt chưa thể giữ nguyên bằng API; cần kiểm tra thủ công.')
    if row.get('priceType', 0) not in {0, '0'}:
        raise ValueError('Chỉ hỗ trợ quảng cáo giá cố định.')
    price = number(price)
    if price != price.to_integral_value(): raise ValueError('Giá VND phải là số nguyên.')
    result = {k: row[k] for k in required if k != 'quantity'}
    result.update(advNo=ad_id(row['advNo']), initQuantity=str(number(row['quantity'])), price=str(price))
    for key in ('countryCode', 'autoReplyMsg', 'tradeTerms', 'kycLevel', 'maxPayLimit',
                'buyerRegDaysLimit', 'supportKycCountry', 'onlyTradeKybUser',
                'merchantTradeEnable', 'display', 'adsType', 'adDisplayAreaType', 'overVerify'):
        if key in row and row[key] is not None: result[key] = row[key]
    if row['side'] == 'BUY':
        methods = row.get('paymethodIds')
        if not methods: raise ValueError('Thiếu loại phương thức thanh toán quảng cáo mua.')
        result['paymethodIds'] = methods
    else:
        accounts = row.get('paymentInfo')
        if not isinstance(accounts, list) or not accounts or any(not x.get('id') for x in accounts):
            raise ValueError('Thiếu ID tài khoản nhận tiền quảng cáo bán.')
        result['payMethod'] = ','.join(str(x['id']) for x in accounts)
    return {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in result.items()}


async def write_price(client, row, price):
    body = update_body(row, price)
    result = await client._request_once('/api/v3/fiat/merchant/ads/save_or_update', params=body, method='POST')
    if str(result) != str(row['advNo']):
        raise ValueError('Kết quả chỉnh quảng cáo MEXC chưa xác nhận đúng ID; không gửi lại.')


async def write_visibility(client, code, visible):
    await client._request_once('/api/v3/fiat/merchant/ads/state',
        params={'advNo': ad_id(code), 'open': 'true' if visible else 'false'}, method='POST')

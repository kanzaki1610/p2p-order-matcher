"""Strict, read-only payment instructions from authenticated exchange responses."""
import time
from dataclasses import dataclass
from decimal import Decimal
import re

from .matching import same_name
from .vietqr import account_number, integer_amount, resolve_bank


@dataclass(frozen=True)
class PaymentInstruction:
    exchange: str
    code: str
    amount: str
    bank: str
    bank_bin: str
    account: str
    holder: str
    note: str
    deadline: float | None = None


class InactiveOrder(ValueError):
    """A positively verified state that must not show a payable QR."""


def clean_name(value):
    if not isinstance(value, str) or not 2 <= len(value.strip()) <= 100 or any(ord(c) < 32 for c in value):
        raise ValueError('Thiếu tên người nhận xác minh từ sàn; chưa tạo QR.')
    return value.strip()


def absolute_time(value):
    try:
        n = float(value)
        if n > 1e12:
            return n / 1000
        return n if n > 1e9 else None
    except (TypeError, ValueError, OverflowError):
        return None


def instruction(exchange, code, amount, payment, holder, counterparty, deadline=None):
    if deadline is not None and deadline <= time.time():
        raise InactiveOrder('Lệnh đã hết thời hạn thanh toán; không dùng QR cũ.')
    if not isinstance(payment, dict):
        raise ValueError('Chưa có tài khoản nhận tiền được xác minh từ sàn.')
    bank = resolve_bank(payment.get('bankName'), payment.get('bankCode'))
    account = account_number(payment.get('accountNo') or payment.get('account'))
    holder, counterparty = clean_name(holder), clean_name(counterparty)
    if '*' in holder or not same_name(holder, counterparty):
        raise ValueError('Tên chủ tài khoản không khớp người bán xác minh trên sàn; kiểm tra thủ công, chưa tạo QR.')
    note = code if exchange == 'OKX' else code.removeprefix('d')
    if not re.fullmatch(r'\d{5,40}', note):
        raise ValueError('Mã lệnh không hợp lệ; chưa tạo nội dung QR.')
    return PaymentInstruction(exchange, code, integer_amount(amount), bank['short_name'],
                              bank['bin'], account, holder, note, deadline)


def okx_instruction(row, code, counterparty=None):
    if not isinstance(row, dict) or str(row.get('orderId')) != code:
        raise ValueError('Chi tiết OKX không khớp mã lệnh.')
    if str(row.get('side')).upper() != 'BUY' or str(row.get('cryptoCurrency')).upper() != 'USDT' or str(row.get('fiatCurrency')).upper() != 'VND':
        raise ValueError('Không phải lệnh Mua USDT/VND của tài khoản OKX này.')
    if row.get('orderStatus') in {'cancelled', 'completed'} or row.get('paymentStatus') in {'paid', 'confirmed', 'unreceived'}:
        raise InactiveOrder('Lệnh đã hủy, hoàn tất hoặc đã báo thanh toán; QR đã được bỏ.')
    if row.get('orderStatus') != 'new' or row.get('paymentStatus') != 'unpaid':
        raise ValueError('Trạng thái lệnh OKX chưa xác minh là đang chờ bạn thanh toán.')
    if row.get('isFrozen') is not False or str(row.get('disputeStatus')) != '0':
        raise ValueError('Lệnh OKX bị giữ/đang tranh chấp hoặc thiếu trạng thái; chưa tạo QR.')
    # This dedicated authenticated endpoint returns the counterparty's collection
    # method even when the order-detail response omits it. Never use our own
    # maker payment method or an account copied from advertisements/chat text.
    party = counterparty if counterparty is not None else row.get('counterpartyDetail')
    if not isinstance(party, dict):
        raise ValueError('Thiếu thông tin người bán OKX.')
    original = row.get('counterpartyDetail') or {}
    if (not isinstance(original, dict) or not original.get('userId') or
            str(party.get('userId')) != str(original['userId'])):
        raise ValueError('Người nhận OKX không khớp đối tác của lệnh.')
    p = party.get('takerPaymentMethod')
    if not isinstance(p, dict) or str(p.get('type')).upper() != 'BANK' or str(p.get('currency')).upper() != 'VND' or p.get('isDisabled') not in {False, 'false'}:
        raise ValueError('Chưa có phương thức ngân hàng VND hợp lệ của người bán OKX.')
    return instruction('OKX', code, row.get('fiatAmount'), p,
        p.get('accountName'), party.get('realName'), absolute_time(row.get('expiryTimeInSeconds')))


def mexc_instruction(row, code, expected_side):
    if not isinstance(row, dict) or code not in {str(row.get('advNo')), str(row.get('advOrderNo'))}:
        raise ValueError('Chi tiết MEXC không khớp mã lệnh.')
    if row.get('coinName') != 'USDT' or row.get('fiatUnit') != 'VND' or row.get('side') != expected_side:
        raise ValueError('Không phải lệnh Mua USDT/VND đã xác minh từ danh sách maker MEXC.')
    state = row.get('state')
    if state in {'DONE', 'CANCEL', 'CANCELLED', 'INVALID', 'REFUSE', 'TIMEOUT', 'PAID', 'PROCESSING', 'WAIT_PROCESS'}:
        raise InactiveOrder('Lệnh đã hủy, hoàn tất hoặc đã báo thanh toán; QR đã được bỏ.')
    if state != 'NOT_PAID' or row.get('complained') is not False:
        raise ValueError('Trạng thái MEXC chưa xác minh là chờ thanh toán, không tranh chấp.')
    p = row.get('confirmPaymentInfo')
    payments = row.get('paymentInfo')
    if not isinstance(payments, list):
        raise ValueError('Thiếu danh sách tài khoản người nhận MEXC.')
    if isinstance(p, dict) and p:
        selected = [x for x in payments if isinstance(x, dict) and x.get('id') == p.get('id')]
        if len(selected) != 1 or any(selected[0].get(k) != p.get(k) for k in ('bankName','account','payee','payMethod')):
            raise ValueError('Tài khoản được chọn MEXC không khớp danh sách của lệnh.')
    elif len(payments) == 1:
        p = payments[0]
    else:
        raise ValueError('Có nhiều hoặc chưa có tài khoản người bán MEXC; chọn/xác minh trên sàn, không tự chọn tài khoản đầu tiên.')
    if not isinstance(p, dict) or p.get('payMethod') not in {1, '1'}:
        raise ValueError('Phương thức MEXC không phải chuyển khoản ngân hàng.')
    deadline = absolute_time(row.get('payTimeLimit'))
    if deadline is None:
        raise ValueError('Thiếu thời hạn thanh toán MEXC; chưa tạo QR.')
    party = row.get('userInfo') or {}
    return instruction('MEXC', code, row.get('amount'), p, p.get('payee'), party.get('realName'), deadline)

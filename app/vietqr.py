"""Local VietQR encoding. No beneficiary data is sent to a QR image service."""
import binascii
import io
import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path

import qrcode


def folded(value):
    text = unicodedata.normalize('NFD', str(value).upper().replace('Đ', 'D'))
    return re.sub(r'[^A-Z0-9]', '', ''.join(c for c in text if not unicodedata.combining(c)))


BANKS = json.loads(Path(__file__).with_name('vietqr_banks.json').read_text(encoding='utf-8'))
ALIASES = {'MB': 'MBBANK', 'KLB': 'KIENLONGBANK', 'STB': 'SACOMBANK', 'CTG': 'VIETINBANK',
           'LPBANK': 'LIENVIETPOSTBANK'}


def bank_keys(bank):
    legal = folded(bank['name'])
    labels = {folded(bank['short_name']), folded(bank['code'])}
    # Exchanges can append/prepend the same bank's abbreviation to its full
    # legal name. Accept only these exact registered combinations, never a
    # substring, fuzzy name or a different bank's label.
    return ({legal, bank['bin']} | labels
            | {legal + label for label in labels}
            | {label + legal for label in labels})


def resolve_bank(*values):
    matches = set()
    for value in values:
        key = folded(value or '')
        key = ALIASES.get(key, key)
        found = {b['bin'] for b in BANKS if key and key in bank_keys(b)}
        if len(found) > 1:
            raise ValueError('Tên ngân hàng không xác định duy nhất; chưa tạo QR.')
        matches |= found
    if len(matches) != 1:
        raise ValueError('Ngân hàng chưa có mã BIN xác minh hoặc thông tin ngân hàng mâu thuẫn; chưa tạo QR.')
    return next(b for b in BANKS if b['bin'] == next(iter(matches)))


def integer_amount(value):
    try:
        n = Decimal(str(value))
        if not n.is_finite() or n <= 0 or n != n.to_integral_value() or n > 999999999999:
            raise ValueError
        return str(int(n))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('Số tiền VND thiếu hoặc không hợp lệ; chưa tạo QR.') from None


def account_number(value):
    # Keep leading zeroes; do not guess hidden digits or strip punctuation.
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9]{3,34}', value.strip()):
        raise ValueError('Số tài khoản thiếu, bị che hoặc không hợp lệ; chưa tạo QR.')
    return value.strip()


def tlv(tag, text):
    if not text.isascii() or len(text) > 99:
        raise ValueError('Trường VietQR không hợp lệ.')
    return tag + f'{len(text):02}' + text


def payload(bank_bin, account, amount, note=''):
    if not re.fullmatch(r'\d{6}', bank_bin) or not any(b['bin'] == bank_bin for b in BANKS):
        raise ValueError('Mã BIN chưa xác minh.')
    account = account_number(account)
    amount = integer_amount(amount)
    if not isinstance(note,str) or (note and not re.fullmatch(r'[0-9]{5,40}', note)):
        raise ValueError('Nội dung chuyển khoản phải là mã lệnh đã xác minh.')
    beneficiary = tlv('00', bank_bin) + tlv('01', account)
    merchant = tlv('00', 'A000000727') + tlv('01', beneficiary) + tlv('02', 'QRIBFTTA')
    text = '000201010212' + tlv('38', merchant) + '5303704' + tlv('54', amount) + '5802VN'
    # Omit the entire additional-data template when no memo is requested.
    # An empty 62/08 field can override a banking app's own default memo.
    if note:text += tlv('62', tlv('08', note))
    text += '6304'
    return text + f'{binascii.crc_hqx(text.encode("ascii"), 0xFFFF):04X}'


def png_bytes(text):
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=4)
    qr.add_data(text)
    qr.make(fit=True)
    output = io.BytesIO()
    qr.make_image(fill_color='black', back_color='white').save(output, format='PNG')
    return output.getvalue()

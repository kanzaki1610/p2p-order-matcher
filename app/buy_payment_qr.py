"""Primary-account BUY payment QR notifications. Exchange calls are GET-only."""
import asyncio
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import logging
import re
import time
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import Boolean, Numeric, String, select
from sqlalchemy.orm import Mapped, mapped_column

from .config import settings
from .database import Base, SessionLocal
from .buy_payment_data import InactiveOrder, absolute_time, okx_instruction, mexc_instruction
from .vietqr import payload, png_bytes

logger = logging.getLogger('uvicorn.error.buy_payment_qr')
VIEW = 1024


class BuyPaymentQR(Base):
    __tablename__ = 'buy_payment_qr_notifications'
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    exchange: Mapped[str] = mapped_column(String(10))
    code: Mapped[str] = mapped_column(String(60))
    channel: Mapped[str] = mapped_column(String(30))
    message: Mapped[str] = mapped_column(String(30), default='')
    fingerprint: Mapped[str] = mapped_column(String(64), default='')
    created: Mapped[float] = mapped_column(Numeric(20, 3))
    terminal: Mapped[bool] = mapped_column(Boolean, default=False)
    delivery_unknown: Mapped[bool] = mapped_column(Boolean, default=False)


def enabled():
    # No KAYHAP flag can activate this primary-only feature.
    return (__package__ == 'app' and settings.database_schema != 'kayhap'
            and settings.buy_payment_qr_enabled)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def vn_time(value=None):
    return datetime.fromtimestamp(time.time() if value is None else value,
                                  ZoneInfo('Asia/Ho_Chi_Minh')).strftime('%H:%M:%S %d/%m/%Y')


def message_body(info):
    stamp = vn_time()
    fields = [('Lệnh', info.code), ('Ngân hàng', info.bank), ('Số tài khoản', info.account),
              ('Người nhận', info.holder), ('Số tiền', f'{int(info.amount):,} VND'),
              ('Nội dung gợi ý · mã lệnh', info.note)]
    if info.deadline:
        fields.append(('Hạn thanh toán VN', vn_time(info.deadline)))
    return {'content': f'{info.exchange} · MUA USDT · CHỜ BẠN THANH TOÁN',
        'allowed_mentions': {'parse': []},
        'embeds': [{'title': 'QR chuyển khoản cho người bán USDT',
            'description': 'Mở lệnh trên sàn và đối chiếu người nhận, số tiền, nội dung yêu cầu trước khi quét. '
                'Bạn tự xác nhận trong ứng dụng ngân hàng; bot không chuyển tiền hoặc báo đã thanh toán.',
            'fields': [{'name': k, 'value': str(v), 'inline': k not in {'Người nhận','Nội dung gợi ý · mã lệnh'}} for k,v in fields],
            'image': {'url': 'attachment://payment.png'},
            'footer': {'text': 'Đọc API lúc ' + stamp + ' VN · Không dùng QR cũ khi lệnh thay đổi.'}}],
        'attachments': [{'id': 0, 'filename': 'payment.png'}]}


class DiscordDelivery:
    def __init__(self, client):
        self.client = client
        self.headers = {'Authorization': 'Bot ' + settings.discord_bot_token}

    async def validate(self, channel):
        if not re.fullmatch(r'\d{15,25}', channel) or not settings.discord_guild_id:
            raise ValueError('Thiếu kênh QR riêng của tài khoản.')
        r = await self.client.get('https://discord.com/api/v10/channels/' + channel, headers=self.headers)
        if r.status_code != 200:
            raise ValueError('Bot chưa đọc được kênh QR.')
        data = r.json()
        if data.get('guild_id') != settings.discord_guild_id or data.get('type') != 0:
            raise ValueError('Kênh QR sai server hoặc sai loại kênh.')
        overwrites = data.get('permission_overwrites', [])
        everyone = next((x for x in overwrites if x.get('id') == settings.discord_guild_id), {})
        operators = {x.strip() for x in settings.discord_allowed_user_ids.split(',') if x.strip()}
        roles = await self.client.get('https://discord.com/api/v10/guilds/' + settings.discord_guild_id + '/roles', headers=self.headers)
        if roles.status_code != 200:
            raise ValueError('Chưa xác minh vai trò bot trong server.')
        bot_roles = {x['id'] for x in roles.json() if x.get('tags',{}).get('bot_id') == settings.discord_application_id}
        if not int(everyone.get('deny',0)) & VIEW or int(everyone.get('allow',0)) & VIEW:
            raise ValueError('Kênh QR chưa chặn @everyone; không gửi dữ liệu người nhận.')
        for item in overwrites:
            if int(item.get('allow',0)) & VIEW and item.get('id') not in operators | bot_roles | {settings.discord_application_id}:
                raise ValueError('Kênh QR cho phép thành viên/vai trò ngoài operator và bot; chưa gửi.')

    @staticmethod
    def nonce(record):
        return str(int(hashlib.sha256(record.key.encode()).hexdigest()[:16],16))

    async def recover(self, record):
        r = await self.client.get('https://discord.com/api/v10/channels/' + record.channel + '/messages',
                                  headers=self.headers, params={'limit':100})
        if r.status_code != 200:
            return False
        found = [x for x in r.json() if str(x.get('nonce')) == self.nonce(record)
                 and str(x.get('author',{}).get('id')) == settings.discord_application_id]
        if len(found) == 1:
            record.message = str(found[0]['id'])
            record.delivery_unknown = False
            return True
        return False

    async def send(self, db, record, body, image=None):
        if record.delivery_unknown and not await self.recover(record):
            db.commit()
            return False
        path = 'https://discord.com/api/v10/channels/' + record.channel + '/messages'
        method = 'PATCH' if record.message else 'POST'
        if record.message:
            path += '/' + record.message
        else:
            body = {**body, 'nonce': self.nonce(record), 'enforce_nonce': True}
            # A lost HTTP response must not cause another new notification.
            record.delivery_unknown = True
            db.commit()
        try:
            if image is None:
                r = await self.client.request(method, path, headers=self.headers, json=body)
            else:
                r = await self.client.request(method, path, headers=self.headers,
                    data={'payload_json':json.dumps(body,ensure_ascii=False)},
                    files={'files[0]':('payment.png',image,'image/png')})
            if r.status_code not in {200,201}:
                # Explicit rejected creations are retryable; uncertain writes
                # are reconciled by nonce rather than blindly duplicated.
                if not record.message and 400 <= r.status_code < 500:
                    record.delivery_unknown = False
                db.commit()
                logger.warning('BUY QR Discord delivery failed HTTP=%s',r.status_code)
                return False
            record.message = str(r.json()['id'])
            record.delivery_unknown = False
            db.commit()
            return True
        except (httpx.HTTPError,ValueError,KeyError):
            db.commit()
            logger.warning('BUY QR Discord delivery result requires reconciliation')
            return False


def mexc_code(row):
    codes = {str(row.get(k) or '') for k in ('advOrderNo','advNo')}
    orders = [x for x in codes if re.fullmatch(r'd\d{5,30}',x)]
    return orders[0] if len(orders)==1 else None


def mexc_side():
    incoming = (settings.mexc_p2p_api_incoming_side or settings.mexc_p2p_incoming_side).upper()
    if incoming not in {'BUY','SELL'}:
        raise ValueError('Chưa xác minh chiều maker API MEXC.')
    return 'SELL' if incoming=='BUY' else 'BUY'


async def read_payment(client, mexc, record):
    if record.exchange == 'OKX':
        from .okx_release import detail, request
        row = await detail(client, record.code)
        # Do not query beneficiary details for terminal/paid/wrong-side orders.
        party = row.get('counterpartyDetail') or {}
        try:
            return okx_instruction(row,record.code)
        except InactiveOrder:
            raise
        except ValueError:
            if (str(row.get('side')).lower() != 'buy' or row.get('orderStatus') != 'new'
                    or row.get('paymentStatus') != 'unpaid' or row.get('isFrozen') is not False):
                raise
        party = await request(client,'GET','/api/v5/p2p/order/counterparty-user-info?' +
                              urlencode({'orderId':record.code}))
        return okx_instruction(row,record.code,party)
    row = await mexc.request('/api/v3/fiat/order/detail',params={'advOrderNo':record.code})
    # Verify that this is our BUY advertisement, not a payment method belonging
    # to ourselves on a SELL advertisement. Read before exposing any bank data.
    if not isinstance(row,dict) or mexc_code(row)!=record.code:
        raise ValueError('Chi tiết MEXC không khớp mã lệnh.')
    if row.get('state') in {'DONE','CANCEL','CANCELLED','INVALID','REFUSE','TIMEOUT','PAID','PROCESSING','WAIT_PROCESS'}:
        raise InactiveOrder('Lệnh đã hủy, hoàn tất hoặc đã báo thanh toán; QR đã được bỏ.')
    ads = [str(row.get(k) or '') for k in ('advNo','advOrderNo') if re.fullmatch(r'a\d{5,30}',str(row.get(k) or ''))]
    if len(ads)!=1:
        raise ValueError('Chưa xác minh quảng cáo Mua MEXC của lệnh.')
    from .mexc_ads import read_ad
    await read_ad(mexc,ads[0],'BUY')
    return mexc_instruction(row,record.code,mexc_side())


async def discover(db, client, mexc):
    candidates = []
    if settings.okx_p2p_enabled:
        try:
            from .okx_sync import read_orders
            for page in range(1,11):
                rows = await read_orders(client,page)
                candidates += [('OKX',str(x.get('orderId')),absolute_time(x.get('createdTimestamp')))
                    for x in rows if str(x.get('side')).lower()=='buy' and str(x.get('cryptoCurrency')).upper()=='USDT'
                    and str(x.get('fiatCurrency')).upper()=='VND' and x.get('orderStatus')=='new']
                if len(rows)<100:break
        except Exception:
            logger.warning('BUY QR OKX discovery incomplete')
    if settings.mexc_p2p_enabled:
        try:
            from .mexc_sync import _items
            params={'startTime':int((time.time()-86400)*1000),'endTime':int(time.time()*1000),
                    'side':mexc_side(),'orderDealState':'NOT_PAID','limit':100}
            seen=set()
            for _ in range(10):
                rows = _items(await mexc.request('/api/v3/fiat/merchant/order/paginationV2',params=params))
                candidates += [('MEXC',mexc_code(x),absolute_time(x.get('createTime'))) for x in rows
                    if x.get('side')==mexc_side() and x.get('state')=='NOT_PAID'
                    and x.get('coinName')=='USDT' and x.get('fiatUnit')=='VND']
                if len(rows)<100:break
                last=rows[-1];cursor=(str(last.get('createTime')),mexc_code(last))
                if not cursor[0].isdigit() or not cursor[1] or cursor in seen:break
                seen.add(cursor)
                params={**params,'lastCreateTime':int(cursor[0]),'lastId':cursor[1]}
        except Exception:
            logger.warning('BUY QR MEXC discovery incomplete')
    for exchange,code,created in candidates:
        if (not code or not created or not time.time()-86400 <= created <= time.time()+60
                or not re.fullmatch(r'\d{5,40}' if exchange=='OKX' else r'd\d{5,30}',code)):
            continue
        key=exchange+':'+code
        if db.get(BuyPaymentQR,key) is None:
            db.add(BuyPaymentQR(key=key,exchange=exchange,code=code,
                channel=settings.discord_buy_payment_channel_id,created=created))
            db.commit()


async def process(db, client, mexc, delivery, record):
    terminal = False
    try:
        if float(record.created) < time.time()-86400:
            raise InactiveOrder('Lệnh quá 24 giờ; mở sàn kiểm tra, không dùng QR cũ.')
        info=await read_payment(client,mexc,record)
        new=fingerprint(asdict(info))
        if new==record.fingerprint and record.message and not record.delivery_unknown:
            return
        image=png_bytes(payload(info.bank_bin,info.account,info.amount,info.note))
        # Re-read after rendering, before posting a new or changed payable QR.
        if await read_payment(client,mexc,record)!=info:
            raise ValueError('Lệnh/tài khoản nhận tiền vừa thay đổi; chưa tạo QR, chờ kiểm tra lại.')
        body=message_body(info)
    except InactiveOrder as exc:
        terminal=True
        body={'content':f'{record.exchange} · Lệnh {record.code}\n{exc}\nVN: {vn_time()}',
              'embeds':[],'attachments':[],'allowed_mentions':{'parse':[]}}
        image=None
        new=fingerprint({'status':str(exc)})
    except Exception as exc:
        # Only our bounded validation errors are suitable for Discord.
        reason=str(exc) if type(exc) is ValueError else 'Chưa đọc/xác minh được dữ liệu từ sàn. Mở lệnh trên sàn để kiểm tra; không dùng QR cũ.'
        body={'content':f'{record.exchange} · Lệnh {record.code} · CẦN KIỂM TRA\n{reason[:350]}\nVN: {vn_time()}',
              'embeds':[],'attachments':[],'allowed_mentions':{'parse':[]}}
        image=None
        new=fingerprint({'error':reason})
    if new==record.fingerprint and record.message and not record.delivery_unknown:return
    if terminal and not record.message and not record.delivery_unknown:
        record.terminal=True;db.commit();return
    if await delivery.send(db,record,body,image):
        record.fingerprint=new
        record.terminal=terminal
        db.commit()


async def cycle(db, client, mexc):
    if not enabled():return
    delivery=DiscordDelivery(client)
    await delivery.validate(settings.discord_buy_payment_channel_id)
    try:
        await discover(db,client,mexc)
    except Exception:
        logger.warning('BUY QR discovery incomplete; checking existing notifications')
    records=db.scalars(select(BuyPaymentQR).where(BuyPaymentQR.terminal.is_(False))
        .order_by(BuyPaymentQR.created)).all()
    for record in records:
        if record.channel != settings.discord_buy_payment_channel_id:
            # Never redirect an old instruction into a different channel.
            continue
        await process(db,client,mexc,delivery,record)


async def buy_payment_qr_loop():
    from .mexc import MexcP2PClient
    logger.info('Primary BUY payment QR reader started; exchange writes disabled')
    while enabled():
        try:
            async with httpx.AsyncClient(timeout=20,follow_redirects=False) as client, MexcP2PClient() as mexc:
                with SessionLocal() as db:await cycle(db,client,mexc)
        except asyncio.CancelledError:raise
        except Exception:
            logger.warning('BUY payment QR cycle blocked; check private channel/configuration')
        await asyncio.sleep(max(15,settings.buy_payment_qr_seconds))

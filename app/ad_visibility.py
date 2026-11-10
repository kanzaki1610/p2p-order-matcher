"""Primary-account ad lamps, explicit visibility writes and durable no-retry guard."""
import base64
import asyncio
import hashlib
import hmac
import json
import re
import time
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy import Boolean, String, Text, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from .config import settings
from .database import Base
from . import primary_controls
from .models import OKXSlot, OKXAuditLog


class AdVisibilityGuard(Base):
    __tablename__='ad_visibility_guards'
    key: Mapped[str] = mapped_column(String(80),primary_key=True)
    revision: Mapped[str] = mapped_column(String(32),default='0')
    state: Mapped[str] = mapped_column(String(20),default='IDLE')
    wanted: Mapped[bool] = mapped_column(Boolean,default=False)
    side: Mapped[str] = mapped_column(String(4),default='BUY')
    before: Mapped[str] = mapped_column(Text,default='{}')


def quantity(value):
    try: result=Decimal(str(value))
    except Exception: raise ValueError('Chưa xác minh được số USDT còn lại.') from None
    if not result.is_finite() or result<0:raise ValueError('Số USDT còn lại không hợp lệ.')
    return result


def lamp(exchange,row):
    """Never infer visibility from an Auto flag or merely from status=new."""
    if exchange=='OKX':
        available=quantity(row.get('availableAmount'))
        hidden=row.get('isHidden')
        if type(hidden) is not bool:raise ValueError('OKX chưa xác nhận trạng thái hiện/ẩn.')
        active=row.get('status')=='new'
        visible=active and not hidden
    else:
        available=quantity(row.get('availableQuantity'))
        active=row.get('advStatus') in {'OPEN','CLOSE'}
        visible=row.get('advStatus')=='OPEN'
    price=quantity(row.get('unitPrice' if exchange=='OKX' else 'price'))
    minimum=quantity(row.get('minOrderSize' if exchange=='OKX' else 'minSingleTransAmount'))
    if price<=0 or minimum<=0:raise ValueError('Chưa xác minh giá hoặc Min của quảng cáo.')
    enough=available>0 and available*price>=minimum
    on=visible and enough
    reason=('Đang hiển thị' if on else 'Hết USDT còn giao dịch' if available==0 else
            'USDT còn lại không đủ Min giao dịch' if not enough else 'Đang ẩn / ngừng nhận lệnh')
    return {'on':on,'available':str(available),'reason':reason,'can_show':active and enough}


def stop_unavailable(db,exchange,slot,row):
    if not primary_controls.enabled():return False
    state=lamp(exchange,row)
    if state['on']:return False
    if exchange=='OKX':slot.auto_enabled=False
    else:slot.enabled=False
    # A manually hidden/empty ad cannot be reopened by the floor-resume path.
    if exchange=='OKX':
        from .okx_ad_worker import OKXAdPriceAttempt
        attempt=db.get(OKXAdPriceAttempt,slot.id)
        if attempt:attempt.paused_by_floor=False
    else:slot.paused_by_floor=False
    db.add(OKXAuditLog(action='AD_UNAVAILABLE_AUTO_STOP',actor='ad-monitor',detail=exchange+' '+state['reason']))
    db.commit()
    return True


def assert_clear(db,exchange,code,*,lock=False):
    if not primary_controls.enabled():return
    guard=lock_guard(db,exchange,code) if lock else db.get(AdVisibilityGuard,exchange+':'+code,populate_existing=True)
    if guard and guard.state!='IDLE':raise ValueError('Yêu cầu hiện/ẩn chưa rõ kết quả. Bấm Kiểm tra hiển thị; không gửi lại.')


def fingerprint(exchange,row):
    keys=('adId','side','status','isHidden','unitPrice','availableAmount','onHoldAmount','minOrderSize','maxOrderSize') if exchange=='OKX' else ('advNo','side','advStatus','price','quantity','availableQuantity','frozenQuantity','minSingleTransAmount','maxSingleTransAmount')
    return hashlib.sha256(json.dumps({k:row.get(k) for k in keys},sort_keys=True,default=str).encode()).hexdigest()


def token_for(db,exchange,side,code,row):
    guard=db.get(AdVisibilityGuard,exchange+':'+code,populate_existing=True)
    claims={'exchange':exchange,'side':side,'ad':code,'snapshot':fingerprint(exchange,row),
            'revision':guard.revision if guard else '0','expires':int(time.time()+120)}
    data=base64.urlsafe_b64encode(json.dumps(claims,sort_keys=True).encode()).decode().rstrip('=')
    signature=hmac.new(settings.dashboard_admin_key.encode(),data.encode(),hashlib.sha256).hexdigest()
    return data+'.'+signature


def decode_token(token):
    try:
        data,signature=token.split('.')
        wanted=hmac.new(settings.dashboard_admin_key.encode(),data.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature,wanted):raise ValueError()
        claims=json.loads(base64.urlsafe_b64decode(data+'='*(-len(data)%4)))
        if claims['expires']<time.time() or claims['expires']>time.time()+125:raise ValueError()
        code,exchange,side=claims['ad'],claims['exchange'],claims['side']
        if exchange not in {'OKX','MEXC'} or side not in {'BUY','SELL'}:raise ValueError()
        if not re.fullmatch(r'[0-9]{1,30}' if exchange=='OKX' else r'a[0-9]{1,30}',code):raise ValueError()
        return claims
    except Exception:raise ValueError('Bản xem quảng cáo hết hạn hoặc không hợp lệ. Đọc lại danh sách rồi thao tác.') from None


@asynccontextmanager
async def api_for(exchange):
    if exchange=='OKX':
        async with httpx.AsyncClient(timeout=25,follow_redirects=False) as client:yield client
    else:
        from .mexc import MexcP2PClient
        async with MexcP2PClient() as client:yield client


async def read_owned(api,exchange,code,side):
    if exchange=='OKX':
        from .okx_ads import read_ad
        row=await read_ad(api,code)
        if row['side']!=side.lower():raise ValueError('Quảng cáo sai chiều giao dịch.')
        return row
    from .mexc_ads import read_ad
    return await read_ad(api,code,side)


def pause_bound(db,exchange,code):
    """Lock the same slot/attempt rows that pricing writers claim before submission."""
    from .okx_ad_worker import OKXAdPriceAttempt
    from .mexc_pricing import MexcPriceSlot
    if exchange=='OKX':
        slots=db.scalars(select(OKXSlot).where(OKXSlot.adv_id==code).order_by(OKXSlot.id).with_for_update().execution_options(populate_existing=True)).all()
        for slot in slots:
            attempt=db.scalar(select(OKXAdPriceAttempt).where(OKXAdPriceAttempt.slot_id==slot.id).with_for_update().execution_options(populate_existing=True))
            if attempt and attempt.state!='IDLE':raise ValueError('Slot còn yêu cầu chỉnh giá chưa rõ kết quả; kiểm tra/khôi phục slot trước.')
            slot.auto_enabled=False
            if attempt:attempt.paused_by_floor=False
    else:
        slots=db.scalars(select(MexcPriceSlot).where(MexcPriceSlot.ad==code).order_by(MexcPriceSlot.key).with_for_update().execution_options(populate_existing=True)).all()
        for slot in slots:
            if slot.state!='IDLE':raise ValueError('Slot còn yêu cầu chỉnh giá chưa rõ kết quả; kiểm tra/nhận cấu hình trước.')
            slot.enabled=False;slot.paused_by_floor=False;slot.revision=uuid.uuid4().hex


def lock_guard(db,exchange,code):
    key=exchange+':'+code
    query=select(AdVisibilityGuard).where(AdVisibilityGuard.key==key).with_for_update().execution_options(populate_existing=True)
    row=db.scalar(query)
    if row is None:
        try:
            with db.begin_nested():db.add(AdVisibilityGuard(key=key,revision='0',state='IDLE'));db.flush()
        except IntegrityError:pass
        row=db.scalar(query)
    return row


async def set_visibility(db,token,visible):
    if not primary_controls.enabled():raise ValueError('Chỉ tài khoản chính được dùng chức năng này.')
    claims=decode_token(token);exchange,code,side=claims['exchange'],claims['ad'],claims['side']
    allowed=settings.okx_ad_pricing_enabled if exchange=='OKX' else settings.mexc_ad_pricing_enabled and settings.mexc_p2p_live_writes
    if not allowed:raise ValueError('Sàn chưa cho phép ghi quảng cáo trên Render.')
    async with api_for(exchange) as api:
        row=await read_owned(api,exchange,code,side)
        state=lamp(exchange,row)
        if fingerprint(exchange,row)!=claims['snapshot']:raise ValueError('Quảng cáo đã đổi giá, số dư hoặc trạng thái. Đọc lại danh sách trước khi xác nhận.')
        if visible and not state['can_show']:raise ValueError('Quảng cáo hết USDT, không đủ Min hoặc không còn được phép mở. Không tự bổ sung số lượng.')
        guard=lock_guard(db,exchange,code)
        if guard.state!='IDLE':raise ValueError('Yêu cầu hiện/ẩn cũ chưa rõ kết quả. Chỉ kiểm tra lại, không gửi lại.')
        if guard.revision!=claims['revision']:raise ValueError('Bản xác nhận đã dùng hoặc quảng cáo vừa được thao tác. Đọc lại danh sách.')
        pause_bound(db,exchange,code)
        # Read twice before the only POST. Never use visibility to change price/quantity.
        if fingerprint(exchange,await read_owned(api,exchange,code,side))!=claims['snapshot']:raise ValueError('Quảng cáo vừa thay đổi; chưa gửi yêu cầu.')
        guard.revision=uuid.uuid4().hex;guard.wanted=visible;guard.side=side
        guard.before=json.dumps({'price':str(row['unitPrice'] if exchange=='OKX' else row['price'])})
        already=(row['isHidden'] is (not visible) and row['status']=='new') if exchange=='OKX' else row['advStatus']==('OPEN' if visible else 'CLOSE')
        guard.state='IDLE' if already else 'UNKNOWN'
        db.add(OKXAuditLog(action='AD_VISIBILITY_INTENT',actor='web:primary',detail=exchange+' '+code+' visible='+str(visible)))
        operation_revision=guard.revision
        db.commit()  # Durable before POST; workers/manual writers see the guard and Auto is off.
        if already:return 'Quảng cáo đã ở trạng thái yêu cầu. Auto bám giá của quảng cáo vẫn tắt.'
        try:
            if exchange=='OKX':
                from .okx_release import request
                result=await request(api,'POST','/api/v5/p2p/ad/update-active-status',{'adId':code,'status':'show' if visible else 'hidden'})
                if str(result.get('adId'))!=code:raise ValueError('Sàn chưa xác nhận đúng ID quảng cáo.')
            else:
                from .mexc_ads import write_visibility
                await write_visibility(api,code,visible)
            final=await read_owned(api,exchange,code,side)
            guard=lock_guard(db,exchange,code)
            if guard.revision!=operation_revision:
                return 'Hồ sơ hiện/ẩn đã có thao tác mới. Đọc lại danh sách; không ghi đè kết quả mới.'
            return finish_check(db,guard,exchange,final)
        except Exception:
            db.rollback()
            return 'Chưa xác minh được kết quả hiện/ẩn. Auto bám giá đã tắt; bấm Kiểm tra hiển thị. Không tự gửi lại yêu cầu.'


def finish_check(db,guard,exchange,row):
    price=quantity(row['unitPrice'] if exchange=='OKX' else row['price'])
    actual=(row.get('status')=='new' and row.get('isHidden') is False) if exchange=='OKX' else row.get('advStatus')=='OPEN'
    if actual is not guard.wanted or price!=Decimal(json.loads(guard.before)['price']):
        return 'Chưa khớp trạng thái hiện/ẩn đã yêu cầu. Auto vẫn tắt; không gửi lại, kiểm tra trên sàn.'
    guard.state='IDLE'
    db.add(OKXAuditLog(action='AD_VISIBILITY_OBSERVED',actor='web:primary',detail=guard.key))
    db.commit()
    if actual and quantity(row.get('availableAmount' if exchange=='OKX' else 'availableQuantity'))==0:
        return 'Sàn đã xác nhận trạng thái hiện nhưng không còn USDT giao dịch: đèn tối, Auto bám giá vẫn tắt.'
    return 'Đã xác minh quảng cáo '+('đang hiện.' if actual else 'đang ẩn.')+' Auto bám giá vẫn tắt; bạn tự bật khi sẵn sàng.'


async def check_visibility(db,exchange,code,side):
    # Read-only at the exchange, including after a timeout; never resubmit.
    initial=db.get(AdVisibilityGuard,exchange+':'+code,populate_existing=True)
    revision=initial.revision if initial else '0'
    async with api_for(exchange) as api:row=await read_owned(api,exchange,code,side)
    guard=lock_guard(db,exchange,code)
    if guard.revision!=revision:raise ValueError('Yêu cầu hiện/ẩn vừa thay đổi. Kiểm tra lại; không dùng kết quả đọc cũ.')
    if guard.state=='IDLE':return 'Trạng thái sàn: '+lamp(exchange,row)['reason']+'. Không gửi yêu cầu sửa.'
    return finish_check(db,guard,exchange,row)


async def owned_list(api,exchange,side,page=1):
    if exchange=='MEXC':
        from .mexc_ads import ADS_PATH
        batch=await api.request(ADS_PATH,params={'page':page,'limit':10,'fiatUnit':'VND'})
        if not isinstance(batch,list) or any(not isinstance(r,dict) for r in batch):raise ValueError('Danh sách quảng cáo MEXC chưa đúng cấu trúc.')
        return ([r for r in batch if r.get('side')==side and r.get('coinName')=='USDT' and r.get('fiatUnit')=='VND'],len(batch)==10)
    from .okx_sync import sign
    from .okx_release import OKXRequestError,safe_code
    path='/api/v5/p2p/ad/active-list?'+urlencode({'side':side.lower(),'cryptoCurrency':'USDT','fiatCurrency':'VND'})
    timestamp=datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
    response=await api.get(settings.okx_base_url.rstrip('/')+path,headers={
        'OK-ACCESS-KEY':settings.okx_api_key,'OK-ACCESS-TIMESTAMP':timestamp,
        'OK-ACCESS-PASSPHRASE':settings.okx_api_passphrase,
        'OK-ACCESS-SIGN':sign(settings.okx_api_secret,timestamp,path)})
    payload=response.json()
    if response.status_code!=200 or not isinstance(payload,dict) or str(payload.get('code'))!='0':
        raise OKXRequestError(f'OKX HTTP {response.status_code}; mã API: '+safe_code(payload.get('code') if isinstance(payload,dict) else None)+'.')
    rows=payload.get('data')
    if not isinstance(rows,list) or any(not isinstance(r,dict) for r in rows):raise ValueError('Danh sách quảng cáo OKX chưa đúng cấu trúc.')
    return [r for r in rows if r.get('side')==side.lower()],False


async def advertisements(db,exchange,side,page=1):
    from .mexc_pricing import MexcPriceSlot
    from .web_control import safe_error
    result=[]
    async with api_for(exchange) as api:
        rows,has_more=await owned_list(api,exchange,side,page)
        seen=set()
        for row in rows[:100]:
            code=str(row.get('adId' if exchange=='OKX' else 'advNo',''))
            if not re.fullmatch(r'[0-9]{1,30}' if exchange=='OKX' else r'a[0-9]{1,30}',code) or code in seen:continue
            seen.add(code)
            card={'ad_id':code,'side':side,'on':None,'price':None,'available':None,'reason':'Chưa xác minh','token':None,'pending':False,'slots':[]}
            try:
                # OKX's active-list schema can omit isHidden; read owned detail for lamps.
                available=quantity(row.get('availableAmount' if exchange=='OKX' else 'availableQuantity'))
                if exchange=='OKX' and available>0 and type(row.get('isHidden')) is not bool:
                    await asyncio.sleep(.5)
                    row=await read_owned(api,exchange,code,side)
                if exchange=='OKX' and available==0:
                    state={'on':False,'available':'0','reason':'Hết USDT còn giao dịch','can_show':False}
                else:state=lamp(exchange,row)
                card.update(state,price=str(quantity(row.get('unitPrice' if exchange=='OKX' else 'price'))))
                guard=db.get(AdVisibilityGuard,exchange+':'+code,populate_existing=True)
                card['pending']=bool(guard and guard.state!='IDLE')
                if exchange=='MEXC' or type(row.get('isHidden')) is bool:
                    card['token']=token_for(db,exchange,side,code,row)
                slots=(db.scalars(select(OKXSlot).where(OKXSlot.adv_id==code)).all() if exchange=='OKX' else db.scalars(select(MexcPriceSlot).where(MexcPriceSlot.ad==code)).all())
                card['slots']=[{'side':s.side if exchange=='OKX' else s.key.split(':')[0],
                    'slot':s.slot_number if exchange=='OKX' else int(s.key.split(':')[1]),
                    'auto':(s.auto_enabled if exchange=='OKX' else s.enabled) and state['on']} for s in slots]
            except Exception as error:card['reason']=safe_error(error)
            result.append(card)
    result.sort(key=lambda r:(r['on'] is not True,r['available']=='0',-int(r['ad_id'].lstrip('a'))))
    return {'read_at':time.time(),'rows':result,'limited':len(rows)>=100,'page':page,'has_more':has_more and page<100}

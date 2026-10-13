"""Owner-controlled pricing buttons and explicit manual-price confirmation."""
import re
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import httpx
from sqlalchemy import String, Integer, Numeric, DateTime, Boolean, select, update
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base
from .models import OKXSlot
from .config import settings
from .okx_ads import read_ad, number
from .okx_cost import read_last_buy_cost
from .okx_price_controls import execute_price_command
from .okx_ad_worker import OKXAdPriceAttempt, confirm, safe_failure
from .okx_release import request

class ManualPriceDraft(Base):
    __tablename__='okx_manual_price_drafts'
    id: Mapped[str] = mapped_column(String(32),primary_key=True)
    owner: Mapped[str] = mapped_column(String(100))
    slot_id: Mapped[int] = mapped_column(Integer)
    ad_id: Mapped[str] = mapped_column(String(30))
    price: Mapped[Decimal] = mapped_column(Numeric(20,0))
    hidden: Mapped[bool] = mapped_column(Boolean)
    used: Mapped[bool] = mapped_column(Boolean,default=False)
    expires: Mapped[datetime] = mapped_column(DateTime(timezone=True))

def button(label,code,style=2):
    return {'type':2,'style':style,'label':label,'custom_id':'price:'+code}

def panel():
    rows=[]
    for side,label in [('BUY','Mua'),('SELL','Bán')]:
        rows.append({'type':1,'components':[button('Đặt giá '+label,'price:'+side,1),button('Vị trí '+label,'rank:'+side),button('Chọn quảng cáo '+label,'ad:'+side),button('Auto '+label,'auto:'+side)]})
    rows.append({'type':1,'components':[button('Trạng thái','status'),button('Quảng cáo mua','list:BUY'),button('Quảng cáo bán','list:SELL'),button('Tạm dừng bám giá','pause',4),button('Tiếp tục bám giá','resume',3)]})
    return {'content':'**Điều khiển giá OKX**\nChỉ operator được sử dụng. Đặt giá thủ công cần xác nhận và tắt Auto của slot. Tạm dừng không ẩn quảng cáo trên sàn.','components':rows,'allowed_mentions':{'parse':[]}}

def modal(action,side):
    labels={'price':('Đặt giá thủ công','Giá VND/USDT (số nguyên, không dấu phân cách)'), 'rank':('Chọn vị trí','Vị trí từ 1 đến 50'), 'ad':('Chọn quảng cáo','ID quảng cáo OKX')}
    title,label=labels[action]
    fields=[]
    if action in {'price','ad'}:
        fields.append({'type':1,'components':[{'type':4,'custom_id':'slot','label':'Slot 1 hoặc 2','style':1,'required':True,'value':'1','max_length':1}]})
    fields.append({'type':1,'components':[{'type':4,'custom_id':'value','label':label,'style':1,'required':True,'max_length':30}]})
    return {'type':9,'data':{'custom_id':f'price:form:{action}:{side}','title':title+' '+('mua' if side=='BUY' else 'bán'),'components':fields}}

def command(action,**options):
    return {'name':'gia','options':[{'name':action,'options':[{'name':k,'value':v} for k,v in options.items()]}]}

async def manual_preview(db,owner,side,slot_number,price):
    if not settings.okx_ad_pricing_enabled: raise ValueError('Công tắc cho phép sửa giá trên Render đang tắt.')
    if side not in {'BUY','SELL'} or slot_number not in {1,2} or price<=0: raise ValueError('Slot hoặc giá không hợp lệ.')
    slot=db.scalar(select(OKXSlot).where(OKXSlot.side==side,OKXSlot.slot_number==slot_number))
    if slot is None: raise ValueError('Slot chưa được cấu hình.')
    if db.scalar(select(OKXSlot.id).where(OKXSlot.id!=slot.id,OKXSlot.adv_id==slot.adv_id)):
        raise ValueError('ID quảng cáo đang gán cho nhiều slot; cần sửa cấu hình trước.')
    attempt=db.get(OKXAdPriceAttempt,slot.id)
    if attempt and attempt.state!='IDLE': raise ValueError('Yêu cầu cũ chưa rõ kết quả; chưa cho đặt giá mới.')
    async with httpx.AsyncClient(timeout=30) as client:
        ad=await read_ad(client,slot.adv_id)
        if ad['side']!=side.lower() or ad.get('type')!='limit' or ad.get('status')!='new' or type(ad.get('isHidden')) is not bool: raise ValueError('Quảng cáo sai chiều hoặc không còn hoạt động với giá cố định.')
        if side=='SELL':
            cost=await read_last_buy_cost(client)
            if price<number(cost['unit_price'])+10: raise ValueError('Giá bán phải đạt giá mua hoàn tất gần nhất +10đ.')
    draft=ManualPriceDraft(id=uuid.uuid4().hex,owner=owner,slot_id=slot.id,ad_id=slot.adv_id,price=price,hidden=ad['isHidden'],expires=datetime.now(timezone.utc)+timedelta(minutes=5))
    db.add(draft);db.commit()
    return {'content':f'**Xác nhận đặt giá {side} slot {slot_number}**\nID {slot.adv_id}\nGiá hiện tại {ad["unitPrice"]}đ → {price}đ/USDT.\nAuto của slot sẽ tắt; quảng cáo {"giữ ẩn" if ad["isHidden"] else "giữ hiển thị"}.\nXác nhận hết hạn sau 5 phút.','components':[{'type':1,'components':[button('Xác nhận gửi OKX','confirm:'+draft.id,3),button('Hủy','cancel:'+draft.id)]}]}

async def manual_confirm(db,owner,draft_id):
    draft=db.get(ManualPriceDraft,draft_id)
    now=datetime.now(timezone.utc)
    if draft is None or draft.owner!=owner or draft.used: raise ValueError('Xác nhận không hợp lệ hoặc đã dùng.')
    expiry=draft.expires.replace(tzinfo=timezone.utc) if not draft.expires.tzinfo else draft.expires
    if expiry<now or not settings.okx_ad_pricing_enabled: raise ValueError('Xác nhận hết hạn hoặc sửa giá đã bị tắt trên Render.')
    slot=db.get(OKXSlot,draft.slot_id)
    if slot.adv_id!=draft.ad_id: raise ValueError('ID slot đã đổi; hãy xem và xác nhận lại.')
    if db.scalar(select(OKXSlot.id).where(OKXSlot.id!=slot.id,OKXSlot.adv_id==draft.ad_id)):
        raise ValueError('ID quảng cáo đang gán cho nhiều slot; chưa gửi yêu cầu.')
    async with httpx.AsyncClient(timeout=30) as client:
        ad=await read_ad(client,draft.ad_id)
        if ad['side']!=slot.side.lower() or ad.get('type')!='limit' or ad.get('status')!='new' or ad.get('isHidden') is not draft.hidden: raise ValueError('Quảng cáo đã thay đổi; hãy tạo xác nhận mới.')
        if slot.side=='SELL':
            cost=await read_last_buy_cost(client)
            if draft.price<number(cost['unit_price'])+10: raise ValueError('Giá vốn mới không cho phép giá bán này.')
        attempt=db.get(OKXAdPriceAttempt,slot.id)
        if attempt is None:
            attempt=OKXAdPriceAttempt(slot_id=slot.id,state='IDLE');db.add(attempt);db.commit()
        # One attempt per slot shared with the auto worker; never reset UNKNOWN.
        taken=db.execute(update(OKXAdPriceAttempt).where(OKXAdPriceAttempt.slot_id==slot.id,OKXAdPriceAttempt.state=='IDLE').values(state='SUBMITTING',action='MANUAL_PRICE',old_ad_id=draft.ad_id,result_ad_id=None,price=draft.price,hidden=draft.hidden,last_action_at=now,paused_by_floor=False))
        used=db.execute(update(ManualPriceDraft).where(ManualPriceDraft.id==draft.id,ManualPriceDraft.used.is_(False)).values(used=True))
        if taken.rowcount!=1 or used.rowcount!=1: db.rollback();raise ValueError('Slot đang xử lý hoặc xác nhận đã dùng; không gửi lại.')
        db.execute(update(OKXSlot).where(OKXSlot.id==slot.id,OKXSlot.adv_id==draft.ad_id).values(auto_enabled=False))
        db.commit();db.refresh(attempt);db.refresh(slot)
        if slot.adv_id!=draft.ad_id:
            attempt.state='IDLE';db.commit();raise ValueError('Slot đã đổi trước khi gửi; chưa gửi OKX.')
        attempt.state='UNKNOWN';db.commit()
        try:
            result=await request(client,'POST','/api/v5/p2p/ad/update',{'adId':draft.ad_id,'unitPrice':str(draft.price),'online':'false' if draft.hidden else 'true'})
            if str(result.get('oldAdId'))!=draft.ad_id or not re.fullmatch(r'[0-9]{1,30}',str(result.get('newAdId',''))): raise ValueError('Phản hồi chưa xác nhận ID quảng cáo mới.')
            attempt.result_ad_id=str(result['newAdId']);attempt.state='CONFIRMING';db.commit()
            if await confirm(db,client,attempt,slot): return 'Đã xác minh giá thủ công trên OKX. ID mới '+slot.adv_id+' · Auto đã tắt.'
            return 'OKX đã nhận yêu cầu; chưa xác minh trạng thái cuối. Không gửi lại; kiểm tra /gia trangthai.'
        except Exception as error:
            db.rollback();attempt=db.get(OKXAdPriceAttempt,slot.id);attempt.reason=safe_failure(error)[:500];db.commit()
            return 'Chưa xác minh kết quả: '+attempt.reason+' Không tự gửi lại. Auto của slot đã tắt.'

async def execute_panel(payload,db,owner):
    code=payload['data']['custom_id'].split(':')
    action=code[1]
    if action=='form':
        action,side=code[2:4]
        values={x['custom_id']:x.get('value','') for row in payload['data'].get('components',[]) for x in row.get('components',[])}
        value=values.get('value','')
        if not re.fullmatch(r'[0-9]{1,30}',value): raise ValueError('Nhập số nguyên không dấu phân cách.')
        if action=='price': return await manual_preview(db,owner,side,int(values.get('slot','0')),Decimal(value))
        data=command('vitri',chieu=side,vitri=int(value)) if action=='rank' else command('quangcao',chieu=side,slot=int(values.get('slot','0')),id=value)
    elif action=='confirm': return {'content':await manual_confirm(db,owner,code[2]),'components':[]}
    elif action=='cancel':
        db.execute(update(ManualPriceDraft).where(ManualPriceDraft.id==code[2],ManualPriceDraft.owner==owner).values(used=True));db.commit()
        return {'content':'Đã hủy xác nhận; không gửi yêu cầu sửa giá.','components':[]}
    elif action=='status': data=command('trangthai')
    elif action=='list': data=command('danhsach',chieu=code[2])
    elif action in {'pause','resume'}: data=command('bamtat',bat=action=='resume')
    elif action=='auto':
        side=code[2]
        return {'content':'Chọn Auto cho '+side+' (không mở quảng cáo đã ẩn).','components':[{'type':1,'components':[button(f'Slot {n}: '+label,f'setauto:{side}:{n}:{enabled}',style) for n in (1,2) for label,enabled,style in [('Bật',1,3),('Tắt',0,4)]]}]}
    elif action=='setauto': data=command('auto',chieu=code[2],slot=int(code[3]),bat=code[4]=='1')
    else: raise ValueError('Nút không hợp lệ.')
    return {'content':await execute_price_command(data,db,owner),'components':[]}

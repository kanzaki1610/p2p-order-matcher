"""Owner-controlled pricing buttons and explicit manual-price confirmation."""
import re
import uuid
import json
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import httpx
from sqlalchemy import String, Integer, Numeric, DateTime, Boolean, Text, select, update
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base
from .models import OKXSlot, OKXAuditLog
from .config import settings
from .okx_ads import read_ad, number
from .okx_cost import read_last_buy_cost, read_pricing_cost, cost_summary, cost_revision, save_cost
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

class ManualAdTerms(Base):
    __tablename__='okx_manual_ad_terms'
    draft_id: Mapped[str] = mapped_column(String(32),primary_key=True)
    terms_json: Mapped[str] = mapped_column(Text)
    snapshot_json: Mapped[str] = mapped_column(Text)

def input_number(value, money=False):
    value=value.strip().replace(' ','')
    if money and re.fullmatch(r'[0-9]{1,3}(\.[0-9]{3})+',value): value=value.replace('.','')
    elif not money: value=value.replace(',','.')
    if not re.fullmatch(r'[0-9]+(?:\.[0-9]{1,8})?',value): raise ValueError('Nhập số dương; số lượng USDT dùng dấu chấm hoặc phẩy thập phân.')
    result=number(value)
    if money and result!=result.to_integral_value(): raise ValueError('Giá và hạn mức VND phải là số đồng nguyên.')
    if result>=Decimal('1000000000000000'): raise ValueError('Số nhập vượt giới hạn hỗ trợ.')
    return result

def ad_quantity(ad):
    if ad.get('cryptoAmount') is not None: return str(Decimal(str(ad['cryptoAmount'])))
    keys=('availableAmount','completedOrderCryptoAmount','onHoldAmount')
    if any(ad.get(k) is None for k in keys): return None
    parts=[Decimal(str(ad[k])) for k in keys]
    if any(not x.is_finite() or x<0 for x in parts): raise ValueError('Số lượng quảng cáo chưa xác minh được.')
    return str(sum(parts,Decimal(0)))

def ad_snapshot(ad):
    return {'unitPrice':str(number(ad['unitPrice'])), 'minOrderLimit':str(number(ad['minOrderSize'])) if ad.get('minOrderSize') is not None else None,
        'maxOrderLimit':str(number(ad['maxOrderSize'])) if ad.get('maxOrderSize') is not None else None,'cryptoAmount':ad_quantity(ad)}

def button(label,code,style=2):
    return {'type':2,'style':style,'label':label,'custom_id':'price:'+code}

def panel():
    rows=[]
    for side,label in [('BUY','Mua'),('SELL','Bán')]:
        rows.append({'type':1,'components':[button('Đặt giá '+label,'price:'+side,1),button('Vị trí '+label,'rank:'+side),button('Chọn quảng cáo '+label,'ad:'+side),button('Auto '+label,'auto:'+side)]})
    rows.append({'type':1,'components':[button('Trạng thái','status'),button('Quảng cáo mua','list:BUY'),button('Quảng cáo bán','list:SELL'),button('Tạm dừng bám giá','pause',4),button('Tiếp tục bám giá','resume',3)]})
    rows.append({'type':1,'components':[button('Nhập giá vốn','cost:SELL',1)]})
    return {'content':'**Điều khiển giá OKX**\nChỉ operator được sử dụng. Đặt giá thủ công cần xác nhận và tắt Auto của slot. Tạm dừng không ẩn quảng cáo trên sàn.','components':rows,'allowed_mentions':{'parse':[]}}

def modal(action,side):
    labels={'cost':('Nhập giá vốn','Giá mua VND/USDT (ví dụ 26050)'), 'price':('Đặt giá thủ công','Giá VND/USDT (số nguyên, không dấu phân cách)'), 'rank':('Chọn vị trí','Vị trí từ 1 đến 50'), 'ad':('Chọn quảng cáo','ID quảng cáo OKX')}
    title,label=labels[action]
    fields=[]
    if action in {'price','ad'}:
        fields.append({'type':1,'components':[{'type':4,'custom_id':'slot','label':'Slot 1 hoặc 2','style':1,'required':True,'value':'1','max_length':1}]})
    fields.append({'type':1,'components':[{'type':4,'custom_id':'value','label':label,'style':1,'required':True,'max_length':30}]})
    if action=='price':
        for field,label in [('quantity','Số lượng USDT (để trống để giữ nguyên)'),('minimum','Tối thiểu VND/giao dịch (trống: giữ nguyên)'),('maximum','Tối đa VND/giao dịch (trống: giữ nguyên)')]:
            fields.append({'type':1,'components':[{'type':4,'custom_id':field,'label':label,'style':1,'required':False,'max_length':30}]})
    return {'type':9,'data':{'custom_id':f'price:form:{action}:{side}','title':title+' '+('mua' if side=='BUY' else 'bán'),'components':fields}}

def command(action,**options):
    return {'name':'gia','options':[{'name':action,'options':[{'name':k,'value':v} for k,v in options.items()]}]}

async def manual_preview(db,owner,side,slot_number,price,terms=None):
    terms=terms or {}
    if set(terms)-{'cryptoAmount','minOrderLimit','maxOrderLimit'}: raise ValueError('Thông số quảng cáo không hợp lệ.')
    terms={key:str(input_number(str(value),money=key!='cryptoAmount')) for key,value in terms.items()}
    if not settings.okx_ad_pricing_enabled: raise ValueError('Công tắc cho phép sửa giá trên Render đang tắt.')
    if side not in {'BUY','SELL'} or slot_number not in {1,2} or price<=0: raise ValueError('Slot hoặc giá không hợp lệ.')
    slot=db.scalar(select(OKXSlot).where(OKXSlot.side==side,OKXSlot.slot_number==slot_number))
    if slot is None: raise ValueError('Slot chưa được cấu hình.')
    if db.scalar(select(OKXSlot.id).where(OKXSlot.id!=slot.id,OKXSlot.adv_id==slot.adv_id)):
        raise ValueError('ID quảng cáo đang gán cho nhiều slot; cần sửa cấu hình trước.')
    attempt=db.get(OKXAdPriceAttempt,slot.id)
    if attempt and attempt.state!='IDLE': raise ValueError(f'Slot {side} {slot_number} đang gắn ID {slot.adv_id}; yêu cầu cũ chưa rõ kết quả ({attempt.reason or attempt.state}). Bấm Quảng cáo mua/bán rồi Chọn quảng cáo để gắn ID đang hoạt động; không gửi lại vào ID cũ.')
    async with httpx.AsyncClient(timeout=30) as client:
        ad=await read_ad(client,slot.adv_id)
        if ad['side']!=side.lower() or ad.get('type')!='limit' or ad.get('status')!='new' or type(ad.get('isHidden')) is not bool: raise ValueError('Quảng cáo sai chiều hoặc không còn hoạt động với giá cố định.')
        if side=='SELL':
            cost=await read_pricing_cost(db,client,reader=read_last_buy_cost)
            if price<number(cost['unit_price'])+10: raise ValueError('Giá bán phải đạt giá vốn đang chọn +10đ.')
    snapshot=ad_snapshot(ad)
    lower=terms.get('minOrderLimit') or snapshot['minOrderLimit']
    upper=terms.get('maxOrderLimit') or snapshot['maxOrderLimit']
    quantity=terms.get('cryptoAmount') or snapshot['cryptoAmount']
    if lower is not None and upper is not None and Decimal(lower)>Decimal(upper): raise ValueError('Hạn mức tối thiểu không được lớn hơn tối đa.')
    if 'cryptoAmount' in terms and lower is not None and Decimal(lower)>Decimal(quantity)*price: raise ValueError('Số lượng USDT × giá nhỏ hơn hạn mức tối thiểu mỗi giao dịch.')
    draft=ManualPriceDraft(id=uuid.uuid4().hex,owner=owner,slot_id=slot.id,ad_id=slot.adv_id,price=price,hidden=ad['isHidden'],expires=datetime.now(timezone.utc)+timedelta(minutes=5))
    db.add(draft);db.add(ManualAdTerms(draft_id=draft.id,terms_json=json.dumps(terms),snapshot_json=json.dumps(snapshot)));db.commit()
    details=''
    for key,label in [('cryptoAmount','Số lượng USDT'),('minOrderLimit','Tối thiểu VND'),('maxOrderLimit','Tối đa VND')]:
        details+=f'\n{label}: {snapshot[key] or "chưa có dữ liệu"} → {terms[key] if key in terms else "giữ nguyên"}'
    return {'content':f'**Xác nhận cập nhật quảng cáo {side} slot {slot_number}**\nID {slot.adv_id}\nGiá hiện tại {ad["unitPrice"]}đ → {price}đ/USDT.{details}\nAuto của slot sẽ tắt; quảng cáo {"giữ ẩn" if ad["isHidden"] else "giữ hiển thị"}. Các thiết lập khác giữ nguyên.\nXác nhận hết hạn sau 5 phút.','components':[{'type':1,'components':[button('Xác nhận gửi OKX','confirm:'+draft.id,3),button('Hủy','cancel:'+draft.id)]}]}

async def manual_confirm(db,owner,draft_id):
    draft=db.get(ManualPriceDraft,draft_id)
    now=datetime.now(timezone.utc)
    if draft is None or draft.owner!=owner or draft.used: raise ValueError('Xác nhận không hợp lệ hoặc đã dùng.')
    expiry=draft.expires.replace(tzinfo=timezone.utc) if not draft.expires.tzinfo else draft.expires
    if expiry<now or not settings.okx_ad_pricing_enabled: raise ValueError('Xác nhận hết hạn hoặc sửa giá đã bị tắt trên Render.')
    slot=db.get(OKXSlot,draft.slot_id)
    stored=db.get(ManualAdTerms,draft.id)
    terms=json.loads(stored.terms_json) if stored else {}
    if slot.adv_id!=draft.ad_id: raise ValueError('ID slot đã đổi; hãy xem và xác nhận lại.')
    if db.scalar(select(OKXSlot.id).where(OKXSlot.id!=slot.id,OKXSlot.adv_id==draft.ad_id)):
        raise ValueError('ID quảng cáo đang gán cho nhiều slot; chưa gửi yêu cầu.')
    async with httpx.AsyncClient(timeout=30) as client:
        ad=await read_ad(client,draft.ad_id)
        if ad['side']!=slot.side.lower() or ad.get('type')!='limit' or ad.get('status')!='new' or ad.get('isHidden') is not draft.hidden: raise ValueError('Quảng cáo đã thay đổi; hãy tạo xác nhận mới.')
        if stored and ad_snapshot(ad)!=json.loads(stored.snapshot_json): raise ValueError('Giá/số lượng/hạn mức đã thay đổi từ lúc xem trước; hãy tạo xác nhận mới.')
        if slot.side=='SELL':
            cost=await read_pricing_cost(db,client,reader=read_last_buy_cost)
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
        if slot.side=='SELL' and cost_revision(db)!=cost.get('settings_revision'):
            attempt.state='IDLE';db.commit();raise ValueError('Giá vốn vừa thay đổi; cần xem lại giá bán trước khi xác nhận.')
        attempt.state='UNKNOWN';db.commit()
        try:
            result=await request(client,'POST','/api/v5/p2p/ad/update',{'adId':draft.ad_id,'unitPrice':str(draft.price),'online':'false' if draft.hidden else 'true',**terms})
            if str(result.get('oldAdId'))!=draft.ad_id or not re.fullmatch(r'[0-9]{1,30}',str(result.get('newAdId',''))): raise ValueError('Phản hồi chưa xác nhận ID quảng cáo mới.')
            attempt.result_ad_id=str(result['newAdId']);attempt.state='CONFIRMING';db.commit()
            if terms:
                final=ad_snapshot(await read_ad(client,attempt.result_ad_id))
                for key,value in terms.items():
                    if final.get(key) is None or Decimal(final[key])!=Decimal(value):
                        return 'OKX đã trả ID mới '+attempt.result_ad_id+' nhưng chưa xác minh đúng số lượng/hạn mức. Auto vẫn tắt; không gửi lại yêu cầu.'
            if await confirm(db,client,attempt,slot):
                if 'minOrderLimit' in terms: slot.min_amount=Decimal(terms['minOrderLimit'])
                if 'maxOrderLimit' in terms: slot.max_amount=Decimal(terms['maxOrderLimit'])
                if terms:
                    db.add(OKXAuditLog(action='OKX_MANUAL_TERMS_CONFIRMED',actor='discord:'+owner,detail=json.dumps({'ad_id':slot.adv_id,**terms})))
                    db.commit()
                return 'Đã xác minh giá, số lượng và hạn mức đã yêu cầu trên OKX. ID mới '+slot.adv_id+' · Auto đã tắt.'
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
        if action=='cost':
            return {'content':save_cost(db,input_number(value,money=True),'discord:'+owner)+'\nÁp dụng cho cả hai slot bán ở lần quét tiếp theo nếu Auto đang bật.','components':[]}
        if action=='price':
            if values.get('slot') not in {'1','2'}: raise ValueError('Chọn slot 1 hoặc 2.')
            terms={target:values[source] for source,target in [('quantity','cryptoAmount'),('minimum','minOrderLimit'),('maximum','maxOrderLimit')] if values.get(source,'').strip()}
            return await manual_preview(db,owner,side,int(values['slot']),input_number(value,money=True),terms)
        if not re.fullmatch(r'[0-9]{1,30}',value): raise ValueError('Nhập số nguyên không dấu phân cách.')
        data=command('vitri',chieu=side,vitri=int(value)) if action=='rank' else command('quangcao',chieu=side,slot=int(values.get('slot','0')),id=value)
    elif action=='confirm': return {'content':await manual_confirm(db,owner,code[2]),'components':[]}
    elif action=='cancel':
        changed=db.execute(update(ManualPriceDraft).where(ManualPriceDraft.id==code[2],ManualPriceDraft.owner==owner,ManualPriceDraft.used.is_(False)).values(used=True));db.commit()
        if changed.rowcount!=1: return {'content':'Xác nhận không tồn tại hoặc đã xử lý. Không thể hủy yêu cầu đã gửi; xem trạng thái trên OKX.','components':[]}
        return {'content':'Đã hủy xác nhận; không gửi yêu cầu sửa giá.','components':[]}
    elif action=='costauto':
        return {'content':'Chỉ dùng giá vốn bạn nhập thủ công. Bấm Nhập giá vốn; không lấy lệnh mua gần nhất.','components':[]}
    elif action=='status': data=command('trangthai')
    elif action=='list': data=command('danhsach',chieu=code[2])
    elif action in {'pause','resume'}: data=command('bamtat',bat=action=='resume')
    elif action=='auto':
        side=code[2]
        return {'content':'Chọn Auto cho '+side+' (không mở quảng cáo đã ẩn).','components':[{'type':1,'components':[button(f'Slot {n}: '+label,f'setauto:{side}:{n}:{enabled}',style) for n in (1,2) for label,enabled,style in [('Bật',1,3),('Tắt',0,4)]]}]}
    elif action=='setauto': data=command('auto',chieu=code[2],slot=int(code[3]),bat=code[4]=='1')
    else: raise ValueError('Nút không hợp lệ.')
    return {'content':await execute_price_command(data,db,owner),'components':[]}

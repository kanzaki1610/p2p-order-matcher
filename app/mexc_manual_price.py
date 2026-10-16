"""Preview and one-use confirmation for manual MEXC advertisement updates."""
import json
import time
import uuid
from decimal import Decimal

from sqlalchemy import Boolean, Numeric, String, Text, update, select
from sqlalchemy.orm import Mapped, mapped_column

from .config import settings
from .database import Base
from .models import OKXAuditLog
from .mexc import MexcP2PClient
from .mexc_ads import read_ad, update_body
from .mexc_pricing import MexcPriceSlot, cost_settings
from .okx_ads import number


class MexcManualPriceDraft(Base):
    __tablename__='mexc_manual_price_drafts'
    id: Mapped[str] = mapped_column(String(32),primary_key=True)
    owner: Mapped[str] = mapped_column(String(100))
    slot: Mapped[str] = mapped_column(String(12))
    ad: Mapped[str] = mapped_column(String(40))
    price: Mapped[Decimal] = mapped_column(Numeric(20,0))
    snapshot: Mapped[str] = mapped_column(Text)
    terms: Mapped[str] = mapped_column(Text)
    used: Mapped[bool] = mapped_column(Boolean,default=False)
    expires: Mapped[Decimal] = mapped_column(Numeric(20,3))


def snapshot(row):
    return {'body':update_body(row,number(row['price'])),'status':row['advStatus']}


def manual_body(row, price, terms):
    if set(terms)-{'quantity','minimum','maximum'}:raise ValueError('Thông số không hợp lệ.')
    body=update_body(row,price)
    quantity=number(terms.get('quantity',row['quantity']))
    lower=number(terms.get('minimum',row['minSingleTransAmount']))
    upper=number(terms.get('maximum',row['maxSingleTransAmount']))
    if lower!=lower.to_integral_value() or upper!=upper.to_integral_value() or lower>upper:
        raise ValueError('Hạn mức VND phải nguyên dương; tối thiểu không vượt tối đa.')
    if 'quantity' in terms and lower>quantity*price:
        raise ValueError('Số lượng USDT × giá nhỏ hơn hạn mức tối thiểu.')
    body.update(minSingleTransAmount=str(lower),maxSingleTransAmount=str(upper))
    if 'quantity' in terms:
        delta=quantity-number(row['quantity'])
        if delta>0:body['supplyQuantity']=str(delta)
        elif delta<0:body['reduceQuantity']=str(-delta)
    # Explicitly preserve visibility, including manually closed ads.
    body['advStatus']=row['advStatus']
    expected={'price':str(price),'quantity':str(quantity),
        'minSingleTransAmount':str(lower),'maxSingleTransAmount':str(upper),'advStatus':row['advStatus']}
    return body,expected


def validate_floor(db,side,price):
    config=cost_settings(db)
    if side=='SELL' and (config.cost is None or price<config.cost+10):
        raise ValueError('Nhập giá vốn MEXC trước; giá bán phải đạt giá vốn MEXC +10đ.')
    return config.revision


async def preview(db,owner,side,slot_number,price,terms):
    if settings.database_schema=='kayhap' or not settings.mexc_ad_pricing_enabled:
        raise ValueError('Chỉnh giá MEXC chưa được bật cho tài khoản này.')
    if side not in {'BUY','SELL'} or str(slot_number) not in {'1','2'}:raise ValueError('Slot không hợp lệ.')
    key=side+':'+str(slot_number)
    slot=db.get(MexcPriceSlot,key,populate_existing=True)
    if slot is None or not slot.ad:raise ValueError('Cài quảng cáo MEXC cho slot trước.')
    if slot.state!='IDLE':raise ValueError('Slot có yêu cầu chưa rõ kết quả; không gửi lại.')
    if db.scalar(select(MexcPriceSlot.key).where(MexcPriceSlot.key!=key,MexcPriceSlot.ad==slot.ad)):
        raise ValueError('Quảng cáo đang gán cho nhiều slot.')
    validate_floor(db,side,price)
    async with MexcP2PClient() as api:row=await read_ad(api,slot.ad,side)
    manual_body(row,price,terms)
    draft=MexcManualPriceDraft(id=uuid.uuid4().hex,owner=owner,slot=key,ad=slot.ad,price=price,
        snapshot=json.dumps(snapshot(row),sort_keys=True),terms=json.dumps(terms),expires=Decimal(str(time.time()+300)))
    db.add(draft);db.commit()
    text=f'Xác nhận MEXC {key}\nID: {slot.ad}\nGiá: {row["price"]} → {price}đ/USDT'
    for key,label,field in [('quantity','Số lượng USDT','quantity'),('minimum','Tối thiểu VND','minSingleTransAmount'),('maximum','Tối đa VND','maxSingleTransAmount')]:
        text+=f'\n{label}: {row[field]} → {terms.get(key,"giữ nguyên")}'
    text+='\nAuto slot sẽ tắt; giữ nguyên trạng thái hiển thị. Xác nhận hết hạn sau 5 phút.'
    return {'content':text,'components':[{'type':1,'components':[
        {'type':2,'style':3,'label':'Xác nhận gửi MEXC','custom_id':'mprice:confirm:'+draft.id},
        {'type':2,'style':2,'label':'Hủy','custom_id':'mprice:cancel:'+draft.id}]}]}


async def confirm(db,owner,draft_id):
    draft=db.get(MexcManualPriceDraft,draft_id,populate_existing=True)
    if draft is None or draft.owner!=owner or draft.used or float(draft.expires)<time.time():
        raise ValueError('Xác nhận không hợp lệ, đã dùng hoặc hết hạn.')
    if settings.database_schema=='kayhap' or not settings.mexc_ad_pricing_enabled:
        raise ValueError('Chỉnh giá MEXC đang tắt.')
    slot=db.get(MexcPriceSlot,draft.slot,populate_existing=True)
    if slot is None or slot.ad!=draft.ad or slot.state!='IDLE':raise ValueError('Slot đã thay đổi hoặc đang xử lý; tạo xác nhận mới.')
    side=draft.slot.split(':')[0]
    async with MexcP2PClient() as api:
        row=await read_ad(api,draft.ad,side)
        if snapshot(row)!=json.loads(draft.snapshot):raise ValueError('Quảng cáo đã thay đổi từ lúc xem trước; cần xác nhận mới.')
        revision=validate_floor(db,side,draft.price)
        body,expected=manual_body(row,draft.price,json.loads(draft.terms))
        attempt=uuid.uuid4().hex
        taken=db.execute(update(MexcPriceSlot).where(MexcPriceSlot.key==draft.slot,
            MexcPriceSlot.ad==draft.ad,MexcPriceSlot.state=='IDLE').values(state='UNKNOWN',action='MANUAL',
                attempt_id=attempt,enabled=False,paused_by_floor=False,wanted_price=draft.price,
                wanted_status=row['advStatus'],last_action=Decimal(str(time.time())),reason=None))
        used=db.execute(update(MexcManualPriceDraft).where(MexcManualPriceDraft.id==draft.id,
            MexcManualPriceDraft.used.is_(False)).values(used=True))
        if taken.rowcount!=1 or used.rowcount!=1:
            db.rollback();raise ValueError('Xác nhận đã dùng hoặc slot đang xử lý; không gửi lại.')
        db.commit();db.refresh(slot)
        if cost_settings(db).revision!=revision:
            db.execute(update(MexcPriceSlot).where(MexcPriceSlot.key==draft.slot,MexcPriceSlot.attempt_id==attempt).values(state='IDLE'))
            db.commit();raise ValueError('Giá vốn vừa đổi; tạo xác nhận mới, chưa gửi MEXC.')
        try:
            result=await api._request_once('/api/v3/fiat/merchant/ads/save_or_update',params=body,method='POST')
            if str(result)!=draft.ad:raise ValueError('MEXC chưa xác nhận đúng ID quảng cáo.')
            final=await read_ad(api,draft.ad,side)
            if any(number(final.get(key))!=Decimal(value) for key,value in expected.items() if key!='advStatus') or final['advStatus']!=expected['advStatus']:
                raise ValueError('Chưa xác minh đúng giá, số lượng, hạn mức hoặc hiển thị sau yêu cầu.')
            changed=db.execute(update(MexcPriceSlot).where(MexcPriceSlot.key==draft.slot,
                MexcPriceSlot.attempt_id==attempt,MexcPriceSlot.state=='UNKNOWN').values(state='IDLE',reason=None))
            db.add(OKXAuditLog(action='MEXC_MANUAL_PRICE_CONFIRMED',actor='discord:'+owner,detail=json.dumps({'ad':draft.ad,**expected})))
            db.commit()
            if changed.rowcount!=1:return 'Đã đọc giá trên MEXC, nhưng trạng thái slot đã thay đổi; kiểm tra trước khi thao tác tiếp.'
            return 'Đã xác minh cập nhật quảng cáo MEXC. Auto của slot đã tắt; giá vốn MEXC vẫn giữ riêng.'
        except Exception as error:
            db.rollback()
            reason=str(error) if isinstance(error,ValueError) else 'Không xác minh được phản hồi MEXC ('+type(error).__name__+').'
            db.execute(update(MexcPriceSlot).where(MexcPriceSlot.key==draft.slot,MexcPriceSlot.attempt_id==attempt).values(reason=reason[:450]))
            db.commit()
            return 'Chưa rõ kết quả: '+reason[:450]+' Auto đã tắt; không tự gửi lại, cần kiểm tra MEXC.'

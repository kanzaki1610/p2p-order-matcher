"""MEXC pricing buttons, isolated from all OKX controls and cost settings."""
import re
import uuid
from decimal import Decimal

from sqlalchemy import select, update

from .config import settings
from .mexc import MexcP2PClient
from .mexc_ads import ad_id, read_ad, my_ads, update_body
from .mexc_pricing import MexcPriceSlot, cost_settings
from .okx_ads import number
from .discord_price_panel import input_number
from .mexc_manual_price import preview as manual_preview, confirm as manual_confirm, MexcManualPriceDraft
from .discord_notifications import valid_webhook


def button(label, action, style=2):
    return {'type':2,'style':style,'label':label,'custom_id':'mprice:'+action}


def panel():
    return {'content':'Điều khiển bám giá MEXC · USDT/VND\nGiá vốn MEXC riêng; bán tối thiểu giá vốn +10đ. Chọn quảng cáo, vị trí, biệt danh và bật Auto từng slot. Bot không tự mở quảng cáo bạn tắt thủ công.',
        'components':[{'type':1,'components':[button('Đặt giá Mua','price:BUY',1),button('Cài Mua','config:BUY'),button('Auto Mua','auto:BUY'),button('Quảng cáo mua','list:BUY')]},
                      {'type':1,'components':[button('Đặt giá Bán','price:SELL',1),button('Cài Bán','config:SELL'),button('Auto Bán','auto:SELL'),button('Quảng cáo bán','list:SELL')]},
                      {'type':1,'components':[button('Giá vốn MEXC','cost:SELL',1),button('Trạng thái','status'),button('Tắt toàn bộ Auto','pause',4)]}]}


def modal(action, side):
    if side not in {'BUY','SELL'} or action not in {'config','cost','price'}: raise ValueError('Nút MEXC không hợp lệ.')
    fields = [('cost','Giá mua trước đó (VND/USDT)', 'Ví dụ 25905')] if action=='cost' else [
        ('slot','Slot 1 hoặc 2','1'),('ad','ID quảng cáo MEXC','a...'),
        ('rank','Bám vị trí đối thủ (1–50)','10'),('nickname','Biệt danh MEXC của bạn','Nhập đúng biệt danh trên bảng giá')]
    if action=='price':
        fields=[('slot','Slot 1 hoặc 2','1'),('price','Giá VND/USDT','26050'),
                ('quantity','Số lượng USDT (trống: giữ nguyên)',''),
                ('minimum','Tối thiểu VND/giao dịch (trống: giữ nguyên)',''),
                ('maximum','Tối đa VND/giao dịch (trống: giữ nguyên)','')]
    return {'type':9,'data':{'custom_id':f'mprice:form:{action}:{side}',
        'title':'Giá vốn riêng MEXC' if action=='cost' else ('Đặt giá MEXC ' if action=='price' else 'Bám giá MEXC ')+side,
        'components':[{'type':1,'components':[{'type':4,'custom_id':key,'label':label,'style':1,
            'required':key not in {'quantity','minimum','maximum'},'max_length':100,'placeholder':placeholder}]} for key,label,placeholder in fields]}}


async def execute_panel(payload, db, owner):
    if settings.database_schema == 'kayhap': raise ValueError('Bám giá MEXC chỉ được cấu hình cho tài khoản gốc.')
    parts = str(payload.get('data',{}).get('custom_id','')).split(':')
    if not parts or parts[0] != 'mprice': raise ValueError('Nút MEXC không hợp lệ.')
    action = parts[1] if len(parts)>1 else ''
    actor = 'discord:'+owner
    from .models import OKXAuditLog
    if payload['type']==5:
        if len(parts)!=4 or parts[1]!='form': raise ValueError('Biểu mẫu MEXC không hợp lệ.')
        action, side = parts[2:]
        if side not in {'BUY','SELL'}: raise ValueError('Chiều không hợp lệ.')
        values={x['custom_id']:x.get('value','').strip() for row in payload['data'].get('components',[]) for x in row.get('components',[])}
        if action=='price':
            if values.get('slot') not in {'1','2'}:raise ValueError('Chọn slot 1 hoặc 2.')
            terms={key:str(input_number(values[key],money=key!='quantity')) for key in ('quantity','minimum','maximum') if values.get(key)}
            return await manual_preview(db,owner,side,values['slot'],input_number(values.get('price',''),money=True),terms)
        if action=='cost':
            raw=values.get('cost','')
            if not re.fullmatch(r'[0-9]{1,12}',raw) or int(raw)<=0: raise ValueError('Nhập giá vốn nguyên dương, không dấu phân cách.')
            config=cost_settings(db);config.cost=Decimal(raw);config.revision=uuid.uuid4().hex
            db.add(OKXAuditLog(action='MEXC_PRICE_COST',actor=actor,detail='Giá vốn MEXC='+raw));db.commit()
            return {'content':f'Đã lưu giá vốn MEXC {int(raw):,}đ · bán tối thiểu {int(raw)+10:,}đ/USDT. Không thay giá vốn OKX.','components':[]}
        if action!='config' or values.get('slot') not in {'1','2'}: raise ValueError('Chọn slot 1 hoặc 2.')
        if not re.fullmatch(r'[0-9]{1,2}',values.get('rank','')) or not 1<=int(values['rank'])<=50: raise ValueError('Vị trí phải từ 1 đến 50.')
        nick=values.get('nickname','')
        if not nick or len(nick)>100: raise ValueError('Thiếu biệt danh MEXC.')
        code=ad_id(values.get('ad',''));key=side+':'+values['slot']
        slot=db.get(MexcPriceSlot,key)
        if slot and slot.state!='IDLE': raise ValueError('Slot có yêu cầu chưa rõ kết quả; kiểm tra MEXC, không thay ID để gửi lại.')
        async with MexcP2PClient() as api: row=await read_ad(api,code,side)
        update_body(row,number(row['price']))
        collision=db.scalar(select(MexcPriceSlot).where(MexcPriceSlot.key!=key,MexcPriceSlot.ad==code))
        if collision: raise ValueError('Quảng cáo đã được gán cho slot khác.')
        db.expire_all()
        if slot:
            db.refresh(slot,with_for_update=True)
            if slot.state!='IDLE': raise ValueError('Slot vừa gửi yêu cầu; chưa thay cấu hình.')
        else: slot=MexcPriceSlot(key=key);db.add(slot)
        slot.ad=code;slot.rank=int(values['rank']);slot.nickname=nick;slot.enabled=False
        slot.paused_by_floor=False;slot.revision=uuid.uuid4().hex
        db.add(OKXAuditLog(action='MEXC_PRICE_CONFIG',actor=actor,detail=f'{key} ad={code}; rank={slot.rank}; Auto=False'));db.commit()
        return {'content':f'Đã lưu MEXC {key}: {code} · vị trí {slot.rank}. Auto đang tắt; kiểm tra giá vốn rồi bấm Auto để bật.','components':[]}
    if action=='confirm' and len(parts)==3:
        return {'content':await manual_confirm(db,owner,parts[2]),'components':[]}
    if action=='cancel' and len(parts)==3:
        changed=db.execute(update(MexcManualPriceDraft).where(MexcManualPriceDraft.id==parts[2],
            MexcManualPriceDraft.owner==owner,MexcManualPriceDraft.used.is_(False)).values(used=True))
        db.commit()
        return {'content':'Đã hủy xác nhận; chưa gửi MEXC.' if changed.rowcount==1 else 'Xác nhận đã xử lý; không hủy yêu cầu đã gửi.','components':[]}
    if action=='auto' and len(parts)==3 and parts[2] in {'BUY','SELL'}:
        return {'content':'Chọn Auto MEXC '+parts[2]+'. Không mở quảng cáo tắt thủ công.','components':[{'type':1,'components':[
            button(f'Slot {i}: '+label,f'setauto:{parts[2]}:{i}:{flag}',style)
            for i in (1,2) for label,flag,style in [('Bật','1',3),('Tắt','0',4)]]}]}
    if action=='setauto' and len(parts)==5 and parts[2] in {'BUY','SELL'} and parts[3] in {'1','2'} and parts[4] in {'0','1'}:
        slot=db.get(MexcPriceSlot,parts[2]+':'+parts[3],populate_existing=True)
        if slot is None: raise ValueError('Cài quảng cáo cho slot trước.')
        enabled=parts[4]=='1'
        verified_revision=slot.revision
        if enabled:
            if not settings.mexc_ad_pricing_enabled: raise ValueError('MEXC_AD_PRICING_ENABLED đang tắt trên Render.')
            if not valid_webhook(settings.discord_webhook_mexc_pricing):raise ValueError('Chưa cấu hình kênh cảnh báo giá MEXC; chưa bật Auto. Đặt giá thủ công vẫn dùng được.')
            if slot.state!='IDLE': raise ValueError('Kết quả yêu cầu cũ chưa rõ; kiểm tra MEXC trước.')
            if parts[2]=='SELL' and cost_settings(db).cost is None: raise ValueError('Nhập giá vốn MEXC trước khi bật Auto bán.')
            async with MexcP2PClient() as api: row=await read_ad(api,slot.ad,parts[2])
            if row['advStatus']!='OPEN': raise ValueError('Quảng cáo chưa OPEN; bạn cần mở trên MEXC trước.')
            update_body(row,number(row['price']))
        # Row update takes a lock; recheck state after slow API reads.
        db.refresh(slot,with_for_update=True)
        if slot.revision!=verified_revision: raise ValueError('Cấu hình slot vừa thay đổi; kiểm tra lại trước khi bật Auto.')
        if enabled and slot.state!='IDLE': raise ValueError('Yêu cầu vừa được gửi; chưa bật lại Auto.')
        slot.enabled=enabled;slot.revision=uuid.uuid4().hex
        db.add(OKXAuditLog(action='MEXC_PRICE_AUTO',actor=actor,detail=f'{slot.key} Auto={enabled}'));db.commit()
        return {'content':f'MEXC {slot.key}: Auto '+('bật' if enabled else 'tắt')+'.','components':[]}
    if action=='pause':
        for slot in db.scalars(select(MexcPriceSlot)).all():slot.enabled=False;slot.revision=uuid.uuid4().hex
        db.add(OKXAuditLog(action='MEXC_PRICE_PAUSE',actor=actor,detail='Auto=False tất cả slot'));db.commit()
        return {'content':'Đã tắt Auto MEXC. Yêu cầu đã gửi vẫn có thể hoàn tất. Không thay đổi OKX.','components':[]}
    if action=='status':
        config=cost_settings(db)
        text='MEXC · '+('Sẵn sàng' if settings.mexc_ad_pricing_enabled else 'Render đang tắt')+'\n'
        if not settings.discord_webhook_mexc_pricing:text+='Auto còn chờ cấu hình kênh cảnh báo. Đặt giá thủ công vẫn dùng được.\n'
        text+='Giá vốn riêng: '+(f'{config.cost:,.0f}đ · sàn bán {config.cost+10:,.0f}đ' if config.cost else 'chưa nhập')
        for slot in db.scalars(select(MexcPriceSlot)).all():
            text+=f'\n{slot.key}: {slot.ad} · vị trí {slot.rank} · Auto {slot.enabled} · {slot.state}'
            if slot.reason:text+='\n'+slot.reason
        return {'content':text,'components':[]}
    if action=='list' and len(parts)==3 and parts[2] in {'BUY','SELL'}:
        async with MexcP2PClient() as api: rows=await my_ads(api)
        rows=[r for r in rows if r.get('side')==parts[2] and r.get('coinName')=='USDT']
        return {'content':'Quảng cáo MEXC '+parts[2]+' (tối đa 10 gần nhất):\n'+('\n'.join(
            f"{r.get('advNo')} · {r.get('price')}đ · {r.get('advStatus')} · còn {r.get('availableQuantity')} USDT" for r in rows) or 'Không có.'),'components':[]}
    raise ValueError('Nút MEXC không hợp lệ.')

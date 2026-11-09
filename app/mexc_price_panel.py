"""MEXC pricing buttons, isolated from all OKX controls and cost settings."""
import re
import uuid
import json
import time
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
from .mexc_price_state import MexcAutoLimits, MexcPricingSwitch, MexcPendingPrice, active
from .mexc_price_recovery import recover
from .mexc_price_adoption import preview as adoption_preview, confirm as adoption_confirm, MexcAdoptionDraft


def button(label, action, style=2):
    return {'type':2,'style':style,'label':label,'custom_id':'mprice:'+action}


def panel():
    rows = []
    for side, label in [('BUY', 'Mua'), ('SELL', 'Bán')]:
        rows.append({'type':1,'components':[button('Đặt giá '+label,'price:'+side,1),
            button('Vị trí '+label,'rank:'+side),button('Chọn quảng cáo '+label,'ad:'+side),button('Auto '+label,'auto:'+side),button('Bám biệt danh '+label,'follow:'+side)]})
    rows.append({'type':1,'components':[button('Trạng thái','status'),button('Quảng cáo mua','list:BUY'),
        button('Quảng cáo bán','list:SELL'),button('Tạm dừng bám giá','pause',4),button('Tiếp tục bám giá','resume',3)]})
    rows.append({'type':1,'components':[button('Nhập giá vốn MEXC','cost:SELL',1),button('Min bán · Max tự động','limits:SELL')]})
    rows.append({'type':1,'components':[button('Kiểm tra/khôi phục Mua','recover:BUY'),button('Kiểm tra/khôi phục Bán','recover:SELL'),
        button('Nhận cấu hình Mua','adopt:BUY'),button('Nhận cấu hình Bán','adopt:SELL')]})
    buy_mode = 'BUY chỉ đổi giá, giữ Min/Max và USDT' if settings.mexc_buy_price_only else 'BUY giữ ngân sách VND còn lại'
    return {'content':f'Điều khiển giá MEXC · USDT/VND\nGiá vốn MEXC riêng; bán tối thiểu giá vốn +10đ. {buy_mode}; SELL tự tính Max theo USDT khả dụng và giá mới. Chỉ operator của tài khoản này được dùng. Không tự mở quảng cáo bạn tắt thủ công.',
        'components':rows,'allowed_mentions':{'parse':[]}}


def modal(action, side):
    if action == "follow":
        from .nickname_target import modal as follow_modal
        return follow_modal("mprice", side)
    if side not in {'BUY','SELL'} or action not in {'config','cost','price','rank','ad','limits'}:
        raise ValueError('Nút MEXC không hợp lệ.')
    if action == 'limits' and side == 'BUY':
        return {'type':4,'data':{'content':('Auto mua chỉ đổi giá; giữ Min/Max và USDT trên sàn.' if settings.mexc_buy_price_only else 'Auto mua tự giữ Min và trừ tiền đã mua khỏi ngân sách VND. Không cần nhập Min/Max mua.'),'flags':64}}
    fields = {
        'cost':[('cost','Giá mua trước đó (VND/USDT)','Ví dụ 25905')],
        'config':[('slot','Slot 1 hoặc 2','1'),('ad','ID quảng cáo MEXC','a...'),('rank','Bám vị trí đối thủ (1–50)','10'),('nickname','Biệt danh MEXC của bạn','Nhập đúng biệt danh trên bảng giá')],
        'ad':[('slot','Slot 1 hoặc 2','1'),('ad','ID quảng cáo MEXC','a...'),('nickname','Biệt danh (trống: giữ nguyên)','')],
        'rank':[('slot','Slot 1 hoặc 2','1'),('rank','Vị trí đối thủ (1–50)','10'),('nickname','Biệt danh (trống: giữ nguyên)','')],
        'limits':[('slot','Slot 1 hoặc 2','1'),('minimum','Min VND/giao dịch (Max tự tính)','20000000')],
        'price':[('slot','Slot 1 hoặc 2','1'),('price','Giá VND/USDT','26050'),('quantity','Số lượng USDT (trống: giữ nguyên)',''),
            ('minimum','Tối thiểu VND/giao dịch (trống: giữ nguyên)',''),('maximum','Tối đa VND/giao dịch (trống: giữ nguyên)','')],
    }[action]
    labels={'cost':'Giá vốn riêng MEXC','config':'Cấu hình MEXC','ad':'Chọn quảng cáo MEXC','rank':'Vị trí MEXC','limits':'Min bán MEXC','price':'Đặt giá MEXC'}
    return {'type':9,'data':{'custom_id':f'mprice:form:{action}:{side}','title':labels[action]+('' if action in {'cost','limits'} else ' '+side),
        'components':[{'type':1,'components':[{'type':4,'custom_id':key,'label':label,'style':1,
            'required':key not in {'quantity','minimum','maximum'} and not (key=='nickname' and action in {'ad','rank'}) or action=='limits',
            'max_length':100,'placeholder':placeholder}]} for key,label,placeholder in fields]}}


async def execute_panel(payload, db, owner):
    if not settings.discord_pricing_commands_enabled and settings.database_schema == 'kayhap': raise ValueError('Điều khiển giá chưa được bật cho tài khoản này.')
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
        if action=='follow':
            from .nickname_target import save
            return save(db,'MEXC',side,values,owner)
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
        if action in {'rank','ad','limits'}:
            if values.get('slot') not in {'1','2'}: raise ValueError('Chọn slot 1 hoặc 2.')
            key=side+':'+values['slot']
            slot=db.get(MexcPriceSlot,key,populate_existing=True)
            if action=='limits':
                if side=='BUY':return {'content':'BUY tự giữ ngân sách VND còn lại, không cần nhập Min/Max.','components':[]}
                if slot is None or not slot.ad:raise ValueError('Chọn quảng cáo MEXC cho slot trước.')
                lower=input_number(values.get('minimum',''),money=True)
                if lower<=0:raise ValueError('Min phải là VND nguyên dương.')
                db.merge(MexcAutoLimits(slot=key,ad=slot.ad,minimum=lower))
                slot.enabled=False;slot.revision=uuid.uuid4().hex
                db.add(OKXAuditLog(action='MEXC_PRICE_LIMITS',actor=actor,detail=f'{key} Min={lower}; Max=remaining stock * price - 1; Auto=False'))
                db.commit()
                return {'content':f'Đã lưu MEXC {key}: Min {lower:,.0f} VND. Max tự tính theo USDT khả dụng × giá mới − 1 VND, trong giới hạn sàn. Auto slot đã tắt; yêu cầu cũ nếu còn khóa vẫn cần khôi phục.','components':[]}
            if action=='rank':
                if slot is None or not slot.ad:raise ValueError('Chọn quảng cáo MEXC cho slot trước.')
                if not re.fullmatch(r'[0-9]{1,2}',values.get('rank','')) or not 1<=int(values['rank'])<=50:raise ValueError('Vị trí phải từ 1 đến 50.')
                nickname=values.get('nickname') or slot.nickname or settings.mexc_merchant_nicknames.split(',')[0].strip()
                if not nickname:raise ValueError('Nhập biệt danh MEXC để loại quảng cáo của chính bạn.')
                from .nickname_target import clear
                clear(db,'MEXC',side,values['slot'])
                slot.rank=int(values['rank']);slot.nickname=nickname;slot.revision=uuid.uuid4().hex
                db.add(OKXAuditLog(action='MEXC_PRICE_RANK',actor=actor,detail=f'{key} rank={slot.rank}'))
                db.commit()
                return {'content':f'Đã lưu vị trí MEXC {key}: {slot.rank}. Giữ nguyên công tắc Auto; yêu cầu cũ chưa rõ kết quả không được gửi lại.','components':[]}
            # Reuse the existing owned-ad validation for ID changes.
            action='config'
            values['rank']=str(slot.rank if slot else 10)
            values['nickname']=values.get('nickname') or (slot.nickname if slot else '') or settings.mexc_merchant_nicknames.split(',')[0].strip()
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
        message=f'MEXC {slot.key}: Auto '+('bật' if enabled else 'tắt')+'.'
        if enabled:
            message+=' Đã lưu công tắc, chưa xác nhận đổi giá trên sàn.'
            if not active(db):message+=' Bám giá chung đang TẠM DỪNG; cần bấm Tiếp tục bám giá.'
        return {'content':message,'components':[]}
    if action in {'pause','resume'}:
        enabled=action=='resume'
        if enabled and (not settings.mexc_ad_pricing_enabled or not valid_webhook(settings.discord_webhook_mexc_pricing)):
            raise ValueError('Chưa bật tính năng hoặc webhook MEXC trên Render; chưa tiếp tục bám giá.')
        db.merge(MexcPricingSwitch(id=1,enabled=enabled))
        db.add(OKXAuditLog(action='MEXC_PRICING_SWITCH',actor=actor,detail=str(enabled)));db.commit()
        message=('Đã bật công tắc bám giá chung MEXC; chưa xác nhận đổi giá trên sàn.' if enabled else 'Đã tạm dừng bám giá MEXC; giữ cấu hình Auto từng slot để tiếp tục sau. Yêu cầu đã gửi vẫn có thể hoàn tất.')
        if enabled:
            slots=db.scalars(select(MexcPriceSlot)).all()
            ready=[s for s in slots if s.enabled and s.state=='IDLE']
            if not ready:message+=' Chưa có slot Auto sẵn sàng. Chọn quảng cáo, nhập giá vốn bán, kiểm tra slot lỗi rồi bật Auto từng slot.'
            else:message+=' Slot chờ chu kỳ kiểm tra: '+', '.join(s.key for s in ready)+'.'
        return {'content':message+' Không thay đổi OKX hoặc Auto mở khóa.','components':[]}
    if action=='recover' and len(parts)==3 and parts[2] in {'BUY','SELL'}:
        return {'content':'Chọn slot MEXC cần kiểm tra/khôi phục. Chỉ đọc API, không gửi lại yêu cầu; Auto slot sẽ tắt.','components':[{'type':1,'components':[
            button('Slot '+str(i),f'checkslot:{parts[2]}:{i}') for i in (1,2)]}]}
    if action=='checkslot' and len(parts)==4:
        message=await recover(db,owner,parts[2],parts[3])
        slot=db.get(MexcPriceSlot,parts[2]+':'+parts[3],populate_existing=True)
        fallback=slot is not None and (slot.state in {'UNKNOWN','CONFIRMING'} or parts[2]=='BUY')
        return {'content':message+('\nNếu đã sửa quảng cáo trên sàn: bấm Nhận cấu hình hiện tại để xem và xác nhận dữ liệu mới.' if fallback else ''),
            'components':[{'type':1,'components':[button('Nhận cấu hình hiện tại',f'adoptpreview:{parts[2]}:{parts[3]}')]}] if fallback else []}
    if action=='adopt' and len(parts)==3 and parts[2] in {'BUY','SELL'}:
        side=parts[2]
        return {'content':'Chọn slot '+('Mua' if side=='BUY' else 'Bán')+' MEXC để xem cấu hình hiện tại. Auto slot sẽ tắt; chưa gỡ khóa cho đến khi bạn xác nhận.'+((' Mua chỉ đổi giá, giữ Min/Max và USDT.' if settings.mexc_buy_price_only else ' Mua sẽ xác minh lịch sử và giữ ngân sách VND còn lại.') if side=='BUY' else ''),
            'components':[{'type':1,'components':[button('Slot '+str(i),f'adoptpreview:{side}:{i}') for i in (1,2)]}]}
    if action=='adoptpreview' and len(parts) in {3,4}:
        return await adoption_preview(db,owner,parts[-1],parts[2] if len(parts)==4 else 'SELL')
    if action=='adoptconfirm' and len(parts)==3:
        return {'content':await adoption_confirm(db,owner,parts[2]),'components':[]}
    if action=='adoptcancel' and len(parts)==3:
        changed=db.execute(update(MexcAdoptionDraft).where(MexcAdoptionDraft.id==parts[2],
            MexcAdoptionDraft.owner==owner,MexcAdoptionDraft.used.is_(False)).values(used=True))
        db.commit()
        return {'content':'Đã hủy; giữ khóa cũ và Auto tắt.' if changed.rowcount else 'Xác nhận đã xử lý hoặc không thuộc tài khoản này.','components':[]}
    if action=='status':
        config=cost_settings(db)
        text='MEXC · '+('Tính năng được bật trên Render' if settings.mexc_ad_pricing_enabled else 'Render đang tắt')+'\n'
        if not settings.discord_webhook_mexc_pricing:text+='Auto còn chờ cấu hình kênh cảnh báo. Đặt giá thủ công vẫn dùng được.\n'
        text+=('Bám giá chung: BẬT\n' if active(db) else 'Bám giá chung: TẠM DỪNG\n')
        text+='Giá vốn riêng: '+(f'{config.cost:,.0f}đ · sàn bán {config.cost+10:,.0f}đ' if config.cost else 'chưa nhập')
        slots=db.scalars(select(MexcPriceSlot)).all()
        if not slots:text+='\nChưa chọn quảng cáo cho slot nào; chưa có slot bám giá.'
        for slot in slots:
            from .nickname_target import summary
            mode=summary(db,'MEXC',slot.key.split(':')[0],slot.key.split(':')[1]) or f'vị trí {slot.rank}'
            text+=f'\n{slot.key}: {slot.ad} · {mode} · Auto {slot.enabled} · {slot.state}'
            if slot.state!='IDLE':text+='\nĐANG DỪNG vì kết quả yêu cầu cũ chưa rõ; không gửi đổi giá tiếp.'
            elif not slot.enabled:text+='\nAuto slot đang tắt.'
            if slot.state!='IDLE' and slot.wanted_price is not None:
                text+=f'\nGiá yêu cầu gần nhất: {slot.wanted_price:,.0f}đ/USDT.'
                pending=db.get(MexcPendingPrice,slot.key)
                if pending and time.time()-float(slot.last_action)>120 and 'api_http_status' not in json.loads(pending.expected_json):
                    text+=' Hồ sơ cũ chưa lưu mã API gốc; chưa thể xác định lỗi chỉ từ thông báo chung.'
            if slot.reason:text+='\n'+slot.reason
        return {'content':text,'components':[]}
    if action=='list' and len(parts)==3 and parts[2] in {'BUY','SELL'}:
        async with MexcP2PClient() as api: rows=await my_ads(api)
        rows=[r for r in rows if r.get('side')==parts[2] and r.get('coinName')=='USDT']
        return {'content':'Quảng cáo MEXC '+parts[2]+' (tối đa 10 gần nhất):\n'+('\n'.join(
            f"{r.get('advNo')} · {r.get('price')}đ · {r.get('advStatus')} · còn {r.get('availableQuantity')} USDT · Min {r.get('minSingleTransAmount')} / Max {r.get('maxSingleTransAmount')} VND" for r in rows) or 'Không có.'),'components':[]}
    raise ValueError('Nút MEXC không hợp lệ.')

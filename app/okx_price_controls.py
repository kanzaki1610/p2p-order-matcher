"""Shared rank preferences for dashboard, Discord and the original pricing worker."""
import httpx
import re
from urllib.parse import urlencode
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .config import settings
from .models import OKXPricePreferences, OKXAuditLog, OKXSlot, OKXPricingSwitch
from .okx_ads import read_ad, number
from .okx_cost import read_last_buy_cost
from .okx_rank import read_rank_plan
from .diagnostics import error_detail


def validate_rank(rank):
    if type(rank) is not int or not 1 <= rank <= 50:
        raise ValueError("Vị trí phải là số nguyên từ 1 đến 50.")
    return rank


def pricing_active(db):
    if not settings.okx_ad_pricing_enabled or settings.database_schema == 'kayhap':
        return False
    switch = db.get(OKXPricingSwitch, 1)
    return settings.okx_ad_pricing_enabled and (switch is None or switch.enabled)


async def manage_price_command(data, db, user_id):
    command = data['options'][0]
    action = command['name']
    options = {x['name']: x.get('value') for x in command.get('options', [])}
    actor = 'discord:' + user_id
    if action == 'bamtat':
        enabled = options.get('bat')
        if type(enabled) is not bool: raise ValueError('Chọn bật hoặc tạm dừng.')
        if enabled and not settings.okx_ad_pricing_enabled:
            return 'Công tắc trên Render đang tắt; cần bật OKX_AD_PRICING_ENABLED trên Render trước.'
        db.merge(OKXPricingSwitch(id=1, enabled=enabled))
        db.add(OKXAuditLog(action='OKX_PRICING_SWITCH', actor=actor, detail=str(enabled)))
        db.commit()
        return ('Đã bật bám giá theo các slot đang Auto.' if enabled else 'Đã tạm dừng bám giá cả mua và bán. Yêu cầu đã gửi trước đó có thể vẫn hoàn tất. Không ẩn quảng cáo trên OKX.')
    side = options.get('chieu')
    if side not in {'BUY','SELL'}: raise ValueError('Chọn chiều mua hoặc bán.')
    if action == 'danhsach':
        from .okx_sync import sign
        from .okx_release import OKXRequestError, safe_code
        from datetime import datetime, timezone
        path = '/api/v5/p2p/ad/active-list?' + urlencode({'side':side.lower(),'cryptoCurrency':'USDT','fiatCurrency':'VND'})
        stamp = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(settings.okx_base_url.rstrip('/')+path, headers={'OK-ACCESS-KEY':settings.okx_api_key,'OK-ACCESS-TIMESTAMP':stamp,'OK-ACCESS-PASSPHRASE':settings.okx_api_passphrase,'OK-ACCESS-SIGN':sign(settings.okx_api_secret,stamp,path)})
            payload = response.json()
            if response.status_code != 200 or str(payload.get('code')) != '0':
                raise OKXRequestError('Không đọc được quảng cáo; mã API: '+safe_code(payload.get('code')))
            rows = payload.get('data')
            if not isinstance(rows,list): raise ValueError('Danh sách quảng cáo không hợp lệ.')
            result=[]
            for item in rows[:20]:
                code=str(item.get('adId',''))
                if not re.fullmatch(r'[0-9]{1,30}',code): continue
                row=await read_ad(client,code)
                if row['side'] == side.lower():
                    result.append(f"ID {code} · giá {number(row['unitPrice'])}đ · {'đang ẩn' if row.get('isHidden') else 'đang hiển thị'}")
        return '\n'.join(result) or 'Chưa có quảng cáo hoạt động cho chiều này.'
    slot_number = options.get('slot')
    if type(slot_number) is not int or slot_number not in {1,2}: raise ValueError('Chọn slot 1 hoặc 2.')
    slot = db.scalar(select(OKXSlot).where(OKXSlot.side==side, OKXSlot.slot_number==slot_number))
    if slot is None: raise ValueError('Slot chưa được cấu hình trên hệ thống.')
    from .okx_ad_worker import OKXAdPriceAttempt
    attempt = db.get(OKXAdPriceAttempt,slot.id)
    if action == 'auto':
        enabled=options.get('bat')
        if type(enabled) is not bool: raise ValueError('Chọn bật hoặc tắt Auto.')
        if enabled:
            if attempt and attempt.state != 'IDLE': raise ValueError('Slot có yêu cầu chưa rõ kết quả; cần kiểm tra OKX trước, không tự gửi lại.')
            async with httpx.AsyncClient(timeout=30) as client: row=await read_ad(client,slot.adv_id)
            if row['side'] != side.lower() or row.get('type')!='limit' or row.get('status')!='new': raise ValueError('Quảng cáo sai chiều hoặc không còn hoạt động với giá cố định.')
        db.execute(update(OKXSlot).where(OKXSlot.id==slot.id).values(auto_enabled=enabled))
        detail=f'{side} slot {slot_number}: Auto={enabled}'
    elif action == 'quangcao':
        code=options.get('id')
        if not isinstance(code,str) or not re.fullmatch(r'[0-9]{1,30}',code): raise ValueError('ID quảng cáo phải là số.')
        if attempt and attempt.state != 'IDLE': raise ValueError('Không đổi ID khi yêu cầu cũ chưa xác định kết quả.')
        async with httpx.AsyncClient(timeout=30) as client: row=await read_ad(client,code)
        if row['side'] != side.lower() or row.get('type')!='limit' or row.get('status')!='new': raise ValueError('Quảng cáo sai chiều hoặc không còn hoạt động với giá cố định.')
        if db.scalar(select(OKXSlot.id).where(OKXSlot.id!=slot.id,OKXSlot.adv_id==code)): raise ValueError('ID này đã được gán cho slot khác.')
        db.execute(update(OKXSlot).where(OKXSlot.id==slot.id).values(adv_id=code,current_price=number(row['unitPrice']),auto_enabled=False))
        detail=f'{side} slot {slot_number}: ID={code}; Auto=False'
    else: raise ValueError('Lệnh không được hỗ trợ.')
    db.add(OKXAuditLog(action='OKX_PRICING_SLOT_UPDATED',actor=actor,detail=detail))
    db.commit()
    return 'Đã lưu '+detail+'\nĐổi ID luôn tắt Auto; dùng /gia auto để bật sau khi kiểm tra. Bot không tự mở quảng cáo bạn đã ẩn.'


def get_preferences(db):
    row = db.get(OKXPricePreferences, 1)
    if row is None:
        row = OKXPricePreferences(id=1, buy_rank=10, sell_rank=10)
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            row = db.get(OKXPricePreferences, 1)
    return row


def preferences_dict(db):
    row = get_preferences(db)
    return {"buy_rank": row.buy_rank, "sell_rank": row.sell_rank}


def selected_rank(db, side):
    if side not in {"BUY", "SELL"}:
        raise ValueError("Chọn BUY hoặc SELL.")
    return validate_rank(getattr(get_preferences(db), side.lower() + "_rank"))


def save_ranks(db, changes, actor="dashboard"):
    if settings.database_schema == "kayhap":
        raise ValueError("Bám giá hiện chỉ hỗ trợ tài khoản gốc.")
    if not changes or set(changes) - {"buy_rank", "sell_rank"}:
        raise ValueError("Chỉ cập nhật vị trí mua hoặc bán.")
    for rank in changes.values(): validate_rank(rank)
    get_preferences(db)
    # Update only selected fields so simultaneous BUY/SELL edits do not overwrite each other.
    db.execute(update(OKXPricePreferences).where(OKXPricePreferences.id == 1).values(**changes))
    db.add(OKXAuditLog(action="OKX_PRICE_RANK_UPDATED", actor=actor, detail=str(changes)))
    db.commit()
    db.expire_all()
    return preferences_dict(db)


def parse_price_command(data):
    commands = data.get("options", [])
    if len(commands) != 1 or not isinstance(commands[0], dict):
        raise ValueError("Dùng /gia vitri, /gia xem hoặc /gia trangthai.")
    command = commands[0]
    action = command.get("name")
    if action not in {"vitri", "xem", "trangthai"}:
        raise ValueError("Lệnh giá không được hỗ trợ.")
    options = {item.get("name"): item.get("value") for item in command.get("options", []) if isinstance(item, dict)}
    side = options.get("chieu")
    if action != "trangthai" and side not in {"BUY", "SELL"}:
        raise ValueError("Chọn chiều Mua hoặc Bán.")
    rank = validate_rank(options.get("vitri")) if action == "vitri" else None
    return action, side, rank


async def execute_price_command(data, db, user_id):
    if settings.database_schema == "kayhap":
        return "Bám giá hiện chỉ hỗ trợ tài khoản gốc."
    commands=data.get('options',[])
    if len(commands)==1 and commands[0].get('name') in {'bamtat','danhsach','quangcao','auto'}:
        return await manage_price_command(data,db,user_id)
    action, side, rank = parse_price_command(data)
    if action == "vitri":
        save_ranks(db, {side.lower()+"_rank": rank}, actor="discord:"+user_id)
    prefs = preferences_dict(db)
    status = "ĐANG BẬT" if pricing_active(db) else "ĐANG TẮT — chỉ lưu vị trí, chưa sửa quảng cáo thật"
    reply = f"Bám giá OKX: {status}.\nVị trí mua: {prefs['buy_rank']} · vị trí bán: {prefs['sell_rank']}.\nGiá bán luôn phải đạt giá mua hoàn tất gần nhất +10đ."
    if action == "trangthai":
        for slot in db.scalars(select(OKXSlot).order_by(OKXSlot.side,OKXSlot.slot_number)):
            reply += f"\n{slot.side} slot {slot.slot_number}: Auto {'bật' if slot.auto_enabled else 'tắt'} · ID {slot.adv_id or 'chưa chọn'} · giá {slot.current_price}đ"
        return reply
    if action == "vitri": reply = "Đã lưu vị trí. Bot sẽ áp dụng ở lần quét kế tiếp khi bám giá được bật.\n"+reply
    rank = selected_rank(db, side)
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            cost = await read_last_buy_cost(client) if side == "SELL" else None
            plan = await read_rank_plan(client, side, cost["unit_price"] if cost else None, target_rank=rank)
        reply += f"\n{side} · vị trí {rank} · giá tham chiếu: {plan.get('proposed_price') or 'chưa có'}đ/USDT."
        if cost: reply += f"\nGiá vốn {cost['unit_price']}đ · bán tối thiểu {plan.get('minimum_sell_price') or 'chưa xác minh'}đ."
        reply += "\n" + {"UPDATE_PRICE":"Nguồn giá hợp lệ để bám theo khi tính năng được bật.","HIDE_AD":"Giá bán dưới mức tối thiểu; bot sẽ giữ/tạm ẩn khi bám giá được bật.","BLOCKED":"Chưa đủ dữ liệu; chưa sửa/ẩn theo nguồn này."}[plan["action"]]
    except Exception as error:
        reply += "\nVị trí vẫn đã lưu; chưa xác minh được giá tham chiếu: " + error_detail(error)
    return reply + "\nGiá hiển thị là giá dự kiến; bot áp dụng theo trạng thái bám giá ở trên."

"""Primary-account web/Discord controls; no exchange calls."""
import time
from datetime import datetime, timezone

from .config import settings


def enabled():
    return (__package__ == 'app' and settings.database_schema in {'','public'}
            and settings.primary_web_control_enabled)


def set_release(db, value, actor):
    from .release_controls import switch_row
    from .models import OKXAuditLog
    if not enabled():
        raise ValueError('Điều khiển web chỉ dành cho tài khoản chính.')
    row=switch_row(db,lock=True)
    row.enabled=bool(value)
    # Order web actions against delayed Discord snowflakes, using the same lock.
    stamp=((int(time.time()*1000)-1420070400000)<<22)|((1<<22)-1)
    row.last_interaction_id=str(max(stamp,int(row.last_interaction_id)+1))
    row.actor=actor;row.updated_at=datetime.now(timezone.utc)
    db.add(OKXAuditLog(action='RELEASE_AUTO_SWITCH',actor=actor,detail='enabled='+str(row.enabled)))
    return row


def stop_all(db, actor):
    from .models import OKXPricingSwitch, OKXAuditLog
    from .mexc_price_state import MexcPricingSwitch
    set_release(db,False,actor)
    db.merge(OKXPricingSwitch(id=1,enabled=False))
    db.merge(MexcPricingSwitch(id=1,enabled=False))
    db.add(OKXAuditLog(action='PRIMARY_EMERGENCY_STOP',actor=actor,detail='release and OKX/MEXC pricing stopped'))
    db.commit()
    return 'Đã dừng Auto mở khóa và bám giá OKX/MEXC của bạn. Vẫn nhận tiền, đối chiếu và gửi thông báo. Yêu cầu đã gửi trước đó có thể vẫn hoàn tất; quảng cáo trên sàn giữ nguyên.'


def status(db):
    from .models import ReleaseAutoSwitch,OKXPricingSwitch
    from .mexc_price_state import MexcPricingSwitch
    rows=[('Auto mở khóa',db.get(ReleaseAutoSwitch,1)),('Bám giá OKX',db.get(OKXPricingSwitch,1)),('Bám giá MEXC',db.get(MexcPricingSwitch,1))]
    return '\n'.join(label+': '+('công tắc BẬT' if row is None or row.enabled else 'công tắc TẮT') for label,row in rows)+'\nMở dashboard để xem Auto từng slot, trạng thái sàn và lý do dừng.'


def panel(prefix, exchange):
    if not enabled():
        return None
    return {'content':f'{exchange} · Điều khiển chính trên dashboard\nDiscord giữ thông báo, cảnh báo và dừng Auto nhanh. Web và Discord dùng chung công tắc, cấu hình và hồ sơ xác minh.',
        'allowed_mentions':{'parse':[]},'components':[{'type':1,'components':[
            {'type':2,'style':5,'label':'Mở dashboard','url':(settings.public_base_url or 'https://p2p-order-matcher.onrender.com').rstrip('/')+'/dashboard'},
            {'type':2,'style':4,'label':'Dừng toàn bộ Auto','custom_id':prefix+':stopall'},
            {'type':2,'style':2,'label':'Trạng thái','custom_id':prefix+':status'}]}]}

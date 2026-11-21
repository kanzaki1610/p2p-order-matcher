"""Validated, read-only OKX market reference for the first 20 competitor ads."""
from decimal import Decimal
from .okx_rank import plan_rank_price
from .okx_ads import number


def market_reference(data, side, *, own_names, now):
    if not own_names or not all(isinstance(x,str) and x.strip() for x in own_names):
        raise ValueError('Chưa có biệt danh chính chủ để loại quảng cáo của mình.')
    validation=plan_rank_price(data, side, own_nickname=own_names[0],
        own_aliases=own_names[1:], now=now, target_rank=20, enforce_cost=False)
    if validation['action']=='BLOCKED':
        raise ValueError(validation['reason'])
    ours={x.strip().casefold() for x in own_names}
    rows=[r for r in data['rows'] if r['nick'].strip().casefold() not in ours][:20]
    mean=sum((number(r['price']) for r in rows),Decimal(0))/Decimal(20)
    return {'mean':mean, 'source_ts':float(data['saved_ts']), 'count':20,
        'excluded':validation['own_ads_excluded']}

import time
import uuid
from decimal import ROUND_CEILING, ROUND_FLOOR
from sqlalchemy import String, Integer, Numeric, Float
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base

MODE = '__OKX_MARKET20__'


class MarketCycle(Base):
    __tablename__ = 'okx_market_cycles'
    key: Mapped[str] = mapped_column(String(24),primary_key=True)
    revision: Mapped[str] = mapped_column(String(32))
    cycle: Mapped[int] = mapped_column(Integer,default=-1)
    mean: Mapped[Decimal | None] = mapped_column(Numeric(30,12),nullable=True)
    source_ts: Mapped[float] = mapped_column(Float,default=0)
    staged_cycle: Mapped[int] = mapped_column(Integer,default=-1)
    staged_price: Mapped[Decimal | None] = mapped_column(Numeric(20,0),nullable=True)
    staged_ad: Mapped[str] = mapped_column(String(40),default='')
    base_price: Mapped[Decimal | None] = mapped_column(Numeric(20,0),nullable=True)
    staged_mean: Mapped[Decimal | None] = mapped_column(Numeric(30,12),nullable=True)
    change_percent: Mapped[Decimal | None] = mapped_column(Numeric(30,12),nullable=True)


def is_market(follow):
    return bool(follow and follow[0]==MODE)


def save_mode(db,side,slot,actor):
    from .nickname_target import NicknameTarget, clear, key
    from .models import OKXSlot, OKXAuditLog
    from sqlalchemy import select
    row=db.scalar(select(OKXSlot).where(OKXSlot.side==side,OKXSlot.slot_number==slot))
    if row is None or not row.adv_id:raise ValueError('Chọn quảng cáo cho slot trước.')
    clear(db,'OKX',side,slot)
    code=key('OKX',side,slot)
    db.merge(NicknameTarget(key=code,nickname=MODE,offset=0,revision=uuid.uuid4().hex))
    db.add(OKXAuditLog(action='OKX_MARKET_MODE',actor=actor,detail=code))
    db.commit()
    return 'Đã chọn bám % biến động trung bình 20 quảng cáo đối thủ. Lấy mốc giây 0/30, chuẩn bị giây 25/55, áp dụng ở chu kỳ kế tiếp. Giữ công tắc Auto hiện tại.'


def advance(db,key,revision,ad,side,current,reference,now):
    cycle=int(now//30); phase=now-cycle*30
    row=db.get(MarketCycle,key,populate_existing=True)
    if row is None or row.revision!=revision:
        row=MarketCycle(key=key,revision=revision,cycle=-1,staged_cycle=-1,staged_ad='',source_ts=0)
        db.merge(row);db.flush();row=db.get(MarketCycle,key,populate_existing=True)
    # The preceding cycle's staged assignment has a five-second send window.
    ready=(phase<5 and row.staged_cycle==cycle-1 and row.staged_ad==ad
        and row.staged_price is not None and number(current)==row.base_price)
    proposed=row.staged_price if ready else None
    change=row.change_percent if ready else None
    mean=reference['mean'];source=reference['source_ts']
    if phase<10 and source>=cycle*30-5 and row.cycle!=cycle:
        row.cycle=cycle;row.mean=mean;row.source_ts=source
    if (25<=phase<30 and row.cycle==cycle and row.mean is not None
            and source>=cycle*30+20 and source>row.source_ts):
        raw=number(current)*mean/row.mean
        row.staged_price=raw.to_integral_value(rounding=ROUND_CEILING if side=='SELL' else ROUND_FLOOR)
        row.staged_mean=mean;row.change_percent=(mean/row.mean-1)*100
        row.base_price=number(current);row.staged_ad=ad;row.staged_cycle=cycle
    db.commit()
    return proposed,change


async def read_plan(db,client,slot,follow,cost=None,enforce_cost=True,now=None,preview=False,current=None):
    from .config import settings
    now=time.time() if now is None else now
    blocked={'action':'WAIT_MARKET','proposed_price':None,'reason':'Đang lấy mốc thị trường hoặc chờ giây 30; chưa đến cửa sổ đổi giá.'}
    if preview:
        row=db.get(MarketCycle,f'OKX:{slot.side}:{slot.slot_number}',populate_existing=True)
        if row and row.revision==follow[2] and row.staged_price is not None:
            return {**blocked,'reason':f'Mốc thị trường: {row.mean:,.3f}đ; mẫu chuẩn bị: {row.staged_mean:,.3f}đ; biến động {row.change_percent:+.6f}%. Giá đã chuẩn bị {row.staged_price:,.0f}đ. Bot chỉ gửi ở cửa sổ đầu chu kỳ 30 giây khi mẫu còn hiệu lực.'}
        return blocked
    response=await client.get('https://autop2p.biz/',params={'api':'panel','panel':'okx_'+slot.side.lower()})
    response.raise_for_status()
    ref=market_reference(response.json(),slot.side,own_names=[x.strip() for x in settings.okx_merchant_nicknames.split(',') if x.strip()],now=now)
    if now-ref['source_ts']>10:raise ValueError('Nguồn thị trường quá 10 giây; chờ mẫu mới.')
    price,change=advance(db,f'OKX:{slot.side}:{slot.slot_number}',follow[2],slot.adv_id,slot.side,current,ref,now)
    if price is None:return blocked
    result={'action':'UPDATE_PRICE','proposed_price':str(price),'market_change_percent':str(change),'market_due':int(now//30)*30,
        'reason':f'Thị trường 20 quảng cáo thay đổi {change:+.6f}%; giá mới {price:,.0f}đ.','target_rank':20}
    if slot.side=='SELL' and enforce_cost:
        if cost is None:return {**blocked,'action':'BLOCKED','reason':'Chưa nhập giá vốn; không đổi giá.'}
        if price<number(cost)+10:return {**result,'action':'HIDE_AD','reason':'Giá thị trường dưới giá vốn +10đ; tạm ẩn theo bảo vệ giá vốn.'}
    return result

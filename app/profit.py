"""Primary-account P2P reporting. Exchange I/O is authenticated GET-only.

Reporting and FIFO use verified completion time consistently on both venues. Only
positively completed USDT/VND orders enter this separate reporting ledger.
It never reads bank beneficiaries or modifies matching/release/pricing state.
"""
import asyncio
import calendar
from collections import deque
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
import json
import re
import uuid
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import DateTime, Numeric, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from .config import settings
from .database import Base, SessionLocal

VN=ZoneInfo('Asia/Ho_Chi_Minh')
ZERO=Decimal(0)


class ProfitTrade(Base):
    __tablename__='profit_trades'
    key: Mapped[str]=mapped_column(String(100),primary_key=True)
    exchange: Mapped[str]=mapped_column(String(10),index=True)
    code: Mapped[str]=mapped_column(String(60))
    side: Mapped[str]=mapped_column(String(4))
    quantity: Mapped[Decimal]=mapped_column(Numeric(28,8))
    fiat: Mapped[Decimal]=mapped_column(Numeric(28,8))
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),index=True)
    completed_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),index=True)


class ProfitOpening(Base):
    __tablename__='profit_opening'
    id: Mapped[int]=mapped_column(primary_key=True)
    day: Mapped[str]=mapped_column(String(10))
    quantity: Mapped[Decimal]=mapped_column(Numeric(28,8))
    unit_cost: Mapped[Decimal]=mapped_column(Numeric(28,8))


class ProfitSyncJob(Base):
    __tablename__='profit_sync_jobs'
    id: Mapped[str]=mapped_column(String(32),primary_key=True)
    state: Mapped[str]=mapped_column(String(20))
    start_day: Mapped[str]=mapped_column(String(10))
    end_day: Mapped[str]=mapped_column(String(10))
    started_at: Mapped[datetime]=mapped_column(DateTime(timezone=True))
    result_json: Mapped[str]=mapped_column(Text,default='{}')


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def day_range(start,end):
    try:
        a,b=date.fromisoformat(start),date.fromisoformat(end)
    except (ValueError,TypeError):raise ValueError('Ngày phải có dạng YYYY-MM-DD.') from None
    if a>b or (b-a).days>366:raise ValueError('Chọn khoảng ngày hợp lệ, tối đa 367 ngày mỗi lần.')
    if b>datetime.now(VN).date():raise ValueError('Chỉ thống kê đến ngày hôm nay, theo giờ Việt Nam.')
    return datetime.combine(a,time.min,VN).astimezone(timezone.utc),datetime.combine(b+timedelta(days=1),time.min,VN).astimezone(timezone.utc)


def decimal(value,*,zero=False):
    try:
        n=Decimal(str(value))
        if not n.is_finite() or n<0 or (not zero and n==0) or n>=Decimal('1000000000000000'):
            raise ValueError()
        if n.as_tuple().exponent < -8:raise ValueError()
        return n
    except (InvalidOperation,ValueError,TypeError):raise ValueError('Số tiền/số USDT thiếu hoặc không hợp lệ.') from None


def stamp(value):
    try:
        n=Decimal(str(value))
        if not n.is_finite() or n<Decimal('1000000000000'):raise ValueError()
        result=datetime.fromtimestamp(float(n/1000),timezone.utc)
        if result>datetime.now(timezone.utc)+timedelta(minutes=1):raise ValueError()
        return result
    except (ValueError,TypeError,InvalidOperation,OverflowError,OSError):
        raise ValueError('Thiếu/sai thời gian tạo lệnh; chưa nhập vào sổ thống kê.') from None


def normalize(exchange,row,*,role='MAKER'):
    if not isinstance(row,dict):raise ValueError('Dòng lịch sử không hợp lệ.')
    if exchange=='OKX':
        if (str(row.get('cryptoCurrency')).upper()!='USDT' or str(row.get('fiatCurrency')).upper()!='VND'
                or row.get('orderStatus')!='completed' or row.get('paymentStatus')!='confirmed'):return None
        code=str(row.get('orderId') or '')
        side=str(row.get('side')).upper()
        q,f,created=row.get('cryptoAmount'),row.get('fiatAmount'),row.get('createdTimestamp')
        times=[stamp(row[k]) for k in ('completionTimestamp','completedTimestamp') if row.get(k)]
        if not times or len(set(times))!=1:raise ValueError('Thiếu/mâu thuẫn thời điểm hoàn tất OKX; chưa nhập lệnh.')
        completed=row.get('completionTimestamp') or row.get('completedTimestamp')
        pattern=r'[0-9]{5,40}'
    elif exchange=='MEXC':
        if row.get('coinName')!='USDT' or row.get('fiatUnit')!='VND' or row.get('state')!='DONE':return None
        api_side=str(row.get('side'))
        incoming=(settings.mexc_p2p_api_incoming_side or settings.mexc_p2p_incoming_side).upper()
        own=settings.mexc_p2p_incoming_side.upper()
        if api_side not in {'BUY','SELL'} or incoming not in {'BUY','SELL'} or own not in {'BUY','SELL'}:
            raise ValueError('Chưa xác minh chiều maker MEXC; chưa tính lãi.')
        if role not in {'MAKER','ACCOUNT'}:raise ValueError('Chưa xác minh vai trò giao dịch MEXC.')
        side=api_side if role=='ACCOUNT' else (own if api_side==incoming else ('BUY' if own=='SELL' else 'SELL'))
        code=str(row.get('advOrderNo') or '')
        q,f,created=row.get('tradableQuantity'),row.get('amount'),row.get('createTime')
        completed=row.get('finishTime')
        pattern=r'd[0-9]{5,40}'
    else:raise ValueError('Sàn không được hỗ trợ.')
    if not re.fullmatch(pattern,code) or side not in {'BUY','SELL'}:raise ValueError('Lịch sử thiếu mã hoặc chiều lệnh.')
    creation,completion=stamp(created),stamp(completed)
    if completion<creation:raise ValueError('Thời điểm hoàn tất trước thời điểm tạo; chưa nhập lệnh vào sổ.')
    return {'key':exchange+':'+code,'exchange':exchange,'code':code,'side':side,
            'quantity':decimal(q),'fiat':decimal(f),'created_at':creation,'completed_at':completion}


def okx_history_floor(now=None):
    now=(now or datetime.now(timezone.utc)).astimezone(VN)
    year,month=divmod(now.year*12+now.month-1-6,12)
    return datetime(year,month+1,min(now.day,calendar.monthrange(year,month+1)[1]),tzinfo=VN).astimezone(timezone.utc)


async def okx_page(client,page,start=None,end=None):
    from .okx_sync import sign
    from .okx_cost import history_rows
    params={'side':'all','pageIndex':page,'pageSize':100,'completionStatus':'completed',
            'cryptoCurrencies':'USDT','fiatCurrencies':'VND'}
    if start is not None:params['start']=int(start.timestamp()*1000)
    if end is not None:params['end']=int(end.timestamp()*1000)-1
    path='/api/v5/p2p/order/list?'+urlencode(params)
    for attempt in range(3):
        timestamp=datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
        try:
            r=await client.get(settings.okx_base_url.rstrip('/')+path,headers={
                'OK-ACCESS-KEY':settings.okx_api_key,'OK-ACCESS-PASSPHRASE':settings.okx_api_passphrase,
                'OK-ACCESS-TIMESTAMP':timestamp,'OK-ACCESS-SIGN':sign(settings.okx_api_secret,timestamp,path)})
        except httpx.TransportError:
            if attempt==2:raise
            await asyncio.sleep(attempt+1);continue
        if r.status_code in {429,500,502,503,504} and attempt<2:
            await asyncio.sleep(attempt+1);continue
        if r.status_code!=200:raise ValueError('Không đọc được lịch sử OKX; thử đồng bộ lại sau.')
        payload=r.json()
        if isinstance(payload,dict) and str(payload.get('code'))=='50011' and attempt<2:
            await asyncio.sleep(attempt+1);continue
        if not isinstance(payload,dict) or str(payload.get('code'))!='0':raise ValueError('OKX từ chối đọc lịch sử.')
        return history_rows(payload.get('data'))


async def collect(exchange,start,end,api):
    """Read a bounded, deduplicated snapshot; no successful partial coverage."""
    result={};seen=set()
    if exchange=='MEXC':return await collect_mexc(start,end,api)
    if exchange!='OKX':raise ValueError('Sàn không được hỗ trợ.')
    if exchange=='OKX' and start<okx_history_floor():
        raise ValueError('API OKX chỉ cung cấp lịch sử 6 tháng gần nhất; chọn ngày đồng bộ gần hơn. Lịch sử đã lưu vẫn được giữ.')
    for page in range(1,101):
        rows=await okx_page(api,page,start,end)
        if not isinstance(rows,list) or any(not isinstance(r,dict) for r in rows):raise ValueError('Lịch sử sai cấu trúc.')
        if not rows:return list(result.values())
        for row in rows:
            raw_id=str(row.get('orderId' if exchange=='OKX' else 'advOrderNo') or '')
            if not raw_id or raw_id in seen:raise ValueError('Lịch sử lặp mã giữa các trang; hãy đồng bộ lại.')
            seen.add(raw_id)
            trade=normalize(exchange,row)
            if trade and start<=trade['created_at']<end:result[trade['key']]=trade
        await asyncio.sleep(.2)
    raise ValueError('Lịch sử vượt 10.000 lệnh trong một lần; chưa xác nhận đồng bộ đủ.')


async def mexc_pages(api,path,start,end,*,done_only):
    rows=[];seen=set();cursor={}
    for _ in range(100):
        params={'startTime':int(start.timestamp()*1000),'endTime':int(end.timestamp()*1000)-1,'limit':100,**cursor}
        if done_only:params['orderDealState']='DONE'
        batch=await api.request(path,params=params)
        if not isinstance(batch,list) or any(not isinstance(r,dict) for r in batch):raise ValueError('Lịch sử MEXC sai cấu trúc.')
        if not batch:return rows
        for row in batch:
            code=str(row.get('advOrderNo') or '')
            if not re.fullmatch(r'd[0-9]{5,40}',code) or code in seen:raise ValueError('Lịch sử MEXC thiếu/trùng mã; đồng bộ lại.')
            stamp(row.get('createTime'));seen.add(code);rows.append(row)
        cursor={'lastCreateTime':int(batch[-1]['createTime']),'lastId':str(batch[-1]['advOrderNo'])}
        await asyncio.sleep(.2)
    raise ValueError('Lịch sử MEXC vượt 10.000 lệnh mỗi nguồn; chưa xác nhận đọc đủ.')


async def collect_mexc(start,end,api):
    # Read all-account completed history first, then a complete maker membership
    # snapshot (all states). Never infer maker/taker from a counterparty's badge.
    market=await mexc_pages(api,'/api/v3/fiat/market/order/paginationV2',start,end,done_only=True)
    makers=await mexc_pages(api,'/api/v3/fiat/merchant/order/paginationV2',start,end,done_only=False)
    own={str(r['advOrderNo']):r for r in makers};result=[]
    for row in market:
        if row.get('coinName')!='USDT' or row.get('fiatUnit')!='VND' or row.get('state')!='DONE':continue
        code=str(row['advOrderNo']);maker=own.get(code)
        if maker is not None:
            if maker.get('state')!='DONE':continue
            if any(str(row.get(k))!=str(maker.get(k)) for k in ('coinName','fiatUnit')) or stamp(row.get('createTime'))!=stamp(maker.get('createTime')):
                raise ValueError('Hai nguồn lịch sử MEXC chưa khớp; chưa xác minh chiều lệnh.')
            if decimal(row.get('amount'))!=decimal(maker.get('amount')) or decimal(row.get('tradableQuantity'))!=decimal(maker.get('tradableQuantity')):
                raise ValueError('Hai nguồn lịch sử MEXC chưa khớp số tiền/số USDT.')
        # The all-account endpoint returns account-side labels, unlike the
        # legacy maker endpoint. Verify the conversion for owned maker rows.
        trade=normalize('MEXC',row,role='ACCOUNT')
        if maker is not None:
            verified=normalize('MEXC',{**maker,'finishTime':row.get('finishTime')})
            if not verified or verified['side']!=trade['side']:
                raise ValueError('Chiều lịch sử tài khoản và maker MEXC chưa khớp; chưa tính lãi.')
        else:
            # An order absent from the complete maker snapshot is a taker
            # candidate; authenticate its detail before accepting its direction.
            detail=await api.request('/api/v3/fiat/order/detail',params={'advOrderNo':code})
            if (not isinstance(detail,dict) or str(detail.get('advOrderNo'))!=code
                    or detail.get('side')!=row.get('side') or detail.get('state')!='DONE'
                    or detail.get('coinName')!='USDT' or detail.get('fiatUnit')!='VND'
                    or stamp(detail.get('createTime'))!=trade['created_at']
                    or decimal(detail.get('amount'))!=trade['fiat']
                    or decimal(detail.get('tradableQuantity'))!=trade['quantity']):
                raise ValueError('Chưa xác minh chi tiết/chiều lệnh taker MEXC; chưa nhập vào sổ.')
        if trade and start<=trade['created_at']<end:result.append(trade)
    return result


async def run_sync(job_id):
    from .mexc import MexcP2PClient
    with SessionLocal() as db:
        job=db.get(ProfitSyncJob,job_id)
        if job is None or job.state!='RUNNING':return
        start,end=day_range(job.start_day,job.end_day)
        db.commit()  # Do not occupy a DB connection while paging the exchange.
        result={}
        for exchange,on in [('OKX',settings.okx_p2p_enabled),('MEXC',settings.mexc_p2p_enabled)]:
            if not on:
                result[exchange]={'ok':False,'message':'Sàn chưa bật kết nối API.'};continue
            try:
                if exchange=='OKX':
                    async with httpx.AsyncClient(timeout=20,follow_redirects=False) as client:
                        rows=await collect(exchange,start,end,client)
                else:
                    async with MexcP2PClient() as client:rows=await collect(exchange,start,end,client)
                latest=db.scalar(select(ProfitSyncJob).order_by(ProfitSyncJob.started_at.desc()).limit(1))
                if latest.id!=job_id:
                    job=db.get(ProfitSyncJob,job_id);job.state='SUPERSEDED';db.commit();return
                # Replace only this successfully read time range, removing stale
                # records if authenticated history no longer confirms completion.
                old=db.scalars(select(ProfitTrade).where(ProfitTrade.exchange==exchange,
                    ProfitTrade.created_at>=start,ProfitTrade.created_at<end)).all()
                ids={r['key'] for r in rows}
                for row in old:
                    if row.key not in ids:db.delete(row)
                for row in rows:db.merge(ProfitTrade(**row))
                db.commit()
                result[exchange]={'ok':True,'count':len(rows)}
            except Exception as error:
                db.rollback()
                # Never persist arbitrary API response text, URLs or credentials.
                result[exchange]={'ok':False,'message':str(error) if type(error) is ValueError else 'Chưa đọc hết lịch sử từ sàn; thử đồng bộ lại.'}
            job=db.get(ProfitSyncJob,job_id)
            job.result_json=json.dumps(result,ensure_ascii=False);db.commit()
        job.state='DONE' if any(r.get('ok') for r in result.values()) else 'FAILED'
        db.commit()


def job_state(job):
    if not job:return None
    state=job.state
    if state=='RUNNING' and datetime.now(timezone.utc)-utc(job.started_at)>timedelta(minutes=10):state='INTERRUPTED'
    return {'id':job.id,'state':state,'from':job.start_day,'to':job.end_day,
            'started':utc(job.started_at).timestamp(),'results':json.loads(job.result_json)}


def start_sync(db,start,end):
    day_range(start,end)
    latest=db.scalar(select(ProfitSyncJob).order_by(ProfitSyncJob.started_at.desc()).limit(1))
    if latest and job_state(latest)['state']=='RUNNING':return latest,False
    job=ProfitSyncJob(id=uuid.uuid4().hex,state='RUNNING',start_day=start,end_day=end,
                      started_at=datetime.now(timezone.utc),result_json='{}')
    db.add(job);db.commit();return job,True


def save_opening(db,day,quantity,unit_cost):
    day_range(day,day)
    q,c=decimal(quantity,zero=True),decimal(unit_cost,zero=True)
    if q>0 and c<=0:raise ValueError('Nhập giá vốn dương cho USDT đầu kỳ.')
    db.merge(ProfitOpening(id=1,day=day,quantity=q,unit_cost=c));db.commit()


def text(n):
    if n is None:return None
    return format(n.quantize(Decimal('.00000001')),'f')


def calculate(trades,start,end,opening=None,exchange='ALL'):
    """FIFO pool shared by both exchanges; presentation filters never reset cost."""
    lots=deque();rows=[];buckets={};totals={'buy_fiat':ZERO,'sell_fiat':ZERO,'buy_qty':ZERO,
        'sell_qty':ZERO,'cost':ZERO,'known_profit':ZERO,'missing_qty':ZERO,'buy_count':0,'sell_count':0}
    anchor=datetime.combine(date.fromisoformat(opening.day),time.min,VN).astimezone(timezone.utc) if opening else None
    seeded=False
    ordered=sorted(trades,key=lambda t:(utc(t.completed_at),0 if t.side=='SELL' else 1,t.key))
    with localcontext() as ctx:
        ctx.prec=50
        for t in ordered:
            moment=utc(t.completed_at)
            if moment>=end:break
            if anchor and not seeded and anchor<=moment:
                lots.clear()
                if opening.quantity>0:lots.append([opening.quantity,opening.unit_cost])
                seeded=True
            valid_period=not anchor or moment>=anchor
            cost=ZERO;missing=ZERO;known_profit=ZERO
            q,f=Decimal(t.quantity),Decimal(t.fiat)
            if t.side=='BUY':
                if valid_period:lots.append([q,f/q])
            else:
                remaining=q
                if valid_period:
                    while remaining>0 and lots:
                        take=min(remaining,lots[0][0]);cost+=take*lots[0][1]
                        remaining-=take;lots[0][0]-=take
                        if lots[0][0]==0:lots.popleft()
                missing=remaining
                known_profit=f*(q-missing)/q-cost
            if moment<start or (exchange!='ALL' and t.exchange!=exchange):continue
            day=moment.astimezone(VN).date().isoformat()
            b=buckets.setdefault(day,{'date':day,'buy_fiat':ZERO,'sell_fiat':ZERO,'known_profit':ZERO,'missing_qty':ZERO,'orders':0})
            b['orders']+=1
            side=t.side.lower()
            totals[side+'_fiat']+=f;totals[side+'_qty']+=q;totals[side+'_count']+=1;b[side+'_fiat']+=f
            if t.side=='SELL':
                totals['cost']+=cost;totals['known_profit']+=known_profit;totals['missing_qty']+=missing
                b['known_profit']+=known_profit;b['missing_qty']+=missing
            rows.append({'exchange':t.exchange,'code':t.code,'side':t.side,'created':utc(t.created_at).timestamp(),'completed':moment.timestamp(),
                'quantity':text(q),'fiat':text(f),'price':text(f/q),'cost':text(cost) if t.side=='SELL' else None,
                'profit':text(known_profit) if t.side=='SELL' and missing==0 else None,'missing_qty':text(missing)})
        if anchor and not seeded and anchor<end:
            lots.clear()
            if opening.quantity>0:lots.append([opening.quantity,opening.unit_cost])
        inventory=sum((q for q,c in lots),ZERO);inventory_cost=sum((q*c for q,c in lots),ZERO)
        totals['inventory_qty']=inventory;totals['inventory_cost']=inventory_cost
        totals['profit']=None if totals['missing_qty'] else totals['known_profit']
        return {'totals':{k:text(v) if isinstance(v,Decimal) or v is None else v for k,v in totals.items()},
                'daily':[{**{k:text(v) if isinstance(v,Decimal) else v for k,v in b.items()},
                          'profit':None if b['missing_qty'] else text(b['known_profit'])} for b in sorted(buckets.values(),key=lambda b:b['date'])],
                'rows':list(reversed(rows))}


def report(db,start_day,end_day,exchange='ALL',page=1):
    start,end=day_range(start_day,end_day)
    opening=db.get(ProfitOpening,1)
    query=select(ProfitTrade).where(ProfitTrade.completed_at<end)
    if opening:
        anchor=datetime.combine(date.fromisoformat(opening.day),time.min,VN).astimezone(timezone.utc)
        query=query.where(ProfitTrade.completed_at>=min(anchor,start))
    trades=db.scalars(query.limit(100001)).all()
    if len(trades)>100000:raise ValueError('Sổ vượt giới hạn tính trực tiếp; cần chia lại mốc vốn.')
    result=calculate(trades,start,end,opening,exchange)
    all_rows=result.pop('rows');result['row_count']=len(all_rows)
    result['rows']=all_rows[(page-1)*100:page*100];result['page']=page
    result['opening']={'day':opening.day,'quantity':text(opening.quantity),'unit_cost':text(opening.unit_cost)} if opening else None
    jobs=db.scalars(select(ProfitSyncJob).order_by(ProfitSyncJob.started_at.desc()).limit(100)).all()
    result['sync']=job_state(jobs[0]) if jobs else None
    venues=['OKX','MEXC'] if exchange=='ALL' else [exchange]
    coverage={}
    for venue in venues:
        cursor=date.fromisoformat(start_day)
        intervals=sorted((date.fromisoformat(j.start_day),date.fromisoformat(j.end_day)) for j in jobs
                         if json.loads(j.result_json).get(venue,{}).get('ok'))
        for a,b in intervals:
            if a<=cursor<=b:cursor=b+timedelta(days=1)
        coverage[venue]=cursor>date.fromisoformat(end_day)
    result['coverage']=coverage
    result['history_first']=min((utc(t.created_at).timestamp() for t in trades),default=None)
    result['warnings']=[]
    if not all(coverage.values()):result['warnings'].append('Chưa xác nhận đã đồng bộ đủ lịch sử của kỳ đang xem. Bấm Đồng bộ lịch sử.')
    if Decimal(result['totals']['missing_qty'])>0:result['warnings'].append('Có lệnh bán thiếu giá vốn. Tổng lãi chưa xác định; nhập vốn đầu kỳ hoặc đồng bộ thêm lịch sử mua trước kỳ.')
    if not opening:result['warnings'].append('Chưa khai báo vốn đầu kỳ: giá vốn chỉ dựa trên lịch sử mua đã nhập được. Kiểm tra mốc bắt đầu lịch sử trước khi dùng kết quả.')
    result['warnings'].append('Lãi gộp FIFO theo ngày hoàn tất, giờ Việt Nam; chưa trừ phí. Giá vốn dùng chung OKX + MEXC. USDT ngoài lịch sử cần khai báo vốn đầu kỳ.')
    result['warnings'].append('Đọc thêm lịch sử trước kỳ để đủ giá vốn và lệnh tạo trước kỳ nhưng hoàn tất trong kỳ. Tồn sổ không phải số dư ví trên sàn.')
    return result

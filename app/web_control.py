"""Authenticated primary dashboard facade over the existing pricing controls."""
import json
import logging
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import select

from .config import settings
from .database import get_db
from .dashboard import verify_dashboard_key
from .background_io import isolated_io
from . import primary_controls, discord_price_panel as okx_ui, mexc_price_panel as mexc_ui
from .models import OKXSlot, OKXPricePreferences, OKXPricingSwitch, OKXManualCost, OKXAuditLog, ReleaseAutoSwitch
from .mexc_pricing import MexcPriceSlot, MexcPriceSettings, rank_plan
from .mexc_price_state import MexcPricingSwitch
from .okx_ad_worker import OKXAdPriceAttempt
from .okx_ad_limits import OKXAutoLimits
from .mexc_price_state import MexcAutoLimits
from .nickname_target import target, plan_options
from .okx_rank import read_rank_plan, plan_rank_price
from .diagnostics import error_detail

OWNER='web:primary'
logger=logging.getLogger('uvicorn.error.web_control')


def verify_primary(_=Depends(verify_dashboard_key)):
    if not primary_controls.enabled():
        raise HTTPException(404,'Dashboard điều khiển chỉ bật cho tài khoản chính.')


class PrivateJSONResponse(JSONResponse):
    def __init__(self,*args,**kwargs):
        kwargs['headers']={**(kwargs.get('headers') or {}),'Cache-Control':'no-store'}
        super().__init__(*args,**kwargs)


router=APIRouter(prefix='/dashboard/api/control',dependencies=[Depends(verify_primary)],default_response_class=PrivateJSONResponse)


class Action(BaseModel):
    model_config=ConfigDict(extra='forbid')
    action: Literal['ad','rank','follow','cost','limits','manual','manual_confirm','manual_cancel',
        'recover','adopt_preview','adopt_confirm','adopt_cancel','auto','pause','resume','stop_all','release']
    exchange: Literal['OKX','MEXC']='OKX'
    side: Literal['BUY','SELL']='BUY'
    slot: int=Field(default=1,ge=1,le=2,strict=True)
    fields: dict[str,str]=Field(default_factory=dict,max_length=6)
    enabled: StrictBool | None=None
    acknowledge: StrictBool=False
    control_version: str=Field(default='',max_length=30,pattern=r'^[0-9]{0,30}$')
    draft_id: str=Field(default='',max_length=32,pattern=r'^(?:[0-9a-f]{32})?$')


def safe_error(error):
    from .mexc import MexcAPIError
    from .okx_release import OKXRequestError, failure_reason
    if type(error) is ValueError or isinstance(error,MexcAPIError):
        return str(error)[:700]
    if isinstance(error,OKXRequestError):
        return failure_reason(error)
    return error_detail(error)


def stamp(value):
    if value is None:return None
    if isinstance(value,datetime):
        return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).timestamp()
    return float(value)


def snapshot(db):
    prefs=db.get(OKXPricePreferences,1)
    costs={'OKX':db.get(OKXManualCost,1),'MEXC':db.get(MexcPriceSettings,1)}
    switches={'OKX':db.get(OKXPricingSwitch,1),'MEXC':db.get(MexcPricingSwitch,1)}
    release=db.get(ReleaseAutoSwitch,1)
    result={'account':'kanzaki','read_at':time.time(),'release_enabled':release.enabled if release else True,
        'control_version':release.last_interaction_id if release else '0',
        'release_ready':{'OKX':settings.okx_auto_release_enabled,
                         'MEXC':settings.mexc_auto_release_enabled and settings.mexc_p2p_live_writes},'exchanges':{}}
    for exchange in ['OKX','MEXC']:
        cost=costs[exchange]
        value=(cost.unit_price if cost and cost.enabled else None) if exchange=='OKX' else cost.cost if cost else None
        ready=settings.okx_ad_pricing_enabled if exchange=='OKX' else settings.mexc_ad_pricing_enabled
        switch=switches[exchange]
        block={'enabled_on_server':ready,'pricing_enabled':switch.enabled if switch else True,
            'cost':str(value) if value is not None else None,'scan_seconds':max(5,settings.okx_ad_pricing_seconds if exchange=='OKX' else settings.mexc_ad_pricing_seconds),
            'write_seconds':max(30,settings.okx_ad_min_update_seconds if exchange=='OKX' else settings.mexc_ad_min_update_seconds),'slots':[]}
        for side in ['BUY','SELL']:
            for n in [1,2]:
                slot=(db.scalar(select(OKXSlot).where(OKXSlot.side==side,OKXSlot.slot_number==n)) if exchange=='OKX' else db.get(MexcPriceSlot,f'{side}:{n}'))
                attempt=db.get(OKXAdPriceAttempt,slot.id) if slot and exchange=='OKX' else slot
                follow=target(db,exchange,side,n)
                limits=db.get(OKXAutoLimits,slot.id) if slot and exchange=='OKX' else db.get(MexcAutoLimits,f'{side}:{n}') if slot else None
                rank=(getattr(prefs,side.lower()+'_rank') if prefs else 10) if exchange=='OKX' else slot.rank if slot else 10
                block['slots'].append({'side':side,'slot':n,'ad_id':(slot.adv_id if exchange=='OKX' else slot.ad) if slot else '',
                    'auto':(slot.auto_enabled if exchange=='OKX' else slot.enabled) if slot else False,
                    'state':attempt.state if attempt else 'IDLE','reason':attempt.reason if attempt else None,
                    'last_action':stamp(attempt.last_action_at if exchange=='OKX' else attempt.last_action) if attempt else None,
                    'rank':rank,'mode':'nickname' if follow else 'rank','nickname':follow[0] if follow else '',
                    'offset':follow[1] if follow else 0,'bound':follow[3] if follow else 0,
                    'sell_min':str(limits.minimum) if limits else None})
        result['exchanges'][exchange]=block
    events=db.scalars(select(OKXAuditLog).order_by(OKXAuditLog.id.desc()).limit(15)).all()
    result['events']=[{'action':r.action,'at':stamp(r.created_at),'source':'Web' if 'web:' in r.actor or r.actor.startswith('dashboard') else 'Discord / Bot'} for r in events]
    return result


@router.get('/state')
def state(db=Depends(get_db)):
    return snapshot(db)


def form(prefix, name, side, values):
    return {'type':5,'data':{'custom_id':f'{prefix}:form:{name}:{side}',
        'components':[{'type':1,'components':[{'custom_id':k,'value':v}]} for k,v in values.items()]}}


def translate(result, exchange, side, slot):
    # Return typed, narrowly recognized choices. Never accept raw custom IDs from the web.
    choices=[]
    for row in result.get('components',[]):
        for button in row.get('components',[]):
            parts=button.get('custom_id','').split(':')
            if len(parts)==3 and parts[1] in {'confirm','cancel','adoptconfirm','adoptcancel'} and re.fullmatch(r'[0-9a-f]{32}',parts[2]):
                name={'confirm':'manual_confirm','cancel':'manual_cancel','adoptconfirm':'adopt_confirm','adoptcancel':'adopt_cancel'}[parts[1]]
                choices.append({'label':button['label'],'action':name,'draft_id':parts[2],'exchange':exchange,'side':side,'slot':slot})
            elif len(parts)==4 and parts[1] in {'checkslot','adoptpreview'} and parts[2] in {'BUY','SELL'} and parts[3] in {'1','2'}:
                choices.append({'label':button['label'],'action':'recover' if parts[1]=='checkslot' else 'adopt_preview',
                                'exchange':exchange,'side':parts[2],'slot':int(parts[3])})
            elif len(parts)==5 and parts[1]=='setauto' and parts[2] in {'BUY','SELL'} and parts[3] in {'1','2'} and parts[4] in {'0','1'}:
                choices.append({'label':button['label'],'action':'auto','enabled':parts[4]=='1','exchange':exchange,'side':parts[2],'slot':int(parts[3])})
    return {'message':result.get('content',''),'choices':choices}


async def dispatch(db, data):
    action,exchange,side,n=data.action,data.exchange,data.side,data.slot
    enabling=action=='resume' or action in {'auto','release'} and data.enabled is True
    if (enabling or action=='manual_confirm') and not data.acknowledge:
        raise ValueError('Bạn cần xác nhận thao tác này trước khi cho phép thay đổi thật.')
    if enabling:
        from .release_controls import switch_row
        guard=switch_row(db,lock=True)
        if not data.control_version or guard.last_interaction_id!=data.control_version:
            raise ValueError('Công tắc dừng vừa thay đổi. Làm mới trạng thái rồi xác nhận lại; không dùng yêu cầu bật cũ.')
    if action in {'manual_confirm','manual_cancel','adopt_confirm','adopt_cancel'} and not data.draft_id:
        raise ValueError('Thiếu bản xem trước cần xác nhận.')
    if action in {'auto','release'} and data.enabled is None:
        raise ValueError('Chọn bật hoặc tắt.')
    if action=='stop_all':return {'message':primary_controls.stop_all(db,OWNER),'choices':[]}
    if action=='release':
        primary_controls.set_release(db,data.enabled,OWNER);db.commit()
        return {'message':'Auto mở khóa '+('bật' if data.enabled else 'tắt')+' cho tài khoản của bạn. Yêu cầu đã gửi trước đó có thể vẫn hoàn tất.','choices':[]}
    prefix='price' if exchange=='OKX' else 'mprice'
    ui=okx_ui if exchange=='OKX' else mexc_ui
    allowed={'ad':{'ad_id'},'rank':{'rank'},'follow':{'target','offset','bound'},'cost':{'cost'},
             'limits':{'minimum'},'manual':{'price','quantity','minimum','maximum'}}
    values={k:str(v).strip() for k,v in data.fields.items()}
    if set(values)-allowed.get(action,set()) or any(len(v)>150 for v in values.values()):
        raise ValueError('Trường cấu hình không hợp lệ.')
    if action in {'ad','rank','follow','cost','limits','manual'}:
        if action=='rank' and exchange=='OKX':
            from .okx_price_controls import save_ranks
            raw=values.get('rank','')
            if not re.fullmatch(r'[0-9]{1,2}',raw):raise ValueError('Vị trí phải từ 1 đến 50.')
            save_ranks(db,{side.lower()+'_rank':int(raw)},actor=OWNER)
            return {'message':'Đã lưu vị trí OKX '+side+': '+raw+'. Áp dụng cả hai slot cùng chiều; giữ nguyên công tắc Auto.','choices':[]}
        values['slot']=str(n)
        name={'manual':'price'}.get(action,action)
        if action=='ad':
            values['value' if exchange=='OKX' else 'ad']=values.pop('ad_id','')
        if action in {'rank','cost','manual'} and exchange=='OKX':
            values['value']=values.pop({'rank':'rank','cost':'cost','manual':'price'}[action],'')
        payload=form(prefix,name,side,values)
    else:
        codes={'auto':f'setauto:{side}:{n}:{1 if data.enabled else 0}',
               'pause':'pause','resume':'resume','recover':f'checkslot:{side}:{n}',
               'adopt_preview':f'adoptpreview:{side}:{n}',
               'adopt_confirm':'adoptconfirm:'+data.draft_id,'adopt_cancel':'adoptcancel:'+data.draft_id,
               'manual_confirm':'confirm:'+data.draft_id,'manual_cancel':'cancel:'+data.draft_id}
        if exchange=='OKX' and action.startswith('adopt_'):
            raise ValueError('OKX dùng Kiểm tra/khôi phục để xác minh yêu cầu cũ.')
        payload={'type':3,'data':{'custom_id':prefix+':'+codes[action]}}
    result=await ui.execute_panel(payload,db,OWNER)
    db.add(OKXAuditLog(action='WEB_CONTROL_'+action.upper(),actor=OWNER,detail=f'{exchange} {side} slot {n}'))
    db.commit()
    return translate(result,exchange,side,n)


@router.post('/action')
@isolated_io
async def act(data:Action,db=Depends(get_db)):
    try:return await dispatch(db,data)
    except Exception as error:
        db.rollback()
        logger.warning('Web control failed action=%s type=%s',data.action,type(error).__name__)
        raise HTTPException(400,safe_error(error)) from None


def amount(value):
    if value is None:return None
    result=Decimal(str(value))
    if not result.is_finite() or result<0:raise ValueError('Số lượng hoặc giá từ sàn chưa hợp lệ.')
    return str(result)


@router.get('/live/{exchange}/{side}/{n}')
@isolated_io
async def live(exchange:Literal['OKX','MEXC'],side:Literal['BUY','SELL'],n:int,db=Depends(get_db)):
    if n not in {1,2}:raise HTTPException(400,'Chọn slot 1 hoặc 2.')
    slot=db.scalar(select(OKXSlot).where(OKXSlot.side==side,OKXSlot.slot_number==n)) if exchange=='OKX' else db.get(MexcPriceSlot,f'{side}:{n}')
    code=(slot.adv_id if exchange=='OKX' else slot.ad) if slot else ''
    if not code:raise HTTPException(400,'Slot chưa có quảng cáo. Bấm Chọn quảng cáo trước.')
    try:
        follow=target(db,exchange,side,n)
        async with httpx.AsyncClient(timeout=25,follow_redirects=False) as client:
            if exchange=='OKX':
                from .okx_ads import read_ad
                row=await read_ad(client,code)
                if row['side']!=side.lower():raise ValueError('Quảng cáo không đúng chiều của slot.')
                ad={'ad_id':code,'status':row['status'],'hidden':row['isHidden'],'price':amount(row['unitPrice']),
                    'quantity':okx_ui.ad_quantity(row),'available':amount(row.get('availableAmount')),
                    'frozen':amount(row.get('onHoldAmount')),'minimum':amount(row.get('minOrderSize')),'maximum':amount(row.get('maxOrderSize'))}
                cost=db.get(OKXManualCost,1)
                basis=cost.unit_price if cost and cost.enabled else None
                prefs=db.get(OKXPricePreferences,1)
                rank=getattr(prefs,side.lower()+'_rank') if prefs else 10
                async def plan():return await read_rank_plan(client,side,basis if side=='SELL' else None,target_rank=rank,**plan_options(follow))
            else:
                from .mexc import MexcP2PClient
                from .mexc_ads import read_ad
                async with MexcP2PClient() as api:row=await read_ad(api,code,side)
                ad={'ad_id':code,'status':row['advStatus'],'hidden':row['advStatus']!='OPEN','price':amount(row['price']),
                    'quantity':amount(row.get('quantity')),'available':amount(row.get('availableQuantity')),
                    'frozen':amount(row.get('frozenQuantity')),'minimum':amount(row.get('minSingleTransAmount')),'maximum':amount(row.get('maxSingleTransAmount'))}
                cost=db.get(MexcPriceSettings,1)
                async def plan():return await rank_plan(client,slot,cost.cost if cost and side=='SELL' else None,follow=follow)
            try:proposal=await plan()
            except Exception as error:proposal={'action':'BLOCKED','reason':safe_error(error),'proposed_price':None}
        return {'exchange':exchange,'side':side,'slot':n,'read_at':time.time(),'ad':ad,
            'plan':{k:proposal.get(k) for k in ('action','reason','proposed_price','competitor','minimum_sell_price','bound_applied')}}
    except Exception as error:raise HTTPException(400,safe_error(error)) from None


@router.get('/market/{exchange}/{side}')
@isolated_io
async def market(exchange:Literal['OKX','MEXC'],side:Literal['BUY','SELL']):
    try:
        async with httpx.AsyncClient(timeout=25,follow_redirects=False) as client:
            response=await client.get('https://autop2p.biz/',params={'api':'panel','panel':exchange.lower()+'_'+side.lower()})
            response.raise_for_status();data=response.json()
        if not isinstance(data,dict) or data.get('exchange')!=exchange.lower() or data.get('panel')!=exchange.lower()+'_'+side.lower() or str(data.get('side','')).upper()!=side:
            raise ValueError('Bảng giá trả về sai sàn hoặc sai chiều.')
        normalized={**data,'exchange':'okx','panel':'okx_'+side.lower(),'side':side.lower()}
        if exchange=='MEXC' and normalized.get('cache')=='new':normalized['cache']='fresh'
        check=plan_rank_price(normalized,side,Decimal('1'),own_nickname='__READ_ONLY_SOURCE_CHECK__',target_rank=1)
        if check['action']=='BLOCKED':raise ValueError(check['reason'])
        rows=[{'nickname':x['nick'],'price':amount(x['price']),'minimum':amount(x.get('min')),'maximum':amount(x.get('max'))} for x in data['rows'][:100]]
        return {'read_at':time.time(),'rows':rows}
    except Exception as error:raise HTTPException(400,safe_error(error)) from None


@router.get('/ads/{exchange}/{side}')
@isolated_io
async def ads(exchange:Literal['OKX','MEXC'],side:Literal['BUY','SELL']):
    try:
        if exchange=='MEXC':
            from .mexc import MexcP2PClient
            from .mexc_ads import my_ads
            async with MexcP2PClient() as api:rows=await my_ads(api)
            result=[{'ad_id':r['advNo'],'price':amount(r['price']),'status':r['advStatus'],'available':amount(r.get('availableQuantity'))}
                for r in rows if r.get('side')==side and r.get('coinName')=='USDT' and r.get('fiatUnit')=='VND']
        else:
            from .okx_sync import sign
            from .okx_release import OKXRequestError, safe_code
            path='/api/v5/p2p/ad/active-list?'+urlencode({'side':side.lower(),'cryptoCurrency':'USDT','fiatCurrency':'VND'})
            timestamp=datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
            async with httpx.AsyncClient(timeout=25) as client:
                response=await client.get(settings.okx_base_url.rstrip('/')+path,headers={
                    'OK-ACCESS-KEY':settings.okx_api_key,'OK-ACCESS-TIMESTAMP':timestamp,
                    'OK-ACCESS-PASSPHRASE':settings.okx_api_passphrase,
                    'OK-ACCESS-SIGN':sign(settings.okx_api_secret,timestamp,path)})
                payload=response.json()
            if response.status_code!=200 or not isinstance(payload,dict) or str(payload.get('code'))!='0':
                raise OKXRequestError(f'OKX HTTP {response.status_code}; mã API: '+safe_code(payload.get('code') if isinstance(payload,dict) else None)+'.')
            rows=payload.get('data')
            if not isinstance(rows,list):raise ValueError('Danh sách quảng cáo OKX chưa đúng cấu trúc.')
            result=[{'ad_id':str(r['adId']),'price':amount(r.get('unitPrice')),'status':r.get('status','new'),'available':amount(r.get('availableAmount'))}
                    for r in rows if isinstance(r,dict) and re.fullmatch(r'[0-9]{1,30}',str(r.get('adId',''))) and r.get('side',side.lower())==side.lower()]
        return {'rows':result[:20]}
    except Exception as error:raise HTTPException(400,safe_error(error)) from None

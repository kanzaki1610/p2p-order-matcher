import asyncio
import time
from types import SimpleNamespace
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app import mexc_pricing as worker
from app import mexc_ads as ads
from app import mexc_price_panel as panel
from app.models import NotificationDelivery
from app.mexc import MexcP2PClient


def ad(side='SELL', status='OPEN'):
    return dict(advNo='a123',side=side,advStatus=status,coinName='USDT',coinId='usdt-id',
        fiatUnit='VND',price='26080',quantity='100',payTimeLimit=15,
        minSingleTransAmount='100000',maxSingleTransAmount='2000000',
        userAllTradeCountMin=3,userAllTradeCountMax=0,payMethod='1',
        paymentInfo=[{'id':777,'payMethod':1}],paymethodIds='1,2',
        tradeTerms='Keep terms',autoReplyMsg='Keep reply',kycLevel='ADVANCED',
        supportKycCountry='VN',onlyTradeKybUser=False,requireMobile=False,blockTrade=False)


@pytest.fixture
def db(monkeypatch):
    engine=create_engine('sqlite://');Base.metadata.create_all(engine)
    monkeypatch.setattr(worker.settings,'database_schema','public')
    monkeypatch.setattr(worker.settings,'mexc_ad_pricing_enabled',True)
    monkeypatch.setattr(worker.settings,'mexc_ad_pricing_seconds',5)
    monkeypatch.setattr(worker.settings,'discord_webhook_mexc_pricing','test-hook')
    monkeypatch.setattr(worker,'valid_webhook',lambda value:bool(value))
    monkeypatch.setattr(worker,'send_discord_message',AsyncMock(return_value=True))
    with Session(engine,expire_on_commit=False) as session:yield session
    engine.dispose()


@pytest.fixture
def setup(db,monkeypatch):
    config=worker.cost_settings(db);config.cost=Decimal('26050')
    slot=worker.MexcPriceSlot(key='SELL:1',ad='a123',rank=10,nickname='VNTrader',enabled=True)
    db.add(slot);db.commit()
    current=ad()
    async def read(*args):return dict(current)
    monkeypatch.setattr(worker,'read_ad',read)
    monkeypatch.setattr(worker,'rank_plan',AsyncMock(return_value={'action':'UPDATE_PRICE','proposed_price':'26070'}))
    async def price(api,row,value):current['price']=str(value)
    async def visibility(api,code,visible):current['advStatus']='OPEN' if visible else 'CLOSE'
    monkeypatch.setattr(worker,'write_price',AsyncMock(side_effect=price))
    monkeypatch.setattr(worker,'write_visibility',AsyncMock(side_effect=visibility))
    return slot,current


def run(db):asyncio.run(worker.process_pricing(db,None,None))


def test_update_preserves_terms_and_payment_account():
    row=ad();body=ads.update_body(row,Decimal('26070'))
    assert body['price']=='26070' and body['initQuantity']=='100'
    assert body['payMethod']=='777' and 'paymethodIds' not in body
    for key in ['tradeTerms','autoReplyMsg','minSingleTransAmount','maxSingleTransAmount','kycLevel']:
        assert body[key]==str(row[key])
    assert not {'supplyQuantity','reduceQuantity','advStatus'}.intersection(body)


def test_buy_uses_method_types_not_sell_bank_accounts():
    body=ads.update_body(ad('BUY'),Decimal('26070'))
    assert body['paymethodIds']=='1,2' and 'payMethod' not in body


@pytest.mark.parametrize('field',['coinId','payTimeLimit','quantity','paymentInfo'])
def test_missing_required_original_configuration_blocks(field):
    row=ad();row.pop(field)
    with pytest.raises(ValueError):ads.update_body(row,Decimal('26070'))


def test_success_reads_back_and_does_not_repeat_unchanged_price(db,setup):
    slot,current=setup;run(db);slot.last_action=0;db.commit();run(db)
    worker.write_price.assert_awaited_once()
    assert slot.state=='IDLE' and current['price']=='26070'


def test_timeout_is_durable_and_never_resubmitted(db,setup,monkeypatch):
    slot,current=setup
    monkeypatch.setattr(worker,'write_price',AsyncMock(side_effect=httpx.ReadTimeout('secret URL')))
    run(db);db.expire_all();run(db);run(db)
    worker.write_price.assert_awaited_once()
    assert slot.state=='UNKNOWN'
    worker.send_discord_message.assert_awaited_once()
    assert 'secret URL' not in worker.send_discord_message.call_args.args[1]


def test_manually_closed_ad_is_not_reopened(db,setup):
    slot,current=setup;current['advStatus']='CLOSE';run(db)
    worker.write_price.assert_not_awaited();worker.write_visibility.assert_not_awaited()


def test_floor_hides_then_reprices_and_reopens_only_bot_closed_ad(db,setup,monkeypatch):
    slot,current=setup
    worker.rank_plan.return_value={'action':'HIDE_AD','proposed_price':'26059'}
    run(db);assert slot.paused_by_floor and current['advStatus']=='CLOSE'
    slot.last_action=0;db.commit()
    worker.rank_plan.return_value={'action':'UPDATE_PRICE','proposed_price':'26070'}
    run(db);assert current['price']=='26070' and current['advStatus']=='CLOSE'
    slot.last_action=0;db.commit();run(db)
    assert current['advStatus']=='OPEN' and not slot.paused_by_floor


@pytest.mark.parametrize('guard',['disabled','kayhap','webhook','cost'])
def test_missing_permission_or_cost_never_writes(db,setup,monkeypatch,guard):
    if guard=='disabled':monkeypatch.setattr(worker.settings,'mexc_ad_pricing_enabled',False)
    if guard=='kayhap':monkeypatch.setattr(worker.settings,'database_schema','kayhap')
    if guard=='webhook':monkeypatch.setattr(worker.settings,'discord_webhook_mexc_pricing','')
    if guard=='cost':worker.cost_settings(db).cost=None;db.commit()
    run(db);worker.write_price.assert_not_awaited();worker.write_visibility.assert_not_awaited()


def test_cost_changed_during_source_read_prevents_stale_write(db,setup,monkeypatch):
    async def changed(*args):
        config=worker.cost_settings(db);config.cost=Decimal('27000');config.revision='changed';db.commit()
        return {'action':'UPDATE_PRICE','proposed_price':'26070'}
    monkeypatch.setattr(worker,'rank_plan',changed)
    run(db);worker.write_price.assert_not_awaited()


def test_cost_changed_during_final_ad_read_prevents_stale_write(db,setup,monkeypatch):
    slot,current=setup;calls=[]
    async def changed(*args):
        calls.append(1)
        if len(calls)==2:
            config=worker.cost_settings(db);config.revision='changed';config.cost=Decimal('27000');db.commit()
        return dict(current)
    monkeypatch.setattr(worker,'read_ad',changed)
    run(db);worker.write_price.assert_not_awaited()


def test_mexc_source_requires_own_exchange_freshness_and_nickname_exclusion(db,setup):
    slot,current=setup;slot.rank=1
    data={'ok':True,'exchange':'mexc','side':'SELL','panel':'mexc_sell','cache':'new','age_sec':0,
        'saved_ts':time.time(),'rows':[{'nick':'VNTrader','price':'26010'},{'nick':'Other','price':'26070'}]}
    async def get(*args,**kwargs):return httpx.Response(200,json=data,request=httpx.Request('GET','https://autop2p.biz/'))
    source=SimpleNamespace(get=get)
    result=asyncio.run(REAL_RANK_PLAN(source,slot,Decimal('26050')))
    assert result['proposed_price']=='26070'
    data['exchange']='okx'
    with pytest.raises(ValueError):asyncio.run(REAL_RANK_PLAN(source,slot,Decimal('26050')))


def test_pricing_post_uses_query_signing_and_is_never_retried(monkeypatch):
    calls=[]
    def handler(request):
        calls.append(request)
        assert request.method=='POST' and request.url.params['advNo']=='a123'
        assert request.url.params['price']=='26070' and request.content==b''
        assert request.headers['X-MEXC-APIKEY']=='fake-key'
        return httpx.Response(503,json={'code':500,'msg':'unavailable'})
    async def execute():
        async with MexcP2PClient(api_key='fake-key',api_secret='fake-secret',transport=httpx.MockTransport(handler)) as api:
            with pytest.raises(Exception):await ads.write_price(api,ad(),Decimal('26070'))
    asyncio.run(execute());assert len(calls)==1


def test_panel_uses_only_mexc_ids_and_separate_cost(db):
    board=panel.panel()
    assert all(button['custom_id'].startswith('mprice:') for row in board['components'] for button in row['components'])
    payload={'type':5,'data':{'custom_id':'mprice:form:cost:SELL','components':[{'components':[{'custom_id':'cost','value':'25905'}]}]}}
    result=asyncio.run(panel.execute_panel(payload,db,'owner'))
    assert worker.cost_settings(db).cost==Decimal('25905') and '25,915' in result['content']
    from app.models import OKXManualCost
    assert db.get(OKXManualCost,1) is None


def test_signed_discord_mexc_controls_are_owner_and_channel_isolated(monkeypatch):
    import json
    from fastapi.testclient import TestClient
    from nacl.signing import SigningKey
    from app.main import app
    from app import discord_commands as commands
    key=SigningKey.generate()
    for name,value in dict(discord_commands_enabled=False,discord_pricing_commands_enabled=True,
        discord_public_key=key.verify_key.encode().hex(),discord_application_id='app',discord_guild_id='guild',
        discord_command_channel_ids='okx-channel',discord_mexc_pricing_channel_id='mexc-channel',
        discord_allowed_user_ids='owner',discord_allowed_role_ids='',database_schema='public').items():
        monkeypatch.setattr(worker.settings,name,value)
    client=TestClient(app)
    def post(payload):
        raw=json.dumps(payload).encode();stamp=str(int(time.time()))
        return client.post('/webhooks/discord',content=raw,headers={'X-Signature-Timestamp':stamp,
            'X-Signature-Ed25519':key.sign(stamp.encode()+raw).signature.hex()}).json()
    payload=dict(type=3,id='test',token='fake',application_id='app',guild_id='guild',channel_id='mexc-channel',
        member={'user':{'id':'owner'},'roles':[]},data={'custom_id':'mprice:config:SELL'})
    assert post(payload)['type']==9
    assert post(dict(payload,channel_id='okx-channel'))['type']==4
    assert post(dict(payload,data={'custom_id':'price:cost:SELL'}))['type']==4
    assert post(dict(payload,member={'user':{'id':'other'},'roles':[]}))['type']==4
    assert post(dict(payload,guild_id='other'))['type']==4
    monkeypatch.setattr(worker.settings,'database_schema','kayhap')
    assert post(payload)['type']==4


def test_stale_or_incomplete_market_data_does_not_propose_a_price(db,setup):
    slot,current=setup;slot.rank=1
    data={'ok':True,'exchange':'mexc','side':'SELL','panel':'mexc_sell','cache':'fresh','age_sec':100,
        'saved_ts':time.time()-100,'rows':[{'nick':'Other','price':'26070'}]}
    async def get(*args,**kwargs):return httpx.Response(200,json=data,request=httpx.Request('GET','https://autop2p.biz/'))
    assert asyncio.run(REAL_RANK_PLAN(SimpleNamespace(get=get),slot,Decimal('26050')))['action']=='BLOCKED'


def test_unknown_slot_cannot_be_reconfigured_to_resubmit(db,setup):
    slot,current=setup;slot.state='UNKNOWN';db.commit()
    payload={'type':5,'data':{'custom_id':'mprice:form:config:SELL','components':[
        {'components':[{'custom_id':k,'value':v}]} for k,v in {'slot':'1','ad':'a999','rank':'10','nickname':'VNTrader'}.items()]}}
    with pytest.raises(ValueError,match='chưa rõ'):asyncio.run(panel.execute_panel(payload,db,'owner'))
    assert slot.ad=='a123' and slot.state=='UNKNOWN'


def test_stale_confirmation_cannot_reset_newer_attempt(db,setup,monkeypatch):
    slot,current=setup
    slot.state='UNKNOWN';slot.attempt_id='old';slot.wanted_price=Decimal('26070');slot.wanted_status='OPEN';db.commit()
    async def newer(*args):
        slot.attempt_id='new';slot.wanted_price=Decimal('26090');db.commit()
        return dict(current,price='26070')
    monkeypatch.setattr(worker,'read_ad',newer)
    assert not asyncio.run(worker.confirm(db,None,slot))
    assert slot.state=='UNKNOWN' and slot.attempt_id=='new'


def test_late_post_response_cannot_overwrite_newer_attempt(db,setup,monkeypatch):
    slot,current=setup
    async def newer(*args):
        slot.attempt_id='new';slot.state='UNKNOWN';slot.wanted_price=Decimal('26090');db.commit()
    monkeypatch.setattr(worker,'write_price',AsyncMock(side_effect=newer))
    run(db)
    assert slot.state=='UNKNOWN' and slot.attempt_id=='new' and slot.wanted_price==Decimal('26090')


REAL_RANK_PLAN=worker.rank_plan

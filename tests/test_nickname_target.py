import asyncio
import time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import OKXSlot
from app.nickname_target import NicknameTarget, save, target, clear, validate_target
from app.okx_rank import plan_rank_price, read_rank_plan
from app import discord_price_panel as okx_ui, mexc_price_panel as mexc_ui
from app.mexc_pricing import MexcPriceSlot, rank_plan


def board(side='SELL', rows=None):
    return dict(ok=True, exchange='okx', side=side.lower(), panel='okx_'+side.lower(),
                saved_ts=time.time(), age_sec=0, cache='fresh',
                rows=rows or [{'nick':'VNTrader','price':'20000'},
                    {'nick':'other','price':'26000'}, {'nick':'1phut30s','price':'26090'}])


@pytest.mark.parametrize('side', ['BUY', 'SELL'])
@pytest.mark.parametrize('offset, expected', [('-1','26089'), ('+1','26091'), ('','26090'), ('0','26090'), ('-90','26000')])
def test_signed_offset_same_meaning_for_both_sides(side, offset, expected):
    name, delta = validate_target('  1phut30s ',offset)
    result=plan_rank_price(board(side),side,'25000',target_nickname=name,price_offset=delta)
    assert result['action']=='UPDATE_PRICE'
    assert Decimal(result['proposed_price'])==Decimal(expected)
    assert result['competitor']['price']=='26090'


@pytest.mark.parametrize('rows', [[], [{'nick':'1phut30s-extra','price':'26090'}],
    [{'nick':'1phut30s','price':'26090'},{'nick':'1phut30s','price':'26091'}]])
def test_missing_or_conflicting_target_never_falls_back_to_rank(rows):
    result=plan_rank_price(board(rows=rows or [{'nick':'other','price':'26000'}]),'SELL','25000',
                           target_rank=1,target_nickname='1phut30s',price_offset=-1)
    assert result['action']=='BLOCKED' and result['proposed_price'] is None


def test_case_insensitive_exact_match_and_duplicate_equal_price():
    result=plan_rank_price(board('BUY',rows=[{'nick':'  1PHUT30S ','price':'26090'},
        {'nick':'1phut30s','price':'26090'}]),'BUY',target_nickname='1phut30s')
    assert result['proposed_price']=='26090'


def test_sell_floor_applies_after_offset():
    result=plan_rank_price(board(),'SELL','26080',target_nickname='1phut30s',price_offset=-1)
    assert result['action']=='HIDE_AD' and result['proposed_price']=='26089'
    assert result['minimum_sell_price']=='26090'


@pytest.mark.parametrize('change', ['stale','wrong_side','wrong_exchange','zero_price','own'])
def test_existing_source_guards_still_apply(change):
    data=board()
    nick='1phut30s'; delta=-1
    if change=='stale': data['saved_ts']-=60
    if change=='wrong_side':data['side']='buy'
    if change=='wrong_exchange':data['exchange']='mexc'
    if change=='zero_price':delta=-26090
    if change=='own':nick='VNTrader'
    assert plan_rank_price(data,'SELL','25000',target_nickname=nick,price_offset=delta)['action']=='BLOCKED'


@pytest.mark.parametrize('offset',['1.5','-1.0','abc','1,000','NaN','--1','+','1e3'])
def test_invalid_offset_rejected(offset):
    with pytest.raises(ValueError):validate_target('someone',offset)


@pytest.fixture
def db():
    engine=create_engine('sqlite://');Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(OKXSlot(side='BUY',slot_number=1,adv_id='123',auto_enabled=True))
        session.add(OKXSlot(side='SELL',slot_number=1,adv_id='456',auto_enabled=False))
        session.add(MexcPriceSlot(key='BUY:1',ad='a123',nickname='VNTrader',enabled=True))
        session.commit();yield session
    engine.dispose()


@pytest.mark.parametrize('exchange',['OKX','MEXC'])
def test_persist_per_exchange_side_slot_and_auto_unchanged(db,exchange):
    result=save(db,exchange,'BUY',{'slot':'1','target':'1phut30s','offset':'-1'},'owner')
    assert '-1đ' in result['content']
    assert target(db,exchange,'BUY',1)[:2]==('1phut30s',-1)
    assert target(db,exchange,'SELL',1) is None
    assert target(db,exchange,'BUY',2) is None
    assert target(db,'MEXC' if exchange=='OKX' else 'OKX','BUY',1) is None
    assert db.query(OKXSlot).filter_by(side='BUY').one().auto_enabled
    assert db.get(MexcPriceSlot,'BUY:1').enabled
    assert db.query(OKXSlot).filter_by(side='SELL').one().auto_enabled is False


def test_rank_switch_removes_only_selected_exchange_side(db):
    from app.okx_price_controls import save_ranks
    for ex in ['OKX','MEXC']:save(db,ex,'BUY',{'slot':'1','target':'other','offset':''},'owner')
    save(db,'OKX','SELL',{'slot':'1','target':'other','offset':''},'owner')
    save_ranks(db,{'buy_rank':9})
    assert target(db,'OKX','BUY',1) is None
    assert target(db,'OKX','SELL',1) is not None
    assert target(db,'MEXC','BUY',1) is not None


@pytest.mark.parametrize('ui,prefix',[(okx_ui,'price'),(mexc_ui,'mprice')])
def test_buttons_and_optional_offset_modal(ui,prefix):
    panel=ui.panel()
    assert len(panel['components'])<=5
    assert all(len(row['components'])<=5 for row in panel['components'])
    for side in ['BUY','SELL']:
        assert any(b['custom_id']==f'{prefix}:follow:{side}' for row in panel['components'] for b in row['components'])
        form=ui.modal('follow',side)['data']
        fields={x['components'][0]['custom_id']:x['components'][0] for x in form['components']}
        assert not fields['offset']['required'] and fields['target']['required']


@pytest.mark.parametrize('ui,prefix',[(okx_ui,'price'),(mexc_ui,'mprice')])
def test_form_executes_without_exchange_call(db,ui,prefix):
    payload={'type':5,'data':{'custom_id':f'{prefix}:form:follow:BUY','components':[
        {'components':[{'custom_id':k,'value':v}]} for k,v in [('slot','1'),('target','1phut30s'),('offset','')]]}}
    result=asyncio.run(ui.execute_panel(payload,db,'owner'))
    assert '+0đ' in result['content']


def test_okx_get_uses_target(db):
    data=board('BUY'); client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=data)))
    async def run():
        async with client:
            return await read_rank_plan(client,'BUY',target_nickname='1phut30s',price_offset=-1)
    assert asyncio.run(run())['proposed_price']=='26089'


def test_mexc_get_uses_own_exchange_and_target():
    data={**board('BUY'),'exchange':'mexc','panel':'mexc_buy','side':'BUY','cache':'new'}
    urls=[]
    def get(r):urls.append(str(r.url));return httpx.Response(200,json=data)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(get)) as client:
            return await rank_plan(client,SimpleNamespace(key='BUY:1',nickname='VNTrader',rank=10),None,('1phut30s',-1,'revision'))
    assert asyncio.run(run())['proposed_price']=='26089'
    assert 'mexc_buy' in urls[0]

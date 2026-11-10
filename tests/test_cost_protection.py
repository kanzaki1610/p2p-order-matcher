import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app import cost_protection as switch,web_control as web
from app.models import OKXManualCost,OKXSlot
from app.mexc_pricing import MexcPriceSettings
from app.okx_cost import read_pricing_cost,cost_revision,save_cost
from app.okx_rank import plan_rank_price
from app.mexc_manual_price import validate_floor
from test_web_control import client,send
from test_okx_rank import market


@pytest.mark.parametrize('exchange',['OKX','MEXC'])
def test_cost_switch_defaults_on_and_is_independent_by_exchange(client,exchange):
    assert client.get('/dashboard/api/control/state').json()['exchanges'][exchange]['cost_enabled']
    result=send(client,'cost_switch',exchange=exchange,enabled=False,acknowledge=True)
    assert result.status_code==200 and 'Đã tắt' in result.json()['message']
    state=client.get('/dashboard/api/control/state').json()
    assert not state['exchanges'][exchange]['cost_enabled']
    assert state['exchanges']['MEXC' if exchange=='OKX' else 'OKX']['cost_enabled']
    assert state['exchanges'][exchange]['cost']=='25000'
    assert send(client,'cost_switch',exchange=exchange,enabled=True,acknowledge=True).status_code==200


def test_switch_needs_explicit_ack_and_primary_account(client,monkeypatch):
    assert send(client,'cost_switch',enabled=False).status_code==400
    monkeypatch.setattr(web.settings,'database_schema','kayhap')
    assert send(client,'cost_switch',enabled=False,acknowledge=True).status_code==404


def test_disabling_retains_cost_and_bumps_existing_write_fence(client):
    with client.factory() as db:old=cost_revision(db)
    assert send(client,'cost_switch',enabled=False,acknowledge=True).status_code==200
    with client.factory() as db:
        assert cost_revision(db)!=old
        value=asyncio.run(read_pricing_cost(db,None))
        assert value['mode']=='DISABLED' and value['unit_price'] is None
        assert db.get(OKXManualCost,1).unit_price==25000
        save_cost(db,Decimal(26050),'web:primary')
        assert not switch.enforced(db,'OKX')
        assert asyncio.run(read_pricing_cost(db,None))['mode']=='DISABLED'


def test_no_cost_can_be_disabled_but_not_enabled_until_saved(client):
    with client.factory() as db:
        db.get(MexcPriceSettings,1).cost=None;db.commit()
    assert send(client,'cost_switch',exchange='MEXC',enabled=False,acknowledge=True).status_code==200
    assert send(client,'cost_switch',exchange='MEXC',enabled=True,acknowledge=True).status_code==400


def test_disabling_floor_does_not_disable_other_source_guards_or_nickname_bound():
    data=market('SELL',24000)
    assert plan_rank_price(data,'SELL',26000,now=1002)['action']=='HIDE_AD'
    result=plan_rank_price(data,'SELL',None,now=1002,enforce_cost=False)
    assert result['action']=='UPDATE_PRICE' and result.get('minimum_sell_price') is None
    stale=dict(data,saved_ts=0)
    assert plan_rank_price(stale,'SELL',None,now=1002,enforce_cost=False)['action']=='BLOCKED'
    bounded=plan_rank_price(data,'SELL',None,now=1002,enforce_cost=False,
        target_nickname='TEST-9',price_offset=-1,price_bound=25000)
    assert bounded['proposed_price']=='25000'


def test_mexc_manual_floor_uses_switch_and_retains_revision_fence(client):
    with client.factory() as db:
        with pytest.raises(ValueError):validate_floor(db,'SELL',Decimal(24000))
        old=db.get(MexcPriceSettings,1).revision
        switch.set_enabled(db,'MEXC',False,'web:primary')
        assert validate_floor(db,'SELL',Decimal(24000))!=old
        switch.set_enabled(db,'MEXC',True,'web:primary')
        with pytest.raises(ValueError):validate_floor(db,'SELL',Decimal(24000))


def test_secondary_package_ignores_primary_off_switch(client,monkeypatch):
    with client.factory() as db:
        switch.set_enabled(db,'OKX',False,'web:primary')
        monkeypatch.setattr(switch.primary_controls,'__package__','app_kayhap')
        assert switch.enforced(db,'OKX')

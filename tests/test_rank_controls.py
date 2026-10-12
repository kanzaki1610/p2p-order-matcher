import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from nacl.signing import SigningKey
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app import okx_price_controls as controls, discord_commands as discord
from app.config import settings
from app.database import Base
from app.dashboard import RankPreferencesUpdate, update_rank_preferences
from app.main import app
from app.okx_rank import plan_rank_price
from app.models import OKXPricePreferences


@pytest.fixture
def db():
    engine=create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as session: yield session
    engine.dispose()


def market():
    return dict(ok=True,panel='okx_buy',exchange='okx',side='buy',saved_ts=1000,age_sec=0,cache='fresh',error=None,
        rows=[{'nick':'VNTrader','price':27000}]+[{'nick':str(i),'price':26000+i} for i in range(1,21)])


@pytest.mark.parametrize('rank',[1,6,10,20])
def test_rank_selects_requested_ad_after_own_exclusion(rank):
    result=plan_rank_price(market(),'BUY',target_rank=rank,now=1000)
    assert result['proposed_price']==str(26000+rank) and result['target_rank']==rank


@pytest.mark.parametrize('rank',[0,51,1.5,True,'10',None])
def test_invalid_rank_rejected(rank):
    with pytest.raises(ValueError): plan_rank_price(market(),'BUY',target_rank=rank,now=1000)


def test_not_enough_ads_never_uses_different_rank():
    assert plan_rank_price(market(),'BUY',target_rank=21,now=1000)['action']=='BLOCKED'


def test_sell_floor_kept_when_custom_rank_is_selected():
    source=market();source.update(panel='okx_sell',side='sell')
    assert plan_rank_price(source,'SELL',cost_basis=26050,target_rank=6,now=1000)['action']=='HIDE_AD'


def test_preferences_persist_partial_updates_and_api_validation(db,monkeypatch):
    monkeypatch.setattr(settings,'database_schema','public')
    assert controls.preferences_dict(db)=={'buy_rank':10,'sell_rank':10}
    controls.save_ranks(db,{'buy_rank':6},'discord:owner')
    controls.save_ranks(db,{'sell_rank':15})
    assert controls.preferences_dict(db)=={'buy_rank':6,'sell_rank':15}
    result=update_rank_preferences(RankPreferencesUpdate(buy_rank=8),db,None)
    assert result['buy_rank']==8 and result['sell_rank']==15
    with pytest.raises(ValueError): RankPreferencesUpdate(buy_rank=6.1)
    monkeypatch.setattr(settings,'database_schema','kayhap')
    with pytest.raises(HTTPException): update_rank_preferences(RankPreferencesUpdate(buy_rank=1),db,None)


def price_data(action='vitri',side='BUY',rank=6):
    options=[{'name':'chieu','value':side}]
    if action=='vitri':options.append({'name':'vitri','value':rank})
    return {'name':'gia','options':[{'name':action,'type':1,'options':options if action!='trangthai' else []}]}


def test_discord_save_then_preview_uses_same_rank_without_financial_write(db,monkeypatch):
    monkeypatch.setattr(settings,'database_schema','public')
    monkeypatch.setattr(settings,'okx_ad_pricing_enabled',False)
    source=AsyncMock(return_value={'action':'UPDATE_PRICE','proposed_price':'26006'})
    monkeypatch.setattr(controls,'read_rank_plan',source)
    reply=asyncio.run(controls.execute_price_command(price_data(),db,'owner'))
    assert 'ĐANG TẮT' in reply and '26006' in reply
    assert source.call_args.kwargs['target_rank']==6
    assert controls.preferences_dict(db)['buy_rank']==6


def test_discord_invalid_rank_does_not_save(db,monkeypatch):
    monkeypatch.setattr(settings,'database_schema','public')
    with pytest.raises(ValueError): asyncio.run(controls.execute_price_command(price_data(rank=0),db,'owner'))
    assert controls.preferences_dict(db)['buy_rank']==10


def test_signed_pricing_only_owner_channel_replay_and_other_commands_disabled(monkeypatch,tmp_path):
    engine=create_engine(f"sqlite:///{tmp_path/'price-discord.db'}",connect_args={'check_same_thread':False})
    Base.metadata.create_all(engine)
    sessions=sessionmaker(engine,expire_on_commit=False)
    monkeypatch.setattr(discord,'SessionLocal',sessions)
    key=SigningKey.generate()
    for field,value in dict(discord_commands_enabled=False,discord_pricing_commands_enabled=True,
        discord_public_key=key.verify_key.encode().hex(),discord_application_id='app',discord_guild_id='guild',
        discord_command_channel_ids='channel',discord_allowed_user_ids='owner',discord_allowed_role_ids='',database_schema='public').items():
        monkeypatch.setattr(settings,field,value)
    runner=AsyncMock();monkeypatch.setattr(discord,'run_command',runner)
    client=TestClient(app)
    def post(payload):
        raw=json.dumps(payload).encode();stamp=str(int(time.time()))
        return client.post('/webhooks/discord',content=raw,headers={'X-Signature-Timestamp':stamp,'X-Signature-Ed25519':key.sign(stamp.encode()+raw).signature.hex()})
    payload=dict(type=2,id='101',token='fake',application_id='app',guild_id='guild',channel_id='channel',
        member={'user':{'id':'owner'},'roles':[]},data=price_data('trangthai'))
    assert post(dict(payload,member={'user':{'id':'other'},'roles':[]})).json()['type']==4
    assert post(dict(payload,channel_id='other')).json()['type']==4
    assert post(dict(payload,data={'name':'confirm','options':[]})).json()['type']==4
    assert post(payload).json()['type']==5
    assert post(payload).json()['type']==5
    assert runner.await_count==1
    monkeypatch.setattr(settings,'database_schema','kayhap')
    assert post(dict(payload,id='102')).json()['type']==4
    engine.dispose()

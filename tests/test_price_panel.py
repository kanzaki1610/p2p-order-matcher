import asyncio
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.database import Base
from app.models import OKXSlot
from app import discord_price_panel as ui

@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(ui.settings,'okx_ad_pricing_enabled',True)
    engine=create_engine('sqlite://');Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(OKXSlot(side='BUY',slot_number=1,adv_id='123',auto_enabled=True));session.commit()
        yield session
    engine.dispose()

def ad(code='123',side='buy',hidden=True,price='26050'):
    return dict(adId=code,isOwner=True,side=side,type='limit',status='new',isHidden=hidden,unitPrice=price)

def test_preview_does_not_write_or_disable_auto(db,monkeypatch):
    monkeypatch.setattr(ui,'read_ad',AsyncMock(return_value=ad()))
    request=AsyncMock();monkeypatch.setattr(ui,'request',request)
    result=asyncio.run(ui.manual_preview(db,'owner','BUY',1,Decimal(26060)))
    assert 'Xác nhận' in result['content']
    request.assert_not_called()
    assert db.query(OKXSlot).one().auto_enabled

def test_confirm_preserves_hidden_disables_auto_and_deduplicates(db,monkeypatch):
    monkeypatch.setattr(ui,'read_ad',AsyncMock(return_value=ad()))
    asyncio.run(ui.manual_preview(db,'owner','BUY',1,Decimal(26060)))
    draft=db.query(ui.ManualPriceDraft).one()
    request=AsyncMock(return_value={'oldAdId':'123','newAdId':'456'})
    monkeypatch.setattr(ui,'request',request)
    monkeypatch.setattr(ui,'confirm',AsyncMock(return_value=True))
    asyncio.run(ui.manual_confirm(db,'owner',draft.id))
    assert request.call_args.args[3]['online']=='false'
    assert not db.query(OKXSlot).one().auto_enabled
    with pytest.raises(ValueError):asyncio.run(ui.manual_confirm(db,'owner',draft.id))
    assert request.await_count==1

@pytest.mark.parametrize('case',['owner','expiry','unknown','changed'])
def test_invalid_confirmation_never_writes(db,monkeypatch,case):
    monkeypatch.setattr(ui,'read_ad',AsyncMock(return_value=ad()))
    asyncio.run(ui.manual_preview(db,'owner','BUY',1,Decimal(26060)))
    draft=db.query(ui.ManualPriceDraft).one();owner='owner'
    if case=='owner':owner='other'
    if case=='expiry':draft.expires=datetime.now(timezone.utc)-timedelta(minutes=1)
    if case=='unknown':db.add(ui.OKXAdPriceAttempt(slot_id=draft.slot_id,state='UNKNOWN'))
    if case=='changed':db.query(OKXSlot).one().adv_id='789'
    db.commit();request=AsyncMock();monkeypatch.setattr(ui,'request',request)
    with pytest.raises(ValueError):asyncio.run(ui.manual_confirm(db,owner,draft.id))
    request.assert_not_called()

def test_sell_floor_blocks_preview(db,monkeypatch):
    db.query(OKXSlot).one().side='SELL';db.commit()
    monkeypatch.setattr(ui,'read_ad',AsyncMock(return_value=ad(side='sell')))
    monkeypatch.setattr(ui,'read_last_buy_cost',AsyncMock(return_value={'unit_price':'26050'}))
    with pytest.raises(ValueError):asyncio.run(ui.manual_preview(db,'owner','SELL',1,Decimal(26059)))
    assert db.query(ui.ManualPriceDraft).count()==0

def test_panel_and_modals_fit_discord_limits():
    for row in ui.panel()['components']:
        assert len(row['components'])<=5
        assert all(len(x['custom_id'])<=100 for x in row['components'])
    assert ui.modal('price','BUY')['type']==9

def test_timeout_is_not_retried(db,monkeypatch):
    monkeypatch.setattr(ui,'read_ad',AsyncMock(return_value=ad()))
    asyncio.run(ui.manual_preview(db,'owner','BUY',1,Decimal(26060)))
    draft=db.query(ui.ManualPriceDraft).one()
    request=AsyncMock(side_effect=TimeoutError());monkeypatch.setattr(ui,'request',request)
    assert 'Không tự gửi lại' in asyncio.run(ui.manual_confirm(db,'owner',draft.id))
    assert db.query(ui.OKXAdPriceAttempt).one().state=='UNKNOWN'
    with pytest.raises(ValueError):asyncio.run(ui.manual_confirm(db,'owner',draft.id))
    assert request.await_count==1

@pytest.mark.parametrize('allowed',[True,False])
def test_signed_buttons_require_owner_and_pricing_flag(monkeypatch,allowed):
    import json,time
    from nacl.signing import SigningKey
    from fastapi.testclient import TestClient
    from app.main import app
    key=SigningKey.generate()
    for field,value in dict(discord_public_key=key.verify_key.encode().hex(),discord_application_id='app',discord_guild_id='guild',discord_command_channel_ids='channel',discord_allowed_user_ids='owner',discord_allowed_role_ids='',discord_pricing_commands_enabled=True,discord_commands_enabled=False,database_schema='public').items():monkeypatch.setattr(ui.settings,field,value)
    payload={'type':3,'id':'1','token':'fake','application_id':'app','guild_id':'guild','channel_id':'channel','member':{'user':{'id':'owner' if allowed else 'other'},'roles':[]},'data':{'custom_id':'price:price:BUY'}}
    raw=json.dumps(payload).encode();stamp=str(int(time.time()))
    response=TestClient(app).post('/webhooks/discord',content=raw,headers={'X-Signature-Timestamp':stamp,'X-Signature-Ed25519':key.sign(stamp.encode()+raw).signature.hex()})
    assert response.json()['type']==(9 if allowed else 4)

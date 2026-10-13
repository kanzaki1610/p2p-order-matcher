import asyncio
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.database import Base
from app.models import OKXSlot, OKXPricingSwitch
from app import okx_price_controls as controls, okx_ad_worker as worker

@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(controls.settings,'database_schema','public')
    monkeypatch.setattr(controls.settings,'okx_ad_pricing_enabled',True)
    engine=create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(OKXSlot(side='BUY',slot_number=1,adv_id='123',auto_enabled=True))
        session.commit()
        yield session
    engine.dispose()

def command(name,**options):
    return {'options':[{'name':name,'options':[{'name':k,'value':v} for k,v in options.items()]}]}

@pytest.mark.parametrize('action',['danhsach','auto','quangcao','bamtat'])
def test_new_commands_pass_interaction_parser(action):
    from app.discord_commands import command_text
    data=command(action,chieu='BUY',slot=1,id='123',bat=False)
    data['name']='gia'
    assert command_text(data)=='/gia'

def test_pause_blocks_worker_and_resume_keeps_slots(db,monkeypatch):
    request=AsyncMock()
    monkeypatch.setattr(worker,'request',request)
    asyncio.run(controls.execute_price_command(command('bamtat',bat=False),db,'owner'))
    assert not controls.pricing_active(db)
    asyncio.run(worker.process_ad_pricing(db,None))
    request.assert_not_called()
    assert db.query(OKXSlot).one().auto_enabled
    asyncio.run(controls.execute_price_command(command('bamtat',bat=True),db,'owner'))
    assert controls.pricing_active(db)

def test_select_owned_id_turns_auto_off_and_never_posts(db,monkeypatch):
    read=AsyncMock(return_value={'side':'buy','status':'new','type':'limit','unitPrice':'26100'})
    monkeypatch.setattr(controls,'read_ad',read)
    asyncio.run(controls.execute_price_command(command('quangcao',chieu='BUY',slot=1,id='456'),db,'owner'))
    db.expire_all(); slot=db.query(OKXSlot).one()
    assert slot.adv_id=='456' and not slot.auto_enabled
    asyncio.run(controls.execute_price_command(command('auto',chieu='BUY',slot=1,bat=True),db,'owner'))
    db.expire_all(); assert db.query(OKXSlot).one().auto_enabled

def test_pending_attempt_cannot_be_reset_or_enabled(db):
    slot=db.query(OKXSlot).one()
    db.add(worker.OKXAdPriceAttempt(slot_id=slot.id,state='UNKNOWN')); db.commit()
    for cmd in [command('quangcao',chieu='BUY',slot=1,id='456'),command('auto',chieu='BUY',slot=1,bat=True)]:
        assert 'Chưa thay đổi' in asyncio.run(controls.execute_price_command(cmd,db,'owner'))
    asyncio.run(controls.execute_price_command(command('auto',chieu='BUY',slot=1,bat=False),db,'owner'))
    db.expire_all(); assert not db.query(OKXSlot).one().auto_enabled

def test_friend_cannot_modify_original(db,monkeypatch):
    monkeypatch.setattr(controls.settings,'database_schema','kayhap')
    assert 'tài khoản gốc' in asyncio.run(controls.execute_price_command(command('bamtat',bat=False),db,'friend'))
    assert db.get(OKXPricingSwitch,1) is None

@pytest.mark.parametrize('old_closed',[True,False])
def test_rebind_stale_ad_only_after_verified_closed_old_and_auto_off(db,monkeypatch,old_closed):
    from datetime import datetime,timezone,timedelta
    from app.okx_release import OKXRequestError
    slot=db.query(OKXSlot).one();slot.auto_enabled=False
    db.add(worker.OKXAdPriceAttempt(slot_id=slot.id,state='UNKNOWN',old_ad_id='123',last_action_at=datetime.now(timezone.utc)-timedelta(minutes=5),reason='old unknown'))
    db.commit()
    new={'side':'buy','status':'new','type':'limit','unitPrice':'26100'}
    old_error=OKXRequestError('OKX HTTP 400; mã API: 55651.' if old_closed else 'OKX HTTP 400; mã API: 55349.')
    read=AsyncMock(side_effect=[new,old_error]);monkeypatch.setattr(controls,'read_ad',read)
    reply=asyncio.run(controls.execute_price_command(command('quangcao',chieu='BUY',slot=1,id='456'),db,'owner'))
    db.expire_all()
    if old_closed:
        assert db.query(OKXSlot).one().adv_id=='456'
        assert db.query(worker.OKXAdPriceAttempt).one().state=='IDLE'
        assert not db.query(OKXSlot).one().auto_enabled
    else:
        assert 'Chưa thay đổi' in reply
        assert db.query(worker.OKXAdPriceAttempt).one().state=='UNKNOWN'
        assert db.query(OKXSlot).one().adv_id=='123'

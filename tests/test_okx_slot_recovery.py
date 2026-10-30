import asyncio
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session
from app.database import Base
from app.models import OKXSlot, OKXAuditLog
from app import okx_slot_recovery as recovery
from app import discord_price_panel as ui


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(recovery.settings, 'database_schema', 'public')
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        slot=OKXSlot(side='SELL',slot_number=2,adv_id='123',current_price=27563,auto_enabled=True)
        db.add(slot);db.flush()
        db.add(recovery.OKXAdPriceAttempt(slot_id=slot.id,state='UNKNOWN',action='UPDATE_PRICE',old_ad_id='123',
            price=26350,hidden=False,last_action_at=datetime.now(timezone.utc)-timedelta(minutes=5),
            reason='OKX HTTP 400; mã API: 55723.'))
        db.add(recovery.OKXAutoLimits(slot_id=slot.id,ad_id='123',minimum=20000000,maximum=180000000))
        db.commit()
        yield db
    engine.dispose()


def ad(code='123',price='27563'):
    return dict(adId=code,side='sell',type='limit',status='new',isHidden=False,
        unitPrice=price,minOrderSize='20000000',maxOrderSize='180000000')


def run(db): return asyncio.run(recovery.recover_slot(db,'owner','SELL',2))


def test_rejected_unchanged_ad_recovers_same_id_keeps_limits_and_auto_off(db,monkeypatch):
    reader=AsyncMock(return_value=ad());monkeypatch.setattr(recovery,'read_ad',reader)
    # Every network call must be an authenticated ad GET through read_ad.
    post=AsyncMock();monkeypatch.setattr(recovery.httpx.AsyncClient,'post',post)
    result=run(db)
    slot=db.query(OKXSlot).one();attempt=db.query(recovery.OKXAdPriceAttempt).one()
    assert attempt.state=='IDLE' and not slot.auto_enabled and slot.adv_id=='123'
    assert db.query(recovery.OKXAutoLimits).one().maximum==180000000
    assert attempt.last_action_at is not None
    assert 'gỡ khóa' in result and 'Auto vẫn TẮT' in result
    assert db.query(OKXAuditLog).one().actor=='discord:owner'
    assert '55723' in db.query(OKXAuditLog).one().detail
    reader.assert_awaited_once();post.assert_not_awaited()
    assert 'không có yêu cầu' in run(db)


@pytest.mark.parametrize('case',['timeout','429','other400','same_target','changed_price','hidden','wrong_side','closed','recent','submitting','no_date','manual','different_slot_id','missing_result'])
def test_unproven_attempt_stays_locked(db,monkeypatch,case):
    attempt=db.query(recovery.OKXAdPriceAttempt).one();row=ad()
    if case=='timeout':attempt.reason='Hết thời gian chờ OKX; chưa xác định yêu cầu đã được xử lý hay chưa.'
    if case=='429':attempt.reason='OKX HTTP 429; mã API: 50011.'
    if case=='other400':attempt.reason='OKX HTTP 400; mã API: 50000.'
    if case=='same_target':row['unitPrice']='26350'
    if case=='changed_price':row['unitPrice']='27560'
    if case=='hidden':row['isHidden']=True
    if case=='wrong_side':row['side']='buy'
    if case=='closed':row['status']='cancelled'
    if case=='recent':attempt.last_action_at=datetime.now(timezone.utc)
    if case=='submitting':attempt.state='SUBMITTING'
    if case=='no_date':attempt.last_action_at=None
    if case=='manual':attempt.action='MANUAL_PRICE'
    if case=='different_slot_id':attempt.old_ad_id='987'
    if case=='missing_result':attempt.reason=None
    db.commit();reader=AsyncMock(return_value=row);monkeypatch.setattr(recovery,'read_ad',reader)
    result=run(db)
    assert 'Chưa khôi phục' in result
    assert attempt.state!='IDLE' and not db.query(OKXSlot).one().auto_enabled
    assert db.query(OKXAuditLog).count()==0


@pytest.mark.parametrize('bad_limits',[False,True])
def test_known_result_verifies_exact_price_and_limits(db,monkeypatch,bad_limits):
    attempt=db.query(recovery.OKXAdPriceAttempt).one();attempt.result_ad_id='456';attempt.state='CONFIRMING'
    db.add(recovery.OKXPendingLimits(slot_id=attempt.slot_id,ad_id='123',minimum=20000000,maximum=180000000));db.commit()
    row=ad('456','26350')
    if bad_limits:row['maxOrderSize']='190000000'
    monkeypatch.setattr(recovery,'read_ad',AsyncMock(return_value=row))
    result=run(db);slot=db.query(OKXSlot).one()
    if bad_limits:
        assert attempt.state=='CONFIRMING' and slot.adv_id=='123'
    else:
        assert attempt.state=='IDLE' and slot.adv_id=='456'
        assert db.query(recovery.OKXAutoLimits).one().ad_id=='456'
        assert db.query(recovery.OKXPendingLimits).count()==0
    assert not slot.auto_enabled


def test_concurrent_attempt_change_is_not_overwritten(db,monkeypatch):
    async def change(*args):
        db.execute(update(recovery.OKXAdPriceAttempt).values(reason='New diagnostic').execution_options(synchronize_session=False));db.commit()
        return ad()
    monkeypatch.setattr(recovery,'read_ad',change)
    assert 'đã thay đổi' in run(db)
    assert db.query(recovery.OKXAdPriceAttempt).one().reason=='New diagnostic'
    assert db.query(recovery.OKXAdPriceAttempt).one().state=='UNKNOWN'


def test_recovery_button_selects_slot_without_exchange_call(db,monkeypatch):
    handler=AsyncMock(return_value='checked');monkeypatch.setattr(ui,'recover_slot',handler)
    response=asyncio.run(ui.execute_panel({'data':{'custom_id':'price:recover:SELL'}},db,'owner'))
    assert len(response['components'][0]['components'])==2
    handler.assert_not_called()
    response=asyncio.run(ui.execute_panel({'data':{'custom_id':'price:checkslot:SELL:2'}},db,'owner'))
    assert response['content']=='checked'
    handler.assert_awaited_once_with(db,'owner','SELL',2)


def test_secondary_account_cannot_recover_primary(db,monkeypatch):
    monkeypatch.setattr(recovery.settings,'database_schema','kayhap')
    reader=AsyncMock();monkeypatch.setattr(recovery,'read_ad',reader)
    with pytest.raises(ValueError):run(db)
    reader.assert_not_called()

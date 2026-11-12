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



def manual_case(db):
    import json
    from app.discord_price_panel import ManualPriceDraft,ManualAdTerms
    slot=db.query(OKXSlot).one();attempt=db.query(recovery.OKXAdPriceAttempt).one()
    attempt.action='MANUAL_PRICE'
    draft=ManualPriceDraft(id='a'*32,owner='web:primary',slot_id=slot.id,ad_id=slot.adv_id,
        price=attempt.price,hidden=attempt.hidden,used=True,
        expires=attempt.last_action_at+timedelta(minutes=2))
    db.add(draft);db.flush()
    data=ui.ad_snapshot({**ad(),'cryptoAmount':'10000'})
    db.add(ManualAdTerms(draft_id=draft.id,snapshot_json=json.dumps(data),
        terms_json=json.dumps({'cryptoAmount':'10000','minOrderLimit':'20000000','maxOrderLimit':'180000000'})))
    db.commit()
    return draft


@pytest.mark.parametrize('side',['BUY','SELL'])
def test_manual_55723_rejection_recovers_full_snapshot_preserves_manual_hide(db,monkeypatch,side):
    import json
    draft=manual_case(db)
    slot=db.query(OKXSlot).one();slot.side=side;db.commit()
    row={**ad(),'side':side.lower(),'cryptoAmount':'10000','isHidden':True}
    reader=AsyncMock(return_value=row);monkeypatch.setattr(recovery,'read_ad',reader)
    post=AsyncMock();monkeypatch.setattr(recovery.httpx.AsyncClient,'post',post)
    result=asyncio.run(recovery.recover_slot(db,'owner',side,2))
    assert 'gỡ khóa' in result and 'vẫn đang ẩn' in result
    assert db.query(recovery.OKXAdPriceAttempt).one().state=='IDLE'
    assert not slot.auto_enabled and slot.adv_id=='123'
    assert row['isHidden'] is True
    record=json.loads(db.query(OKXAuditLog).one().detail)
    assert record['outcome']=='MANUAL_REJECTED_UNCHANGED' and record['manual_draft_id']==draft.id
    assert db.query(recovery.OKXAutoLimits).one().maximum==180000000
    reader.assert_awaited_once();post.assert_not_awaited()


@pytest.mark.parametrize('case',['duplicate','unused','wrong_time','wrong_id','wrong_price','missing_snapshot',
    'changed_quantity','changed_min','changed_max','target_price','unknown_terms','timeout','429','new_id','pending_limits'])
def test_manual_recovery_requires_unique_complete_unchanged_evidence(db,monkeypatch,case):
    import json
    draft=manual_case(db);attempt=db.query(recovery.OKXAdPriceAttempt).one()
    terms=db.get(ui.ManualAdTerms,draft.id)
    row={**ad(),'cryptoAmount':'10000'}
    if case=='duplicate':
        db.add(ui.ManualPriceDraft(id='b'*32,owner=draft.owner,slot_id=draft.slot_id,ad_id=draft.ad_id,
            price=draft.price,hidden=draft.hidden,used=True,expires=draft.expires))
        db.add(ui.ManualAdTerms(draft_id='b'*32,snapshot_json=terms.snapshot_json,terms_json=terms.terms_json))
    if case=='unused':draft.used=False
    if case=='wrong_time':draft.expires=attempt.last_action_at-timedelta(seconds=1)
    if case=='wrong_id':draft.ad_id='987'
    if case=='wrong_price':draft.price=123
    if case=='missing_snapshot':terms.snapshot_json=json.dumps({'unitPrice':'27563'})
    if case=='changed_quantity':row['cryptoAmount']='9999'
    if case=='changed_min':row['minOrderSize']='10000000'
    if case=='changed_max':row['maxOrderSize']='190000000'
    if case=='target_price':row['unitPrice']=str(attempt.price)
    if case=='unknown_terms':terms.terms_json=json.dumps({'online':'true'})
    if case=='timeout':attempt.reason='Hết thời gian chờ OKX'
    if case=='429':attempt.reason='OKX HTTP 429; mã API: 50011.'
    if case=='new_id':attempt.result_ad_id='456'
    if case=='pending_limits':db.add(recovery.OKXPendingLimits(slot_id=attempt.slot_id,ad_id='123',minimum=1,maximum=2))
    db.commit();monkeypatch.setattr(recovery,'read_ad',AsyncMock(return_value=row))
    result=run(db)
    assert 'Chưa khôi phục' in result
    assert attempt.state=='UNKNOWN' and not db.query(OKXSlot).one().auto_enabled
    assert db.query(OKXAuditLog).count()==0


def test_manual_draft_change_during_read_is_not_overwritten(db,monkeypatch):
    draft=manual_case(db)
    async def change(*args):
        db.execute(update(ui.ManualAdTerms).where(ui.ManualAdTerms.draft_id==draft.id).values(terms_json='{}'))
        db.commit()
        return {**ad(),'cryptoAmount':'10000'}
    monkeypatch.setattr(recovery,'read_ad',change)
    assert 'đã thay đổi' in run(db)
    assert db.query(recovery.OKXAdPriceAttempt).one().state=='UNKNOWN'

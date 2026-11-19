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


def replacement_case(db, side='SELL'):
    slot=db.query(OKXSlot).one();slot.side=side
    attempt=db.query(recovery.OKXAdPriceAttempt).one()
    attempt.reason='OKX HTTP 504; phản hồi không phải JSON.'
    terms={'minOrderLimit':'20000000','maxOrderLimit':'26349999','cryptoAmount':'1000'}
    db.add(recovery.OKXPendingLimits(slot_id=slot.id,ad_id='123',minimum=20000000,maximum=26349999))
    if side=='BUY':
        b=recovery.buy_budget
        db.add(b.OKXBuyBudget(root_ad_id='123',slot_id=slot.id,total_vnd=26349999,total_crypto=1000));db.flush()
        db.add(b.OKXBuyBudgetAd(ad_id='123',root_ad_id='123'))
        db.add(b.OKXPendingBuyBudget(slot_id=slot.id,root_ad_id='123',old_ad_id='123',quantity=1000))
    else:db.add(recovery.OKXPendingSellQuantity(slot_id=slot.id,ad_id='123',quantity=1000))
    recovery.replacement.save(db,slot,attempt,terms,['123','777']);db.commit()
    candidate={**ad('456','26350'),'side':side.lower(),'isOwner':True,
        'cryptoCurrency':'USDT','fiatCurrency':'VND','maxOrderSize':'26349999',
        'availableAmount':'900','onHoldAmount':'100','completedOrderCryptoAmount':'0',
        'createdTimestamp':int((datetime.fromisoformat(recovery.replacement.stamp(attempt.last_action_at)).timestamp()+6)*1000)}
    return slot,attempt,candidate


@pytest.mark.parametrize('old_read_code',['55651','55349'])
@pytest.mark.parametrize('side',['BUY','SELL'])
def test_lost_504_response_finds_unique_new_id_preserves_budget_after_restart(db,monkeypatch,side,old_read_code):
    from app.okx_release import OKXRequestError
    slot,attempt,row=replacement_case(db,side)
    async def read(client,code):
        if code=='123':raise OKXRequestError('OKX HTTP 400; mã API: '+old_read_code+'.')
        return row
    monkeypatch.setattr(recovery.replacement,'read_ad',AsyncMock(side_effect=read))
    listing=AsyncMock(return_value=([{'adId':'456','side':side.lower()}],False))
    monkeypatch.setattr(recovery.replacement,'owned_list',listing)
    post=AsyncMock();monkeypatch.setattr(recovery.httpx.AsyncClient,'post',post)
    db.expire_all()  # Saved evidence, not an in-memory response, drives recovery.
    result=asyncio.run(recovery.recover_slot(db,'owner',side,2))
    assert slot.adv_id=='456' and attempt.state=='IDLE' and not slot.auto_enabled, result
    assert slot.current_price==26350 and 'sau lỗi 504' in result
    assert db.get(recovery.OKXPendingLimits,slot.id) is None
    assert db.query(recovery.OKXAutoLimits).one().ad_id=='456'
    if side=='BUY':
        b=recovery.buy_budget
        assert db.get(b.OKXBuyBudgetAd,'456').root_ad_id=='123'
        assert db.get(b.OKXBuyBudget,'123').total_vnd==26349999
        assert db.get(b.OKXPendingBuyBudget,slot.id) is None
    else:assert db.get(recovery.OKXPendingSellQuantity,slot.id) is None
    assert 'replacement_evidence' in db.query(OKXAuditLog).one().detail
    post.assert_not_awaited();assert listing.await_count==2


@pytest.mark.parametrize('old_read_code',['55651','55349'])
@pytest.mark.parametrize('case',[
    'none','multiple','preexisting','wrong_price','wrong_quantity','wrong_min','wrong_max',
    'hidden','wrong_side','wrong_owner','wrong_currency','early','late','missing_time',
    'old_present','old_readable','old_timeout','candidate_error','list_changed','read_changed',
    'other_slot','concurrent_attempt','concurrent_evidence','missing_evidence','wrong_evidence',
    'missing_limits','missing_budget','wrong_root','wrong_pending_quantity','old_429',
])
def test_lost_504_never_guesses_or_resubmits_when_evidence_is_incomplete(db,monkeypatch,case,old_read_code):
    import json
    from app.okx_release import OKXRequestError
    slot,attempt,row=replacement_case(db,'BUY')
    proof=db.get(recovery.replacement.OKXReplacementIntent,slot.id)
    if case=='preexisting':
        evidence=json.loads(proof.evidence);evidence['baseline'].append('456');proof.evidence=json.dumps(evidence)
    if case=='missing_evidence':db.delete(proof)
    if case=='missing_limits':db.delete(db.get(recovery.OKXPendingLimits,slot.id))
    if case=='missing_budget':db.delete(db.get(recovery.buy_budget.OKXPendingBuyBudget,slot.id))
    if case=='wrong_root':
        proof_data=json.loads(proof.evidence);proof_data['budget_root']='999';proof.evidence=json.dumps(proof_data)
    if case=='wrong_pending_quantity':db.get(recovery.buy_budget.OKXPendingBuyBudget,slot.id).quantity=999
    if case=='wrong_evidence':
        evidence=json.loads(proof.evidence);evidence['quantity']='1001';proof.evidence=json.dumps(evidence)
    if case=='wrong_price':row['unitPrice']='26349'
    if case=='wrong_quantity':row['availableAmount']='899'
    if case=='wrong_min':row['minOrderSize']='20000001'
    if case=='wrong_max':row['maxOrderSize']='26349998'
    if case=='hidden':row['isHidden']=True
    if case=='wrong_side':row['side']='sell'
    if case=='wrong_owner':row['isOwner']=False
    if case=='wrong_currency':row['fiatCurrency']='USD'
    if case=='early':row['createdTimestamp']-=60000
    if case=='late':row['createdTimestamp']+=180000
    if case=='missing_time':row.pop('createdTimestamp')
    if case=='other_slot':db.add(OKXSlot(side='BUY',slot_number=1,adv_id='456',auto_enabled=False))
    db.commit()
    reads=0
    async def read(client,code):
        nonlocal reads
        if code=='123':
            if case=='old_readable':return ad()
            if case=='old_timeout':raise TimeoutError()
            if case=='old_429':raise OKXRequestError('OKX HTTP 429; mã API: 50011.')
            raise OKXRequestError('OKX HTTP 400; mã API: '+old_read_code+'.')
        if case=='candidate_error':raise TimeoutError()
        reads+=1
        if reads==2:
            if case=='read_changed':return {**row,'unitPrice':'26349'}
            if case=='concurrent_attempt':
                db.execute(update(recovery.OKXAdPriceAttempt).values(reason='Changed'));db.commit()
            if case=='concurrent_evidence':
                db.execute(update(recovery.replacement.OKXReplacementIntent).values(revision='changed'));db.commit()
        return {**row,'adId':code}
    calls=0
    async def listing(*args):
        nonlocal calls
        calls+=1
        ids=[] if case=='none' else ['456','789'] if case=='multiple' else ['123','456'] if case=='old_present' else ['456']
        if calls==2 and case=='list_changed':ids.append('789')
        return ([{'adId':code,'side':'buy'} for code in ids],False)
    monkeypatch.setattr(recovery.replacement,'read_ad',AsyncMock(side_effect=read))
    monkeypatch.setattr(recovery.replacement,'owned_list',AsyncMock(side_effect=listing))
    post=AsyncMock();monkeypatch.setattr(recovery.httpx.AsyncClient,'post',post)
    result=asyncio.run(recovery.recover_slot(db,'owner','BUY',2))
    assert slot.adv_id=='123' and attempt.state=='UNKNOWN' and not slot.auto_enabled
    assert db.query(OKXAuditLog).count()==0
    if case!='missing_budget':assert db.get(recovery.buy_budget.OKXPendingBuyBudget,slot.id) is not None
    post.assert_not_awaited()


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


@pytest.mark.parametrize('side',['BUY','SELL'])
@pytest.mark.parametrize('hidden',[False,True])
def test_55147_same_owned_ad_at_previous_price_recovers_and_keeps_hidden_state(db,monkeypatch,side,hidden):
    slot=db.query(OKXSlot).one();slot.side=side
    attempt=db.query(recovery.OKXAdPriceAttempt).one()
    attempt.reason='OKX HTTP 400; mã API: 55147.'
    attempt.paused_by_floor=True
    # Current stock can have been consumed after the rejected request. Recovery
    # does not restore stock, show the ad or send the old request again.
    row={**ad(),'side':side.lower(),'isHidden':hidden,'availableAmount':'208.69',
         'completedOrderCryptoAmount':'10811.69','onHoldAmount':'0.00'}
    db.commit()
    monkeypatch.setattr(recovery,'read_ad',AsyncMock(return_value=row))
    post=AsyncMock();monkeypatch.setattr(recovery.httpx.AsyncClient,'post',post)
    result=asyncio.run(recovery.recover_slot(db,'owner',side,2))
    assert attempt.state=='IDLE' and not slot.auto_enabled and slot.adv_id=='123'
    assert not attempt.paused_by_floor
    assert '55147' in result and 'gỡ khóa' in result and 'Auto vẫn TẮT' in result
    assert ('vẫn đang ẩn' in result)==hidden
    assert row['isHidden'] is hidden
    assert '55147' in db.query(OKXAuditLog).one().detail
    post.assert_not_awaited()


@pytest.mark.parametrize('case',['target_price','changed_price','recent','timeout','wrong_side','missing_id','concurrent_budget','wrong_budget'])
def test_55147_requires_unchanged_price_and_matching_stable_pending_budget(db,monkeypatch,case):
    from app import okx_buy_budget as budget
    slot=db.query(OKXSlot).one();slot.side='BUY'
    attempt=db.query(recovery.OKXAdPriceAttempt).one();attempt.reason='OKX HTTP 400; mã API: 55147.'
    db.add(budget.OKXBuyBudget(root_ad_id='123',slot_id=slot.id,total_vnd=180000000,total_crypto=10000))
    db.flush()
    db.add(budget.OKXPendingBuyBudget(slot_id=slot.id,root_ad_id='123',old_ad_id='999' if case=='wrong_budget' else '123',quantity=10000))
    row={**ad(),'side':'buy','isHidden':True}
    if case=='target_price':row['unitPrice']='26350'
    if case=='changed_price':row['unitPrice']='27562'
    if case=='wrong_side':row['side']='sell'
    if case=='recent':attempt.last_action_at=datetime.now(timezone.utc)
    if case=='timeout':attempt.reason='OKX HTTP 504; phản hồi không phải JSON.'
    db.commit()
    async def read(*args):
        if case=='missing_id':
            from app.okx_release import OKXRequestError
            raise OKXRequestError('OKX HTTP 400; mã API: 55651.')
        if case=='concurrent_budget':
            db.execute(update(budget.OKXPendingBuyBudget).values(quantity=9999));db.commit()
        return row
    monkeypatch.setattr(recovery,'read_ad',AsyncMock(side_effect=read))
    post=AsyncMock();monkeypatch.setattr(recovery.httpx.AsyncClient,'post',post)
    result=asyncio.run(recovery.recover_slot(db,'owner','BUY',2))
    assert attempt.state=='UNKNOWN' and not slot.auto_enabled
    assert db.get(budget.OKXPendingBuyBudget,slot.id) is not None
    assert db.query(OKXAuditLog).count()==0
    post.assert_not_awaited()


def test_55147_recovers_then_operator_can_rebind_without_new_exchange_write(db,monkeypatch):
    from app import okx_price_controls as controls,okx_buy_budget as budget
    slot=db.query(OKXSlot).one();slot.side='BUY'
    attempt=db.query(recovery.OKXAdPriceAttempt).one();attempt.reason='OKX HTTP 400; mã API: 55147.'
    db.add(budget.OKXBuyBudget(root_ad_id='123',slot_id=slot.id,total_vnd=180000000,total_crypto=10000));db.flush()
    db.add(budget.OKXPendingBuyBudget(slot_id=slot.id,root_ad_id='123',old_ad_id='123',quantity=10000))
    db.add(recovery.OKXPendingLimits(slot_id=slot.id,ad_id='123',minimum=20000000,maximum=180000000));db.commit()
    monkeypatch.setattr(recovery,'read_ad',AsyncMock(return_value={**ad(),'side':'buy','isHidden':True}))
    asyncio.run(recovery.recover_slot(db,'owner','BUY',2))
    assert attempt.state=='IDLE' and db.get(budget.OKXPendingBuyBudget,slot.id) is None
    assert db.get(recovery.OKXPendingLimits,slot.id) is None
    assert db.get(budget.OKXBuyBudget,'123').total_vnd==180000000
    monkeypatch.setattr(controls,'read_ad',AsyncMock(return_value={**ad('456'),'side':'buy'}))
    post=AsyncMock();monkeypatch.setattr(recovery.httpx.AsyncClient,'post',post)
    result=asyncio.run(controls.manage_price_command({'options':[{'name':'quangcao','options':[
        {'name':'chieu','value':'BUY'},{'name':'slot','value':2},{'name':'id','value':'456'}]}]},db,'owner'))
    db.refresh(slot)
    assert 'Đã lưu' in result and slot.adv_id=='456' and not slot.auto_enabled
    post.assert_not_awaited()


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


@pytest.mark.parametrize('wrong_quantity',[False,True])
def test_recovery_checks_replacement_sell_quantity_including_new_reservations(db,monkeypatch,wrong_quantity):
    attempt=db.query(recovery.OKXAdPriceAttempt).one();attempt.result_ad_id='456';attempt.state='CONFIRMING'
    db.add(recovery.OKXPendingSellQuantity(slot_id=attempt.slot_id,ad_id='123',quantity=1000));db.commit()
    row=ad('456','26350');row.update(availableAmount='800' if wrong_quantity else '900',
                                  onHoldAmount='100',completedOrderCryptoAmount='0')
    monkeypatch.setattr(recovery,'read_ad',AsyncMock(return_value=row))
    run(db)
    if wrong_quantity:
        assert attempt.state=='CONFIRMING' and db.get(recovery.OKXPendingSellQuantity,attempt.slot_id)
    else:
        assert attempt.state=='IDLE' and db.get(recovery.OKXPendingSellQuantity,attempt.slot_id) is None
    assert not db.query(OKXSlot).one().auto_enabled


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


def test_manual_sell_preview_keeps_remaining_excludes_old_held_stock(db,monkeypatch):
    import json
    from app import manual_stock
    row=ad();row.update(availableAmount='1000',completedOrderCryptoAmount='1000',onHoldAmount='500')
    monkeypatch.setattr(ui.settings,'okx_ad_pricing_enabled',True)
    monkeypatch.setattr(ui,'read_ad',AsyncMock(return_value=row))
    monkeypatch.setattr(ui,'read_pricing_cost',AsyncMock(return_value={'mode':'DISABLED'}))
    monkeypatch.setattr(manual_stock,'wallet',AsyncMock(return_value={'available':'2000'}))
    attempt=db.query(recovery.OKXAdPriceAttempt).one();attempt.state='IDLE';db.commit()
    post=AsyncMock();monkeypatch.setattr(ui,'request',post)
    asyncio.run(ui.manual_preview(db,'owner','SELL',2,Decimal('27600'),{'maxOrderLimit':'27600000'}))
    terms=json.loads(db.query(ui.ManualAdTerms).one().terms_json)
    assert terms['cryptoAmount']=='1000'
    post.assert_not_awaited()


@pytest.mark.parametrize('case',['verified','present','list_error','timeout','rate_limit','result','other_read_error','concurrent'])
def test_retire_rejected_unavailable_ad_requires_both_proofs(db,monkeypatch,case):
    from app.okx_release import OKXRequestError
    from app import ad_visibility
    attempt=db.query(recovery.OKXAdPriceAttempt).one()
    if case=='timeout':attempt.reason='Timeout'
    if case=='rate_limit':attempt.reason='OKX HTTP 429; mã API: 50011.'
    if case=='result':attempt.result_ad_id='456'
    db.commit()
    reader=AsyncMock(side_effect=OKXRequestError('OKX HTTP 400; mã API: '+('55349.' if case=='other_read_error' else '55651.')))
    monkeypatch.setattr(recovery,'read_ad',reader)
    async def listing(*args):
        if case=='list_error':raise TimeoutError()
        if case=='concurrent':
            db.execute(update(recovery.OKXAdPriceAttempt).values(reason='changed'));db.commit()
        return ([{'adId':'123' if case=='present' else '789','side':'sell'}],False)
    listing_mock=AsyncMock(side_effect=listing)
    monkeypatch.setattr(ad_visibility,'owned_list',listing_mock)
    post=AsyncMock();monkeypatch.setattr(recovery.httpx.AsyncClient,'post',post)
    result=run(db)
    slot=db.query(OKXSlot).one()
    assert not slot.auto_enabled and slot.adv_id=='123' and slot.current_price==27563
    post.assert_not_awaited()
    if case=='verified':
        assert attempt.state=='IDLE' and '55651' in result
        assert 'REJECTED_AD_UNAVAILABLE' in db.query(OKXAuditLog).one().detail
    else:
        assert attempt.state!='IDLE' and db.query(OKXAuditLog).count()==0




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


@pytest.mark.parametrize('taken', ['held', 'completed', 'both', 'below_min', 'exhausted', 'hidden_below_min', 'hidden_exhausted'])
@pytest.mark.parametrize('old_read_code', ['55349', '55651'])
def test_lost_sell_504_recovers_after_exchange_caps_max_for_taken_stock(db, monkeypatch, taken, old_read_code):
    from app.okx_release import OKXRequestError
    slot, attempt, row = replacement_case(db, 'SELL')
    row.update(availableAmount='800', onHoldAmount='200' if taken == 'held' else '100' if taken == 'both' else '0',
               completedOrderCryptoAmount='200' if taken == 'completed' else '100' if taken == 'both' else '0',
               maxOrderSize='21080000')
    if 'below_min' in taken or 'exhausted' in taken:
        row.update(availableAmount='100' if 'below_min' in taken else '0',
                   onHoldAmount='900' if 'below_min' in taken else '0',
                   completedOrderCryptoAmount='1000' if 'exhausted' in taken else '0',
                   maxOrderSize='20000000')
        row['isHidden'] = taken.startswith('hidden_')
    async def read(client, code):
        if code == '123':
            raise OKXRequestError('OKX HTTP 400; mã API: ' + old_read_code + '.')
        return row
    monkeypatch.setattr(recovery.replacement, 'read_ad', AsyncMock(side_effect=read))
    monkeypatch.setattr(recovery.replacement, 'owned_list', AsyncMock(return_value=([{'adId': '456', 'side': 'sell'}], False)))
    post = AsyncMock(); monkeypatch.setattr(recovery.httpx.AsyncClient, 'post', post)
    result = run(db)
    assert slot.adv_id == '456' and attempt.state == 'IDLE' and not slot.auto_enabled, result
    assert slot.max_amount == Decimal(row['maxOrderSize']) and slot.min_amount == 20000000
    assert db.get(recovery.OKXPendingSellQuantity, slot.id) is None
    if taken.startswith('hidden_'): assert 'vẫn đang ẩn' in result
    if 'below_min' in taken or 'exhausted' in taken:
        assert 'Không bật Auto' in result and 'chưa đủ Min' in result
        assert 'kiểm tra rồi bấm Bật Auto' not in result
    post.assert_not_awaited()


@pytest.mark.parametrize('case', ['low_max', 'high_max', 'lost_stock', 'wrong_min', 'wrong_price',
    'missing_held', 'negative_held', 'nan_completed', 'wrong_side', 'second_read_changed'])
def test_dynamic_sell_max_never_relaxes_other_replacement_evidence(db, monkeypatch, case):
    from app.okx_release import OKXRequestError
    slot, attempt, row = replacement_case(db, 'SELL')
    row.update(availableAmount='800', onHoldAmount='200', completedOrderCryptoAmount='0', maxOrderSize='21080000')
    if case == 'low_max': row['maxOrderSize'] = '21079999'
    if case == 'high_max': row['maxOrderSize'] = '21080001'
    if case == 'lost_stock': row['onHoldAmount'] = '199'
    if case == 'wrong_min': row['minOrderSize'] = '20000001'
    if case == 'wrong_price': row['unitPrice'] = '26349'
    if case == 'missing_held': row.pop('onHoldAmount')
    if case == 'negative_held': row['onHoldAmount'] = '-200'
    if case == 'nan_completed': row['completedOrderCryptoAmount'] = 'NaN'
    if case == 'wrong_side': row['side'] = 'buy'
    reads = 0
    async def read(client, code):
        nonlocal reads
        if code == '123': raise OKXRequestError('OKX HTTP 400; mã API: 55349.')
        reads += 1
        return {**row, 'maxOrderSize': '21080001'} if case == 'second_read_changed' and reads == 2 else row
    monkeypatch.setattr(recovery.replacement, 'read_ad', AsyncMock(side_effect=read))
    monkeypatch.setattr(recovery.replacement, 'owned_list', AsyncMock(return_value=([{'adId': '456', 'side': 'sell'}], False)))
    post = AsyncMock(); monkeypatch.setattr(recovery.httpx.AsyncClient, 'post', post)
    run(db)
    assert slot.adv_id == '123' and attempt.state == 'UNKNOWN' and not slot.auto_enabled
    assert db.get(recovery.OKXPendingSellQuantity, slot.id) is not None
    post.assert_not_awaited()


@pytest.mark.parametrize('old_read_code', ['55349', '55651'])
@pytest.mark.parametrize('bad', [None, 'low_max', 'high_max', 'lost_stock', 'missing_root', 'wrong_root', 'wrong_price'])
def test_buy_504_effective_max_after_completed_purchase_preserves_budget(db, monkeypatch, old_read_code, bad):
    from app.okx_release import OKXRequestError
    b = recovery.buy_budget
    slot, attempt, row = replacement_case(db, 'BUY')
    # BUY Max is a VND budget, independent of ceil-rounded USDT quantity.
    pending = db.get(recovery.OKXPendingLimits, slot.id)
    pending.maximum = 26349865
    root = db.get(b.OKXBuyBudget, '123'); root.total_vnd = 26349865
    recovery.replacement.save(db, slot, attempt,
        {'minOrderLimit': '20000000', 'maxOrderLimit': '26349865', 'cryptoAmount': '1000'}, ['123', '777'])
    row.update(availableAmount='800', onHoldAmount='0', completedOrderCryptoAmount='200',
        completedOrderFiatAmount='5270000', maxOrderSize='21080000')
    if bad == 'low_max': row['maxOrderSize'] = '21079999'
    if bad == 'high_max': row['maxOrderSize'] = '21080001'
    if bad == 'lost_stock': row['completedOrderCryptoAmount'] = '199'
    if bad == 'missing_root': db.delete(root)
    if bad == 'wrong_root': db.get(b.OKXBuyBudgetAd, '123').root_ad_id = '987'
    if bad == 'wrong_price': row['unitPrice'] = '26349'
    db.commit(); db.expire_all()
    async def read(client, code):
        if code == '123': raise OKXRequestError('OKX HTTP 400; mã API: ' + old_read_code + '.')
        return row
    monkeypatch.setattr(recovery.replacement, 'read_ad', AsyncMock(side_effect=read))
    monkeypatch.setattr(recovery.replacement, 'owned_list', AsyncMock(return_value=([{'adId': '456', 'side': 'buy'}], False)))
    post = AsyncMock(); monkeypatch.setattr(recovery.httpx.AsyncClient, 'post', post)
    result = asyncio.run(recovery.recover_slot(db, 'owner', 'BUY', 2))
    assert not slot.auto_enabled
    if bad:
        assert slot.adv_id == '123' and attempt.state == 'UNKNOWN', result
        assert db.get(b.OKXPendingBuyBudget, slot.id) is not None
    else:
        assert slot.adv_id == '456' and attempt.state == 'IDLE', result
        assert slot.max_amount == 21080000
        assert db.get(b.OKXBuyBudgetAd, '456').root_ad_id == '123'
        assert db.get(b.OKXBuyBudget, '123').total_vnd == 26349865
        assert db.get(b.OKXPendingBuyBudget, slot.id) is None
    post.assert_not_awaited()

import asyncio
import json
import time
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app import mexc_price_adoption as adoption
from app import mexc_price_panel as panel
from app.models import OKXAuditLog
from app.mexc_price_state import MexcPendingPrice, record_pending
from test_mexc_pricing import ad, db, setup


@pytest.fixture
def scenario(db, setup, monkeypatch):
    slot, row = setup
    slot.state, slot.action, slot.attempt_id = 'UNKNOWN', 'PRICE', 'old-attempt'
    slot.last_action = Decimal(str(time.time() - 3600))
    slot.reason = 'Chưa rõ kết quả; danh sách cũ sai cấu trúc.'
    record_pending(db, slot, row, {**adoption.source_snapshot(row)['body'], 'advStatus': 'OPEN', 'price': '26070'})
    db.commit()
    # Manual changes to price, stock, limits and receiving account are allowed,
    # but only after the owner previews and explicitly adopts this new state.
    row.update(price='26110', quantity='200', availableQuantity='150', maxSingleTransAmount='3000000')
    row['paymentInfo'] = [{'id': 888, 'payMethod': 2}]
    read = AsyncMock(side_effect=lambda *args: dict(row))
    monkeypatch.setattr(adoption, 'read_ad', read)
    monkeypatch.setattr(adoption.asyncio, 'sleep', AsyncMock())
    return slot, row, read


def preview(db):
    result = asyncio.run(adoption.preview(db, 'owner', '1'))
    nonce = result['components'][0]['components'][0]['custom_id'].split(':')[-1]
    return result, nonce


def test_adopt_changed_sell_source_archives_without_exchange_write_or_auto(db, scenario):
    slot, row, read = scenario
    before = db.get(MexcPendingPrice, slot.key).body_json
    result, nonce = preview(db)
    assert '26,110' in result['content'] and 'Auto giữ TẮT' in result['content']
    assert slot.enabled is False and slot.state == 'UNKNOWN'
    message = asyncio.run(adoption.confirm(db, 'owner', nonce))
    assert 'Auto vẫn TẮT' in message
    assert slot.state == 'IDLE' and not slot.enabled and not slot.paused_by_floor
    assert db.get(MexcPendingPrice, slot.key) is None
    audit = db.scalar(select(OKXAuditLog).where(OKXAuditLog.action == 'MEXC_SLOT_ADOPTED'))
    detail = json.loads(audit.detail)
    assert detail['previous']['pending']['body_json'] == before
    assert detail['adopted_source']['body']['payMethod'] == '888'
    assert detail['old_result'] == 'UNRESOLVED' and not detail['exchange_write']
    assert read.await_count == 4  # Two GET readbacks at each operator step.
    with pytest.raises(ValueError, match='đã dùng'):
        asyncio.run(adoption.confirm(db, 'owner', nonce))


@pytest.mark.parametrize('field,value', [
    ('price', '26111'), ('quantity', '201'), ('availableQuantity', '149'),
    ('frozenQuantity', '1'), ('minSingleTransAmount', '200000'),
    ('maxSingleTransAmount', '2999999'), ('advStatus', 'CLOSE'),
    ('tradeTerms', 'changed'), ('paymentInfo', [{'id': 999, 'payMethod': 2}])])
def test_source_changed_after_preview_keeps_lock(db, scenario, field, value):
    slot, row, _ = scenario
    _, nonce = preview(db)
    row[field] = value
    with pytest.raises(ValueError):
        asyncio.run(adoption.confirm(db, 'owner', nonce))
    assert slot.state == 'UNKNOWN' and not slot.enabled
    assert db.get(MexcPendingPrice, slot.key) is not None


@pytest.mark.parametrize('change', ['owner', 'expired', 'cancelled', 'revision', 'pending'])
def test_one_use_confirmation_bound_to_owner_request_and_revision(db, scenario, change):
    slot, _, _ = scenario
    _, nonce = preview(db)
    draft = db.get(adoption.MexcAdoptionDraft, nonce)
    owner = 'other' if change == 'owner' else 'owner'
    if change == 'expired': draft.expires = 0
    if change == 'cancelled': draft.used = True
    if change == 'revision': slot.revision = 'new'
    if change == 'pending': db.get(MexcPendingPrice, slot.key).body_json = '{}'
    db.commit()
    with pytest.raises(ValueError): asyncio.run(adoption.confirm(db, owner, nonce))
    assert slot.state == 'UNKNOWN'


@pytest.mark.parametrize('change', ['young', 'submitting', 'buy', 'relative', 'absolute', 'visibility', 'missing', 'frozen'])
def test_ineligible_intents_never_offer_adoption(db, scenario, change):
    slot, row, _ = scenario
    pending = db.get(MexcPendingPrice, slot.key)
    body = json.loads(pending.body_json)
    if change == 'young': slot.last_action = Decimal(str(time.time()))
    if change == 'submitting': slot.state = 'SUBMITTING'
    if change == 'buy': slot.key = 'BUY:1'
    if change == 'relative': body['supplyQuantity'] = '1'
    if change == 'absolute': body['initQuantity'] = '101'
    if change == 'visibility': slot.action = 'HIDE'
    if change == 'missing': db.delete(pending)
    if change == 'frozen': row['frozenQuantity'] = '1'
    if change not in {'missing', 'buy'}: pending.body_json = json.dumps(body)
    db.commit()
    with pytest.raises(ValueError): preview(db)
    assert slot.state != 'IDLE'


def test_change_between_two_reads_keeps_lock(db, scenario, monkeypatch):
    slot, row, read = scenario
    async def unstable(*args):
        result = dict(row)
        row['availableQuantity'] = '149'
        return result
    read.side_effect = unstable
    with pytest.raises(ValueError, match='vừa thay đổi'): preview(db)
    assert slot.state == 'UNKNOWN'


def test_concurrent_change_after_confirm_reads_keeps_lock(db, scenario):
    slot, _, read = scenario
    _, nonce = preview(db)
    old_read = read.side_effect
    def race(*args):
        result = old_read(*args)
        slot.revision = 'concurrent'
        db.commit()
        return result
    read.side_effect = race
    with pytest.raises(ValueError): asyncio.run(adoption.confirm(db, 'owner', nonce))
    assert slot.state == 'UNKNOWN'


def test_cancel_is_owner_bound_and_keeps_pending(db, scenario):
    slot, _, _ = scenario
    _, nonce = preview(db)
    payload = {'type': 3, 'data': {'custom_id': 'mprice:adoptcancel:' + nonce}}
    asyncio.run(panel.execute_panel(payload, db, 'other'))
    assert not db.get(adoption.MexcAdoptionDraft, nonce).used
    asyncio.run(panel.execute_panel(payload, db, 'owner'))
    assert db.get(adoption.MexcAdoptionDraft, nonce).used
    assert db.get(MexcPendingPrice, slot.key) is not None and slot.state == 'UNKNOWN'


def test_closed_ad_adopted_without_reopening(db, scenario):
    slot, row, _ = scenario
    row['advStatus'] = 'CLOSE'
    _, nonce = preview(db)
    asyncio.run(adoption.confirm(db, 'owner', nonce))
    assert row['advStatus'] == 'CLOSE' and not slot.paused_by_floor and not slot.enabled


@pytest.fixture(params=['SHOW','HIDE'])
def visibility_scenario(db,scenario,request):
    slot,row,read=scenario
    action=request.param
    slot.action=action;slot.state='CONFIRMING'
    slot.wanted_status='OPEN' if action=='SHOW' else 'CLOSE'
    old=dict(row,advStatus='CLOSE' if action=='SHOW' else 'OPEN')
    slot.wanted_price=Decimal(old['price'])
    record_pending(db,slot,old,{},status=slot.wanted_status,cause='PRICE')
    db.commit()
    row.update(price='26090',quantity='58218.6009',availableQuantity='20000',maxSingleTransAmount='499999999',advStatus='OPEN')
    return slot,row,read


def test_visibility_lock_can_be_explicitly_adopted_without_replaying(db,visibility_scenario):
    slot,row,read=visibility_scenario
    previous=adoption.record(slot,db.get(MexcPendingPrice,slot.key))
    result,nonce=preview(db)
    assert 'Khác cấu hình cũ' in result['content'] and '26,090' in result['content']
    assert 'Hiển thị' in result['content']
    assert slot.state=='CONFIRMING' and not slot.enabled
    asyncio.run(adoption.confirm(db,'owner',nonce))
    assert slot.state=='IDLE' and not slot.enabled
    assert not slot.paused_by_floor
    assert db.get(MexcPendingPrice,slot.key) is None
    detail=json.loads(db.scalar(select(OKXAuditLog).where(OKXAuditLog.action=='MEXC_SLOT_ADOPTED')).detail)
    assert detail['previous']['pending']==previous['pending']
    assert detail['exchange_write'] is False and detail['old_result']=='UNRESOLVED'
    assert row['advStatus']=='OPEN' and read.await_count==4


@pytest.mark.parametrize('change',['body','status','quantity','side','attempt','young','frozen','wanted_price'])
def test_visibility_adoption_rejects_bad_or_changed_intents(db,visibility_scenario,change):
    slot,row,_=visibility_scenario
    pending=db.get(MexcPendingPrice,slot.key)
    expected=json.loads(pending.expected_json)
    if change=='body':pending.body_json='{"supplyQuantity":"1"}'
    if change=='status':expected['advStatus']='WRONG'
    if change=='quantity':expected['quantity']='1'
    if change=='side':
        before=json.loads(pending.before_json);before['body']['side']='BUY';pending.before_json=json.dumps(before)
    if change=='attempt':pending.attempt='other'
    if change=='young':slot.last_action=Decimal(str(time.time()))
    if change=='frozen':row['frozenQuantity']='1'
    if change=='wanted_price':slot.wanted_price=Decimal('1')
    pending.expected_json=json.dumps(expected);db.commit()
    with pytest.raises(ValueError):preview(db)
    assert slot.state=='CONFIRMING' and db.get(MexcPendingPrice,slot.key) is not None


def test_check_recovery_directly_offers_owner_confirmation_for_visibility(db,visibility_scenario,monkeypatch):
    slot,row,_=visibility_scenario
    monkeypatch.setattr(panel,'recover',AsyncMock(return_value='Chưa khớp cấu hình cũ.'))
    result=asyncio.run(panel.execute_panel({'type':3,'data':{'custom_id':'mprice:checkslot:SELL:1'}},db,'owner'))
    assert 'Đã đọc hai lần' in result['content']
    assert result['components'][0]['components'][0]['custom_id'].startswith('mprice:adoptconfirm:')
    assert slot.state=='CONFIRMING' and not slot.enabled


def test_recovery_explains_why_adoption_is_blocked_and_has_retry(db,visibility_scenario,monkeypatch):
    slot,row,_=visibility_scenario
    row['frozenQuantity']='1'
    monkeypatch.setattr(panel,'recover',AsyncMock(return_value='Chưa khớp cấu hình cũ.'))
    result=asyncio.run(panel.execute_panel({'type':3,'data':{'custom_id':'mprice:checkslot:SELL:1'}},db,'owner'))
    assert 'giữ USDT' in result['content'] and 'Chưa thể nhận cấu hình' in result['content']
    assert result['components'][0]['components'][0]['label']=='Kiểm tra lại'
    assert slot.state=='CONFIRMING'


def test_idle_sell_can_be_read_and_adopted_without_a_lock(db,scenario):
    slot,row,_=scenario
    db.delete(db.get(MexcPendingPrice,slot.key));slot.state='IDLE';db.commit()
    _,nonce=preview(db)
    asyncio.run(adoption.confirm(db,'owner',nonce))
    assert not slot.enabled and slot.state=='IDLE'


def test_diff_uses_numeric_equivalence_and_concise_field_names():
    from app.mexc_price_recovery import difference_summary
    row=dict(price=26090.0,quantity='58218.6009',minSingleTransAmount='50000000.0',maxSingleTransAmount=499999999,advStatus='OPEN')
    expected=dict(price='25969',quantity='6936.4101',minSingleTransAmount='50000000',maxSingleTransAmount='180131632',advStatus='OPEN')
    text=difference_summary(row,expected)
    assert 'Giá: hồ sơ cũ 25,969 → hiện tại 26,090' in text
    assert 'Tổng USDT' in text and 'Max:' in text
    assert 'Min:' not in text and 'Hiển thị:' not in text


def test_confirm_shows_explicit_operator_auto_button_but_does_not_enable(db,visibility_scenario):
    slot,row,_=visibility_scenario
    _,nonce=preview(db)
    response=asyncio.run(panel.execute_panel({'type':3,'data':{'custom_id':'mprice:adoptconfirm:'+nonce}},db,'owner'))
    assert response['components'][0]['components'][0]['custom_id']=='mprice:setauto:SELL:1:1'
    assert slot.state=='IDLE' and not slot.enabled


def test_closed_ad_confirmation_explains_exchange_step_without_enable_button(db,scenario):
    slot,row,_=scenario;row['advStatus']='CLOSE'
    _,nonce=preview(db)
    response=asyncio.run(panel.execute_panel({'type':3,'data':{'custom_id':'mprice:adoptconfirm:'+nonce}},db,'owner'))
    assert 'mở quảng cáo trên MEXC trước' in response['content']
    assert not any('setauto' in b['custom_id'] for r in response['components'] for b in r['components'])
    assert not slot.enabled


@pytest.fixture
def legacy_show(db,scenario):
    slot,row,read=scenario
    before=dict(row,advStatus='CLOSE',price='25969',quantity='45155.011',availableQuantity='6936.4101',minSingleTransAmount='50000000',maxSingleTransAmount='180131632')
    slot.state='CONFIRMING';slot.action='SHOW';slot.wanted_price=Decimal('25969');slot.wanted_status='OPEN'
    body=adoption.source_snapshot(before)['body'];body.update(initQuantity='6936.4101',advStatus='CLOSE')
    record_pending(db,slot,before,body,status='OPEN',cause='PRICE');db.commit()
    return slot,row,read


def test_real_legacy_show_with_unsent_sell_quantity_body_can_be_adopted(db,legacy_show):
    slot,row,read=legacy_show
    _,nonce=preview(db)
    asyncio.run(adoption.confirm(db,'owner',nonce))
    assert slot.state=='IDLE' and not slot.enabled
    assert db.get(MexcPendingPrice,slot.key) is None
    detail=json.loads(db.scalar(select(OKXAuditLog).where(OKXAuditLog.action=='MEXC_SLOT_ADOPTED')).detail)
    assert detail['previous']['pending']['body_json']
    assert not detail['exchange_write'] and read.await_count==4


@pytest.mark.parametrize('change',['relative','stock','price','limits','bank','side','frozen','status'])
def test_legacy_show_requires_exact_known_unused_body_shape(db,legacy_show,change):
    slot,row,_=legacy_show
    pending=db.get(MexcPendingPrice,slot.key)
    body=json.loads(pending.body_json);before=json.loads(pending.before_json)
    if change=='relative':body['supplyQuantity']='1'
    if change=='stock':body['initQuantity']='999'
    if change=='price':body['price']='25000'
    if change=='limits':body['maxSingleTransAmount']='1'
    if change=='bank':body['payMethod']='999'
    if change=='side':body['side']='BUY'
    if change=='frozen':before['frozen']='1'
    if change=='status':body['advStatus']='OPEN'
    pending.body_json=json.dumps(body);pending.before_json=json.dumps(before);db.commit()
    with pytest.raises(ValueError):preview(db)
    assert slot.state=='CONFIRMING' and db.get(MexcPendingPrice,slot.key) is not None

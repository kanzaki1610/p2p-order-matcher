import asyncio
import json
import time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import mexc_buy_budget as budget
from app import mexc_price_adoption as adoption
from app import mexc_pricing as worker
from app.mexc_price_state import MexcPendingPrice,record_pending
from test_mexc_pricing import ad,db


def order(code='d1',state='DONE'):
    return dict(advOrderNo=code,advNo='a123',createTime=1,state=state,
        amount='500000',tradableQuantity='20',coinName='USDT',fiatUnit='VND',side='SELL')


def setup_budget(db):
    slot=worker.MexcPriceSlot(key='BUY:1',ad='a123',enabled=False)
    db.add(slot);db.flush()
    db.add(budget.MexcBuyBudget(ad='a123',slot=slot.key,total_vnd=2000000))
    db.add(budget.MexcBudgetOrder(order='d1',ad='a123',fiat=500000,crypto=20,state='DONE'))
    db.commit()
    return slot,ad('BUY')


def test_missing_completed_order_resolved_by_authenticated_detail_without_reset(db):
    slot,row=setup_budget(db)
    api=SimpleNamespace(request=AsyncMock(return_value=[]),get_order_detail=AsyncMock(return_value=order()))
    assert asyncio.run(budget.remaining(db,api,slot,row))==1500000
    api.get_order_detail.assert_awaited_once_with('d1')
    assert db.get(budget.MexcBuyBudget,'a123').total_vnd==2000000
    assert budget.completed_spend(db,'a123')==500000
    assert asyncio.run(budget.remaining(db,api,slot,row,force=True))==1500000
    assert budget.completed_spend(db,'a123')==500000


@pytest.mark.parametrize('field,value',[('advNo','other'),('advOrderNo','other'),('amount','600000'),
    ('tradableQuantity','21'),('coinName','BTC'),('fiatUnit','USD'),('state','CANCEL')])
def test_missing_detail_mismatch_blocks_without_erasing_ledger(db,field,value):
    slot,row=setup_budget(db)
    api=SimpleNamespace(request=AsyncMock(return_value=[]),get_order_detail=AsyncMock(return_value={**order(),field:value}))
    with pytest.raises(ValueError):asyncio.run(budget.remaining(db,api,slot,row))
    db.rollback()
    assert budget.completed_spend(db,'a123')==500000


def test_unreadable_missing_detail_blocks(db):
    slot,row=setup_budget(db)
    api=SimpleNamespace(request=AsyncMock(return_value=[]),get_order_detail=AsyncMock(side_effect=RuntimeError('secret-url')))
    with pytest.raises(ValueError,match='chưa xác minh') as error:
        asyncio.run(budget.remaining(db,api,slot,row))
    assert 'secret' not in str(error.value)


def test_missing_pending_detail_still_pending_blocks(db):
    slot,row=setup_budget(db)
    db.get(budget.MexcBudgetOrder,'d1').state='PAID';db.commit()
    api=SimpleNamespace(request=AsyncMock(return_value=[]),get_order_detail=AsyncMock(return_value=order(state='PAID')))
    with pytest.raises(ValueError,match='đang chờ'):
        asyncio.run(budget.remaining(db,api,slot,row))


@pytest.fixture
def buy_adoption(db,monkeypatch):
    slot,row=setup_budget(db)
    read=AsyncMock(side_effect=lambda *args:dict(row))
    refresh=AsyncMock(return_value=Decimal(1500000))
    monkeypatch.setattr(adoption,'read_ad',read)
    monkeypatch.setattr(adoption,'remaining',refresh)
    monkeypatch.setattr(adoption.asyncio,'sleep',AsyncMock())
    return slot,row,read,refresh


def preview(db):
    result=asyncio.run(adoption.preview(db,'owner','1','BUY'))
    nonce=result['components'][0]['components'][0]['custom_id'].split(':')[-1]
    return result,nonce


def test_idle_buy_preview_confirm_preserves_remaining_vnd_and_leaves_auto_off(db,buy_adoption):
    slot,row,read,refresh=buy_adoption
    result,nonce=preview(db)
    assert '1,500,000 VND' in result['content']
    assert 'không đặt lại theo Max' in result['content']
    message=asyncio.run(adoption.confirm(db,'owner',nonce))
    assert 'Giữ nguyên ngân sách VND' in message and not slot.enabled and slot.state=='IDLE'
    assert db.get(budget.MexcBuyBudget,slot.ad).total_vnd==2000000
    assert budget.completed_spend(db,slot.ad)==500000
    assert read.await_count==4 and refresh.await_count==2
    assert refresh.call_args.kwargs['force'] is True


def test_unknown_relative_buy_adoption_archives_intent_without_replaying(db,buy_adoption):
    slot,row,_,_=buy_adoption
    slot.state='UNKNOWN';slot.action='PRICE';slot.attempt_id='old';slot.last_action=0
    body={**adoption.source_snapshot(row)['body'],'reduceQuantity':'10','advStatus':'OPEN'}
    record_pending(db,slot,row,body);db.commit()
    row['quantity']='120';row['availableQuantity']='120'
    _,nonce=preview(db)
    asyncio.run(adoption.confirm(db,'owner',nonce))
    assert slot.state=='IDLE' and not slot.enabled and db.get(MexcPendingPrice,slot.key) is None
    assert db.get(budget.MexcBuyBudget,slot.ad).total_vnd==2000000


@pytest.mark.parametrize('change',['budget','spent','pending','stock','owner','expired'])
def test_buy_confirmation_rejects_changed_budget_ledger_or_source(db,buy_adoption,change):
    slot,row,_,_=buy_adoption
    _,nonce=preview(db)
    if change=='budget':db.get(budget.MexcBuyBudget,slot.ad).total_vnd=3000000
    if change=='spent':db.add(budget.MexcBudgetOrder(order='d2',ad=slot.ad,fiat=500000,crypto=20,state='DONE'))
    if change=='pending':db.get(budget.MexcBudgetOrder,'d1').state='PAID'
    if change=='stock':row['availableQuantity']='99'
    if change=='expired':db.get(adoption.MexcAdoptionDraft,nonce).expires=0
    db.commit()
    with pytest.raises(ValueError):asyncio.run(adoption.confirm(db,'other' if change=='owner' else 'owner',nonce))
    assert not db.get(adoption.MexcAdoptionDraft,nonce).used


def test_buy_preview_stops_on_pending_or_unverified_history(db,buy_adoption):
    _,_,_,refresh=buy_adoption
    refresh.side_effect=ValueError('Có lệnh mua đang chờ')
    with pytest.raises(ValueError,match='đang chờ'):preview(db)


def test_new_unknown_buy_request_cannot_be_adopted(db,buy_adoption):
    slot,row,_,_=buy_adoption
    slot.state='UNKNOWN';slot.action='PRICE';slot.attempt_id='old';slot.last_action=Decimal(str(time.time()))
    record_pending(db,slot,row,{**adoption.source_snapshot(row)['body'],'advStatus':'OPEN'});db.commit()
    with pytest.raises(ValueError,match='10 phút'):preview(db)

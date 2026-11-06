import asyncio
import json
import time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app import mexc_buy_budget as budget
from app import mexc_auto_terms as terms
from app import mexc_price_panel as panel
from app import mexc_price_recovery as recovery
from app import mexc_pricing as worker
from app.mexc_price_state import MexcPendingPrice, MexcPricingSwitch, record_pending, matches, expected_for
from test_mexc_pricing import ad, db, setup


def order(code='o1', state='DONE', fiat='132475000', crypto='5000'):
    return dict(advOrderNo=code, advNo='a123', createTime=1, state=state,
        amount=fiat, tradableQuantity=crypto, coinName='USDT', fiatUnit='VND', side='SELL')


def buy_slot(db):
    slot=worker.MexcPriceSlot(key='BUY:1',ad='a123',rank=10,nickname='VNTrader',enabled=True)
    db.add(slot);db.commit()
    row=ad('BUY');row.update(quantity='6000',availableQuantity='1000',maxSingleTransAmount='158532000')
    return slot,row


def test_budget_preserves_remaining_vnd_across_price_changes_and_restart(db):
    slot,row=buy_slot(db);api=SimpleNamespace(request=AsyncMock(return_value=[order()]))
    assert asyncio.run(budget.remaining(db,api,slot,row))==Decimal('26057000')
    first=asyncio.run(terms.prepare(db,api,slot,row,Decimal('26495')))
    assert first['body']['maxSingleTransAmount']=='26057000'
    assert Decimal(first['body']['reduceQuantity'])>0
    db.expire_all();row['price']='25000'
    second=asyncio.run(terms.prepare(db,api,slot,row,Decimal('25000')))
    assert second['remaining_vnd']==Decimal('26057000')
    assert second['body']['maxSingleTransAmount']=='26057000'
    assert Decimal(second['body']['supplyQuantity'])>0
    assert db.get(budget.MexcBuyBudget,slot.ad).total_vnd==Decimal('158532000')


@pytest.mark.parametrize('state',['NOT_PAID','PAID','WAIT_PROCESS','PROCESSING'])
def test_pending_buy_blocks_repricing(db,state):
    slot,row=buy_slot(db);api=SimpleNamespace(request=AsyncMock(return_value=[order(state=state)]))
    with pytest.raises(ValueError,match='đang chờ'):asyncio.run(budget.remaining(db,api,slot,row))


def test_cancelled_does_not_spend_but_missing_record_blocks(db):
    slot,row=buy_slot(db);api=SimpleNamespace(request=AsyncMock(return_value=[order(state='CANCEL')]))
    assert asyncio.run(budget.remaining(db,api,slot,row))==Decimal('158532000')
    db.get(budget.MexcBuyBudget,slot.ad).last_scan=0;db.commit();api.request.return_value=[]
    with pytest.raises(ValueError,match='thiếu lệnh'):asyncio.run(budget.remaining(db,api,slot,row))


def test_frozen_buy_never_reads_history_or_replenishes(db):
    slot,row=buy_slot(db);row['frozenQuantity']='1';api=SimpleNamespace(request=AsyncMock())
    with pytest.raises(ValueError,match='giữ số dư'):asyncio.run(budget.remaining(db,api,slot,row))
    api.request.assert_not_awaited()


def test_cursor_uses_order_id_for_equal_timestamp(monkeypatch):
    rows=[order(code='o'+str(i)) for i in range(100)]
    api=SimpleNamespace(request=AsyncMock(side_effect=[rows,[order(code='last')]]))
    monkeypatch.setattr(budget.asyncio,'sleep',AsyncMock())
    result=asyncio.run(budget.read_history(api,ad('BUY')))
    assert len(result)==101
    assert api.request.call_args.kwargs['params']['lastId']=='o99'
    assert api.request.call_args.kwargs['params']['lastCreateTime']==1


def test_duplicate_history_blocks():
    api=SimpleNamespace(request=AsyncMock(return_value=[order(),order()]))
    with pytest.raises(ValueError,match='trùng ID'):asyncio.run(budget.read_history(api,ad('BUY')))


def test_manual_budget_recovery_does_not_replenish_later_fills(db):
    slot,row=buy_slot(db);api=SimpleNamespace(request=AsyncMock(return_value=[order()]))
    asyncio.run(budget.remaining(db,api,slot,row));spent=budget.completed_spend(db,slot.ad)
    db.add(budget.MexcBudgetOrder(order='late',ad=slot.ad,fiat=1000000,crypto=40,state='DONE'));db.commit()
    budget.rebase_after_manual(db,slot,'26057000',spent);db.commit()
    assert db.get(budget.MexcBuyBudget,slot.ad).total_vnd-budget.completed_spend(db,slot.ad)==Decimal('25057000')


@pytest.mark.parametrize('price,maximum',[('26500','79499999'),('26100','78299999')])
def test_sell_max_uses_remaining_stock_not_initial_total(db,price,maximum):
    slot=worker.MexcPriceSlot(key='SELL:1',ad='a123');db.add(slot);db.commit()
    row=ad();row.update(quantity='5000',availableQuantity='3000',frozenQuantity='100')
    result=asyncio.run(terms.prepare(db,None,slot,row,Decimal(price)))
    assert result['body']['maxSingleTransAmount']==maximum
    assert result['body']['initQuantity']=='3000'
    assert result['expected_extra']['quantity']=='5000'
    assert 'supplyQuantity' not in result['body'] and 'reduceQuantity' not in result['body']


def test_sell_below_min_is_exhausted(db):
    slot=worker.MexcPriceSlot(key='SELL:1',ad='a123');db.add(slot);db.commit()
    row=ad();row['availableQuantity']='1'
    assert asyncio.run(terms.prepare(db,None,slot,row,Decimal('26000')))['exhausted']


@pytest.mark.parametrize('field',['price','quantity','minSingleTransAmount','maxSingleTransAmount','advStatus'])
def test_readback_requires_all_terms(field):
    row=ad();expected=expected_for(row,terms.update_body(row,Decimal(row['price'])))
    assert matches(row,expected)
    row[field]='CLOSE' if field=='advStatus' else '123'
    assert not matches(row,expected)


def test_global_pause_keeps_slots_and_sends_no_exchange_write(db,setup):
    slot,_=setup
    result=asyncio.run(panel.execute_panel({'type':3,'data':{'custom_id':'mprice:pause'}},db,'owner'))
    assert slot.enabled and not db.get(MexcPricingSwitch,1).enabled
    asyncio.run(worker.process_pricing(db,None,None))
    worker.write_price.assert_not_awaited();worker.write_visibility.assert_not_awaited()
    assert 'OKX' in result['content']


def test_shared_cooldown_between_sell_slots(db,setup):
    slot,_=setup
    other=worker.MexcPriceSlot(key='SELL:2',ad='a456',rank=10,nickname='VNTrader',enabled=False,last_action=Decimal(str(time.time())))
    db.add(other);db.commit();asyncio.run(worker.process_pricing(db,None,None))
    worker.write_price.assert_not_awaited()


def test_panel_has_full_controls_and_valid_discord_dimensions():
    board=panel.panel();buttons=[b for r in board['components'] for b in r['components']]
    assert len(board['components'])==5 and len(buttons)==19
    assert all(len(r['components'])<=5 for r in board['components'])
    for action in ('price','rank','ad','auto','list','recover'):
        assert all(any(b['custom_id']=='mprice:'+action+':'+side for b in buttons) for side in ('BUY','SELL'))
    assert {'mprice:pause','mprice:resume','mprice:limits:SELL','mprice:cost:SELL'} <= {b['custom_id'] for b in buttons}
    for action in ('price','rank','ad','cost','limits'):
        modal=panel.modal(action,'SELL')['data']
        assert len(modal['components'])<=5 and len(modal['title'])<=45
        assert all(len(r['components'][0]['label'])<=45 for r in modal['components'])


def test_recovery_only_clears_exact_intent_and_keeps_auto_off(db,setup,monkeypatch):
    slot,row=setup;slot.state='UNKNOWN';slot.action='PRICE';slot.attempt_id='attempt';slot.last_action=1
    body=terms.update_body(row,Decimal('26070'));record_pending(db,slot,row,body);db.commit()
    target=dict(row,price='26070')
    client=SimpleNamespace(__aenter__=AsyncMock(),__aexit__=AsyncMock())
    class API:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    monkeypatch.setattr(recovery,'MexcP2PClient',API)
    monkeypatch.setattr(recovery,'read_ad',AsyncMock(return_value=target))
    text=asyncio.run(recovery.recover(db,'owner','SELL',1))
    assert slot.state=='IDLE' and not slot.enabled and db.get(MexcPendingPrice,slot.key) is None
    assert 'Đã xác minh' in text;worker.write_price.assert_not_awaited()


@pytest.mark.parametrize('legacy,wrongmax',[(True,False),(False,True)])
def test_recovery_does_not_clear_legacy_or_inexact_result(db,setup,monkeypatch,legacy,wrongmax):
    slot,row=setup;slot.state='UNKNOWN';slot.action='PRICE';slot.attempt_id='attempt';slot.last_action=1
    if not legacy:record_pending(db,slot,row,terms.update_body(row,Decimal('26070')))
    db.commit()
    class API:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    monkeypatch.setattr(recovery,'MexcP2PClient',API)
    monkeypatch.setattr(recovery,'read_ad',AsyncMock(return_value=dict(row,price='26070',maxSingleTransAmount='1')))
    asyncio.run(recovery.recover(db,'owner','SELL',1))
    assert slot.state=='UNKNOWN' and not slot.enabled;worker.write_price.assert_not_awaited()


def test_same_slot_and_cost_in_two_databases_are_isolated():
    engines=[create_engine('sqlite://'),create_engine('sqlite://')]
    try:
        for engine in engines:Base.metadata.create_all(engine)
        with Session(engines[0]) as primary, Session(engines[1]) as friend:
            primary.add(worker.MexcPriceSlot(key='SELL:1',ad='primary',enabled=True))
            friend.add(worker.MexcPriceSlot(key='SELL:1',ad='friend',enabled=False))
            worker.cost_settings(primary).cost=Decimal('26000');worker.cost_settings(friend).cost=Decimal('25000')
            primary.commit();friend.commit()
            asyncio.run(panel.execute_panel({'type':3,'data':{'custom_id':'mprice:pause'}},friend,'friend'))
            assert primary.get(worker.MexcPriceSlot,'SELL:1').enabled
            assert friend.get(worker.MexcPriceSlot,'SELL:1').ad=='friend'
            assert worker.cost_settings(primary).cost==26000 and worker.cost_settings(friend).cost==25000
            assert primary.get(MexcPricingSwitch,1) is None and friend.get(MexcPricingSwitch,1).enabled is False
    finally:
        for engine in engines:engine.dispose()


@pytest.mark.parametrize('error,accepted',[
    ('MEXC HTTP 400 · mã API 700002.',True),
    ('MEXC HTTP 429 · mã API 700002.',False),
    ('MEXC HTTP 400 · mã API 99999.',False),
    ('Không xác minh được phản hồi MEXC (ReadTimeout).',False),
])
def test_legacy_recovery_only_accepts_explicit_signature_rejection(db,setup,monkeypatch,error,accepted):
    slot,row=setup;slot.state='UNKNOWN';slot.action='PRICE';slot.attempt_id='old';slot.last_action=1
    slot.reason='Chưa rõ kết quả sửa/ẩn quảng cáo. '+error+' Không tự gửi lại; kiểm tra MEXC.';db.commit()
    class API:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    monkeypatch.setattr(recovery,'MexcP2PClient',API)
    monkeypatch.setattr(recovery,'read_ad',AsyncMock(return_value=row))
    result=asyncio.run(recovery.recover(db,'owner','SELL',1))
    assert slot.state==('IDLE' if accepted else 'UNKNOWN') and not slot.enabled
    if accepted:assert '700002' in result
    worker.write_price.assert_not_awaited();worker.write_visibility.assert_not_awaited()


def test_business_rejection_code_is_preserved_in_alert_and_pending_intent(db,setup,monkeypatch):
    from app.mexc import MexcAPIError
    slot,_=setup
    monkeypatch.setattr(worker,'write_price',AsyncMock(side_effect=MexcAPIError(
        'MEXC HTTP 200 · 700007: rejected',http_status=200,api_code=700007)))
    asyncio.run(worker.process_pricing(db,None,None))
    assert slot.state=='UNKNOWN' and '700007' in slot.reason
    saved=json.loads(db.get(MexcPendingPrice,slot.key).expected_json)
    assert saved['api_http_status']==200 and saved['api_code']=='700007'
    asyncio.run(worker.process_pricing(db,None,None));worker.write_price.assert_awaited_once()


def test_resume_does_not_claim_actual_repricing_without_ready_slots(db,monkeypatch):
    monkeypatch.setattr(panel,'valid_webhook',lambda value:True)
    result=asyncio.run(panel.execute_panel({'type':3,'data':{'custom_id':'mprice:resume'}},db,'owner'))
    assert 'Chưa có slot Auto sẵn sàng' in result['content']
    assert 'chưa xác nhận đổi giá trên sàn' in result['content']


def test_status_explicitly_reports_unknown_as_stopped(db,setup):
    slot,_=setup;slot.state='UNKNOWN';db.commit()
    result=asyncio.run(panel.execute_panel({'type':3,'data':{'custom_id':'mprice:status'}},db,'owner'))
    assert 'ĐANG DỪNG' in result['content'] and 'Sẵn sàng' not in result['content']


def test_status_reports_requested_price_and_missing_legacy_api_code(db,setup):
    slot,row=setup;slot.state='UNKNOWN';slot.attempt_id='old';slot.wanted_price=Decimal('26195');slot.last_action=1
    record_pending(db,slot,row,terms.update_body(row,slot.wanted_price));db.commit()
    result=asyncio.run(panel.execute_panel({'type':3,'data':{'custom_id':'mprice:status'}},db,'owner'))
    assert '26,195đ' in result['content'] and 'chưa lưu mã API gốc' in result['content']


def legacy_pending(db,setup):
    slot,row=setup;slot.state='UNKNOWN';slot.action='PRICE';slot.attempt_id='legacy';slot.last_action=Decimal(str(time.time()-1000))
    slot.reason=('Chưa rõ kết quả sửa/ẩn quảng cáo. Không xác minh được phản hồi MEXC '
                 '(MexcAPIError). Không tự gửi lại; kiểm tra MEXC.')
    pending=record_pending(db,slot,row,{**terms.update_body(row,Decimal('26070')),'advStatus':row['advStatus']})
    db.commit()
    current=dict(row,availableQuantity='80',updateTime=int(time.time()*1000)-1000)
    return slot,pending,current


@pytest.mark.parametrize('guard',['recent','no_later_update','buy','relative_quantity','absolute_quantity','new_diagnostics',
    'new_unknown_transport','price_changed','quantity_changed','limits_changed','payment_changed',
    'terms_changed','frozen','available_increased','different_error'])
def test_legacy_source_reconciliation_does_not_relax_other_unknown_fences(db,setup,guard):
    slot,pending,current=legacy_pending(db,setup)
    assert recovery.legacy_source_reconcilable(slot,pending,current)
    if guard=='recent':slot.last_action=Decimal(str(time.time()-100))
    elif guard=='no_later_update':current['updateTime']=1
    elif guard=='buy':slot.key='BUY:1'
    elif guard=='relative_quantity':pending.body_json=json.dumps({**json.loads(pending.body_json),'supplyQuantity':'1'})
    elif guard=='absolute_quantity':pending.body_json=json.dumps({**json.loads(pending.body_json),'initQuantity':'101'})
    elif guard in {'new_diagnostics','new_unknown_transport'}:
        pending.expected_json=json.dumps({**json.loads(pending.expected_json),'api_http_status':200 if guard=='new_diagnostics' else None})
    elif guard=='price_changed':current['price']='26075'
    elif guard=='quantity_changed':current['quantity']='101'
    elif guard=='limits_changed':current['maxSingleTransAmount']='1999999'
    elif guard=='payment_changed':current['paymentInfo']=[{'id':888,'payMethod':1}]
    elif guard=='terms_changed':current['tradeTerms']='changed'
    elif guard=='frozen':current['frozenQuantity']='1'
    elif guard=='available_increased':current['availableQuantity']='101'
    elif guard=='different_error':slot.reason='Timeout'
    assert not recovery.legacy_source_reconcilable(slot,pending,current)


@pytest.mark.parametrize('stock_changes_during_check',[False,True])
def test_operator_legacy_source_reconciliation_is_get_only_and_leaves_auto_off(db,setup,monkeypatch,stock_changes_during_check):
    slot,pending,current=legacy_pending(db,setup)
    class API:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    monkeypatch.setattr(recovery,'MexcP2PClient',API)
    monkeypatch.setattr(recovery.asyncio,'sleep',AsyncMock())
    second=dict(current,availableQuantity='79') if stock_changes_during_check else dict(current)
    monkeypatch.setattr(recovery,'read_ad',AsyncMock(side_effect=[current,second]))
    text=asyncio.run(recovery.recover(db,'owner','SELL',1))
    assert slot.state==('UNKNOWN' if stock_changes_during_check else 'IDLE') and not slot.enabled
    assert recovery.read_ad.await_count==2
    if not stock_changes_during_check:assert 'chưa xác nhận yêu cầu cũ thành công' in text
    worker.write_price.assert_not_awaited();worker.write_visibility.assert_not_awaited()

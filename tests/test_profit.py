import asyncio
from datetime import datetime,timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app import profit
from test_web_control import client


@pytest.fixture(autouse=True)
def history_window(monkeypatch):
    monkeypatch.setattr(profit,'okx_history_floor',lambda:datetime(2020,1,1,tzinfo=timezone.utc))


def trade(code,side,q,f,at='2025-03-01T02:00:00+00:00',exchange='OKX'):
    return SimpleNamespace(key=exchange+':'+code,exchange=exchange,code=code,side=side,
        quantity=Decimal(str(q)),fiat=Decimal(str(f)),created_at=datetime.fromisoformat(at),completed_at=datetime.fromisoformat(at))


def calc(rows,start='2025-03-01',end='2025-03-31',opening=None,exchange='ALL'):
    a,b=profit.day_range(start,end)
    return profit.calculate(rows,a,b,opening,exchange)


def okx(code='12345',**values):
    return {'orderId':code,'side':'buy','orderStatus':'completed','paymentStatus':'confirmed',
            'cryptoCurrency':'usdt','fiatCurrency':'vnd','cryptoAmount':'100','fiatAmount':'2600000',
            'createdTimestamp':'1740794400000','completionTimestamp':'1740794460000',**values}


def test_fifo_multiple_lots_realized_loss_and_unsold_inventory():
    rows=[trade('1','BUY',100,2500000),trade('2','BUY',100,2600000,'2025-03-02T01:00:00+00:00'),
          trade('3','SELL',150,3900000,'2025-03-03T01:00:00+00:00'),
          trade('4','SELL',10,250000,'2025-03-04T01:00:00+00:00')]
    r=calc(rows);t=r['totals']
    assert Decimal(t['profit'])==90000 and Decimal(t['cost'])==4060000
    assert Decimal(t['inventory_qty'])==40 and Decimal(t['inventory_cost'])==1040000
    assert Decimal(r['rows'][0]['profit'])==-10000


def test_period_filter_keeps_prior_purchase_cost_and_vn_midnight_boundary():
    rows=[trade('1','BUY',100,2500000,'2025-02-01T00:00:00+00:00'),
          trade('2','SELL',10,260000,'2025-02-28T16:59:59+00:00'),
          trade('3','SELL',10,260000,'2025-02-28T17:00:00+00:00'),
          trade('4','SELL',10,260000,'2025-03-01T17:00:00+00:00')]
    r=calc(rows,'2025-03-01','2025-03-01')
    assert r['totals']['sell_count']==1 and r['daily'][0]['date']=='2025-03-01'
    assert Decimal(r['totals']['profit'])==10000 and Decimal(r['totals']['inventory_qty'])==80


def test_missing_cost_never_becomes_zero_cost_or_uses_future_buy():
    rows=[trade('1','SELL',10,260000),trade('2','BUY',20,500000,'2025-03-02T00:00:00+00:00')]
    r=calc(rows)
    assert r['totals']['profit'] is None and Decimal(r['totals']['missing_qty'])==10
    assert Decimal(r['totals']['known_profit'])==0 and r['rows'][-1]['profit'] is None


def test_partial_cost_reports_only_priced_portion_profit():
    rows=[trade('1','BUY',5,125000,'2025-02-01T00:00:00+00:00'),trade('2','SELL',10,260000)]
    t=calc(rows)['totals']
    assert t['profit'] is None and Decimal(t['missing_qty'])==5 and Decimal(t['known_profit'])==5000


def test_exchange_filter_never_discards_other_venue_cost():
    rows=[trade('1','BUY',10,250000,exchange='MEXC'),trade('2','SELL',10,260000,'2025-03-02T00:00:00+00:00')]
    r=calc(rows,exchange='OKX')
    assert r['totals']['buy_count']==0 and r['totals']['sell_count']==1
    assert Decimal(r['totals']['profit'])==10000


def test_opening_resets_old_history_without_double_counting():
    opening=SimpleNamespace(day='2025-03-01',quantity=Decimal(10),unit_cost=Decimal(25000))
    rows=[trade('1','BUY',100,2400000,'2025-02-01T00:00:00+00:00'),trade('2','SELL',5,130000)]
    t=calc(rows,opening=opening)['totals']
    assert Decimal(t['inventory_qty'])==5 and Decimal(t['inventory_cost'])==125000
    assert Decimal(t['profit'])==5000
    assert Decimal(calc([],opening=opening)['totals']['inventory_qty'])==10


def test_same_timestamp_sell_does_not_assume_buy_completed_first():
    r=calc([trade('z','BUY',10,250000),trade('a','SELL',10,260000)])
    assert r['totals']['profit'] is None


def test_overnight_order_is_reported_and_costed_by_completion_time():
    buy=trade('1','BUY',10,250000,'2025-03-02T01:00:00+00:00')
    sell=trade('2','SELL',10,260000,'2025-03-01T01:00:00+00:00')
    sell.completed_at=datetime.fromisoformat('2025-03-02T02:00:00+00:00')
    r=calc([sell,buy],'2025-03-02','2025-03-02')
    assert r['totals']['sell_count']==1 and Decimal(r['totals']['profit'])==10000
    assert r['rows'][0]['completed']!=r['rows'][0]['created']


def test_missing_or_conflicting_completion_time_is_not_guessed():
    for change in ({'completionTimestamp':None},{'completionTimestamp':'1740794300000'},
                   {'completedTimestamp':'1740794490000'}):
        with pytest.raises(ValueError):profit.normalize('OKX',okx(**change))


@pytest.mark.parametrize('start,end',[('bad','2025-01-01'),('2025-02-02','2025-02-01'),('2024-01-01','2025-03-01')])
def test_invalid_periods(start,end):
    with pytest.raises(ValueError):profit.day_range(start,end)


@pytest.mark.parametrize('value',['NaN','Infinity','-1','0','1.000000001','1000000000000000'])
def test_invalid_numeric_values(value):
    with pytest.raises(ValueError):profit.normalize('OKX',okx(cryptoAmount=value))


@pytest.mark.parametrize('change',[{'orderStatus':'cancelled'},{'orderStatus':'new'}, {'paymentStatus':'unpaid'}, {'cryptoCurrency':'BTC'}])
def test_only_completed_usdt_vnd_is_imported(change):
    assert profit.normalize('OKX',okx(**change)) is None


@pytest.mark.parametrize('api_side,expected',[('BUY','SELL'),('SELL','BUY')])
def test_mexc_maker_direction_is_account_direction(monkeypatch,api_side,expected):
    monkeypatch.setattr(profit.settings,'mexc_p2p_api_incoming_side','BUY')
    monkeypatch.setattr(profit.settings,'mexc_p2p_incoming_side','SELL')
    row={'advOrderNo':'d12345','side':api_side,'state':'DONE','coinName':'USDT','fiatUnit':'VND',
         'tradableQuantity':'100','amount':'2600000','createTime':'1740794400000','finishTime':'1740794460000'}
    assert profit.normalize('MEXC',row)['side']==expected
    assert profit.normalize('MEXC',{**row,'state':'CANCEL'}) is None


def test_collect_follows_short_pages_until_empty_and_deduplicates(monkeypatch):
    reader=AsyncMock(side_effect=[[okx('12345')],[okx('12346')],[]]);monkeypatch.setattr(profit,'okx_page',reader)
    a,b=profit.day_range('2025-03-01','2025-03-31')
    assert len(asyncio.run(profit.collect('OKX',a,b,object())))==2
    assert reader.await_count==3
    reader.side_effect=[[okx('12345')],[okx('12345')]]
    with pytest.raises(ValueError,match='lặp'):asyncio.run(profit.collect('OKX',a,b,object()))


def test_outside_okx_retention_never_marks_empty_history_complete(monkeypatch):
    monkeypatch.setattr(profit,'okx_history_floor',lambda:datetime(2025,4,1,tzinfo=timezone.utc))
    reader=AsyncMock();monkeypatch.setattr(profit,'okx_page',reader)
    a,b=profit.day_range('2025-03-01','2025-03-31')
    with pytest.raises(ValueError,match='6 tháng'):asyncio.run(profit.collect('OKX',a,b,object()))
    reader.assert_not_awaited()


def test_collect_mexc_uses_only_read_endpoint_and_advances_cursor(monkeypatch):
    monkeypatch.setattr(profit.settings,'mexc_p2p_api_incoming_side','BUY');monkeypatch.setattr(profit.settings,'mexc_p2p_incoming_side','SELL')
    row={'advOrderNo':'d12345','side':'SELL','state':'DONE','coinName':'USDT','fiatUnit':'VND',
         'tradableQuantity':'100','amount':'2600000','createTime':'1740794400000','finishTime':'1740794460000'}
    api=SimpleNamespace(request=AsyncMock(side_effect=[[{**row,'side':'BUY'}],[],[row],[]]));a,b=profit.day_range('2025-03-01','2025-03-31')
    rows=asyncio.run(profit.collect('MEXC',a,b,api))
    assert len(rows)==1 and api.request.call_args_list[1].kwargs['params']['lastId']=='d12345'
    assert [c.args[0] for c in api.request.call_args_list]==['/api/v3/fiat/market/order/paginationV2']*2+['/api/v3/fiat/merchant/order/paginationV2']*2
    assert 'orderDealState' not in api.request.call_args_list[2].kwargs['params']


@pytest.mark.parametrize('case',['both_roles','cancelled_maker','conflicting_source'])
def test_mexc_full_history_matches_maker_membership_before_side_mapping(monkeypatch,case):
    monkeypatch.setattr(profit.settings,'mexc_p2p_api_incoming_side','BUY');monkeypatch.setattr(profit.settings,'mexc_p2p_incoming_side','SELL')
    row={'advOrderNo':'d12345','side':'BUY','state':'DONE','coinName':'USDT','fiatUnit':'VND',
         'tradableQuantity':'100','amount':'2600000','createTime':'1740794400000','finishTime':'1740794460000'}
    maker={**row}
    row={**row,'side':'SELL'}
    if case=='cancelled_maker':maker['state']='CANCEL'
    if case=='conflicting_source':maker['amount']='2600001'
    api=SimpleNamespace(request=AsyncMock(side_effect=[[row,{**row,'advOrderNo':'d12346','side':'BUY'}],[],[maker],[],{**row,'advOrderNo':'d12346','side':'BUY'}]))
    a,b=profit.day_range('2025-03-01','2025-03-31')
    if case=='conflicting_source':
        with pytest.raises(ValueError,match='chưa khớp'):asyncio.run(profit.collect('MEXC',a,b,api))
    else:
        rows=asyncio.run(profit.collect('MEXC',a,b,api))
        sides={r['code']:r['side'] for r in rows}
        assert sides['d12346']=='BUY'
        if case=='both_roles':assert sides['d12345']=='SELL'
        else:assert 'd12345' not in sides


def test_report_auth_opening_and_no_trading_mutation(client,monkeypatch):
    from app.models import OKXSlot,P2POrder,BankTransaction
    path='/dashboard/api/control/profit?from_day=2025-03-01&to_day=2025-03-31'
    assert client.get(path,headers={'X-Admin-Key':'wrong'}).status_code==401
    assert client.post('/dashboard/api/control/profit/sync',headers={'X-Admin-Key':'wrong'},json={'from_day':'2025-03-01','to_day':'2025-03-31'}).status_code==401
    assert client.put('/dashboard/api/control/profit/opening',headers={'X-Admin-Key':'wrong'},json={'day':'2025-03-01','quantity':'1','unit_cost':'25000'}).status_code==401
    with client.factory() as db:
        for t in [trade('1','BUY',10,250000,'2025-02-01T00:00:00+00:00'),trade('2','SELL',10,260000)]:
            db.add(profit.ProfitTrade(**vars(t)))
        db.commit()
    r=client.get(path);assert r.status_code==200 and r.headers['cache-control']=='no-store'
    assert Decimal(r.json()['totals']['profit'])==10000
    assert client.get(path+'&exchange=foreign').status_code==422
    assert client.get(path+'&page=0').status_code==422
    r=client.put('/dashboard/api/control/profit/opening',json={'day':'2025-03-01','quantity':'10','unit_cost':'24000'})
    assert r.status_code==200
    assert Decimal(client.get(path).json()['totals']['profit'])==20000
    with client.factory() as db:
        assert db.query(OKXSlot).filter_by(side='BUY').one().auto_enabled
        assert db.query(P2POrder).count()==0 and db.query(BankTransaction).count()==0


def test_sync_persists_completed_rows_once_and_preserves_other_state(client,monkeypatch):
    monkeypatch.setattr(profit,'SessionLocal',client.factory)
    monkeypatch.setattr(profit.settings,'okx_p2p_enabled',True);monkeypatch.setattr(profit.settings,'mexc_p2p_enabled',False)
    row=profit.normalize('OKX',okx())
    monkeypatch.setattr(profit,'collect',AsyncMock(return_value=[row]))
    for _ in range(2):
        r=client.post('/dashboard/api/control/profit/sync',json={'from_day':'2025-03-01','to_day':'2025-03-31'})
        assert r.status_code==200
        state=client.get('/dashboard/api/control/profit/sync/'+r.json()['id']).json()
        assert state['state']=='DONE' and state['results']['OKX']['count']==1
    with client.factory() as db:assert db.query(profit.ProfitTrade).count()==1
    monkeypatch.setattr(profit,'collect',AsyncMock(side_effect=RuntimeError('do-not-expose-api-secret')))
    r=client.post('/dashboard/api/control/profit/sync',json={'from_day':'2025-03-01','to_day':'2025-03-31'})
    state=client.get('/dashboard/api/control/profit/sync/'+r.json()['id'])
    assert 'do-not-expose' not in state.text
    with client.factory() as db:assert db.query(profit.ProfitTrade).count()==1


def test_history_read_retries_transport_and_api_rate_limit_without_writes(monkeypatch):
    import httpx
    from unittest.mock import Mock
    monkeypatch.setattr(profit.asyncio,'sleep',AsyncMock())
    limited=Mock(status_code=200);limited.json.return_value={'code':'50011'}
    good=Mock(status_code=200);good.json.return_value={'code':'0','data':[]}
    api=SimpleNamespace(get=AsyncMock(side_effect=[httpx.ReadTimeout('private-error'),limited,good]))
    assert asyncio.run(profit.okx_page(api,1))==[]
    assert api.get.await_count==3 and profit.asyncio.sleep.await_count==2


def test_superseded_history_job_cannot_overwrite_newer_snapshot(client,monkeypatch):
    from datetime import timedelta
    monkeypatch.setattr(profit,'SessionLocal',client.factory)
    monkeypatch.setattr(profit.settings,'okx_p2p_enabled',True);monkeypatch.setattr(profit.settings,'mexc_p2p_enabled',False)
    old=profit.ProfitSyncJob(id='old',state='RUNNING',start_day='2025-03-01',end_day='2025-03-31',
                            started_at=datetime.now(timezone.utc)-timedelta(minutes=11),result_json='{}')
    with client.factory() as db:db.add(old);db.commit()
    async def newer(*args):
        with client.factory() as db:
            db.add(profit.ProfitSyncJob(id='new',state='RUNNING',start_day='2025-03-01',end_day='2025-03-31',
                started_at=datetime.now(timezone.utc),result_json='{}'));db.commit()
        return [profit.normalize('OKX',okx())]
    monkeypatch.setattr(profit,'collect',newer)
    asyncio.run(profit.run_sync('old'))
    with client.factory() as db:
        assert db.get(profit.ProfitSyncJob,'old').state=='SUPERSEDED'
        assert db.query(profit.ProfitTrade).count()==0

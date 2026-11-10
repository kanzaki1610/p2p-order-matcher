import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app import ad_visibility as vis, mexc_ads, okx_release
from app.models import OKXSlot
from app.okx_ad_worker import OKXAdPriceAttempt
from app.mexc_pricing import MexcPriceSlot
from test_web_control import client
from test_mexc_pricing import ad


def ox(hidden=False,available='100'):
    return dict(adId='123',isOwner=True,side='buy',type='limit',status='new',isHidden=hidden,
        unitPrice='26000',availableAmount=available,onHoldAmount='0',minOrderSize='100000',maxOrderSize='2000000')


@pytest.mark.parametrize('exchange,row,on,can_show',[
    ('OKX',ox(),True,True),('OKX',ox(True),False,True),('OKX',ox(available='0'),False,False),
    ('OKX',dict(ox(),status='cancelled'),False,False),('MEXC',ad(),True,True),
    ('MEXC',ad(status='CLOSE'),False,True),('MEXC',ad(status='LOW_STOCK'),False,False),
    ('MEXC',dict(ad(),availableQuantity='0'),False,False)])
def test_lamps_depend_on_exchange_visibility_and_remaining_stock(exchange,row,on,can_show):
    result=vis.lamp(exchange,row)
    assert result['on'] is on and result['can_show'] is can_show


@pytest.mark.parametrize('row',[dict(ox(),isHidden=None),dict(ox(),availableAmount=None),dict(ox(),availableAmount='NaN')])
def test_missing_state_is_unknown_not_lit_or_off(row):
    with pytest.raises(ValueError):vis.lamp('OKX',row)


@pytest.fixture
def exchange_io(client,monkeypatch):
    @asynccontextmanager
    async def fake_api(exchange):yield object()
    monkeypatch.setattr(vis,'api_for',fake_api)
    monkeypatch.setattr(vis.settings,'mexc_p2p_live_writes',True)
    writer=AsyncMock(return_value={'adId':'123'})
    monkeypatch.setattr(okx_release,'request',writer)
    monkeypatch.setattr(mexc_ads,'write_visibility',writer)
    return writer


def token(client,exchange,row):
    with client.factory() as db:
        return vis.token_for(db,exchange,'BUY' if exchange=='OKX' else 'SELL','123' if exchange=='OKX' else 'a123',row)


def send(client,t,visible=True,ack=True):
    return client.post('/dashboard/api/control/visibility',json={'token':t,'visible':visible,'acknowledge':ack})


@pytest.mark.parametrize('exchange',['OKX','MEXC'])
@pytest.mark.parametrize('visible',[False,True])
def test_visibility_posts_once_with_readback_and_pauses_auto(client,exchange_io,monkeypatch,exchange,visible):
    before=ox(hidden=visible) if exchange=='OKX' else ad(status='CLOSE' if visible else 'OPEN')
    final=ox(hidden=not visible) if exchange=='OKX' else ad(status='OPEN' if visible else 'CLOSE')
    monkeypatch.setattr(vis,'read_owned',AsyncMock(side_effect=[before,before,final]))
    r=send(client,token(client,exchange,before),visible)
    assert r.status_code==200 and 'Đã xác minh' in r.json()['message']
    assert exchange_io.await_count==1
    if exchange=='OKX':assert exchange_io.call_args.args[3]=={'adId':'123','status':'show' if visible else 'hidden'}
    else:assert exchange_io.call_args.args[1:]==('a123',visible)
    with client.factory() as db:
        guard=db.get(vis.AdVisibilityGuard,exchange+':'+('123' if exchange=='OKX' else 'a123'))
        assert guard.state=='IDLE' and guard.revision!='0'
        if exchange=='OKX':assert not db.scalar(select(OKXSlot)).auto_enabled
        else:assert not db.get(MexcPriceSlot,'SELL:1').enabled


def test_post_requires_primary_auth_and_acknowledgement(client,exchange_io):
    t=token(client,'OKX',ox())
    assert send(client,t,ack=False).status_code==400
    assert client.post('/dashboard/api/control/visibility',headers={'X-Admin-Key':'wrong'},json={'token':t,'visible':False,'acknowledge':True}).status_code==401
    exchange_io.assert_not_awaited()


@pytest.mark.parametrize('error',['tamper','expired','stock_change','second_read_change','exhausted','pending_price'])
def test_stale_foreign_or_unavailable_input_never_posts(client,exchange_io,monkeypatch,error):
    before=ox(True,available='0' if error=='exhausted' else '100')
    t=token(client,'OKX',before)
    if error=='tamper':t=t[:-1]+('a' if t[-1]!='a' else 'b')
    if error=='expired':monkeypatch.setattr(vis.time,'time',lambda:999999999999)
    rows=[before,before]
    if error=='stock_change':rows[0]=dict(before,availableAmount='99')
    if error=='second_read_change':rows[1]=dict(before,availableAmount='99')
    if error=='pending_price':
        with client.factory() as db:
            slot=db.scalar(select(OKXSlot));db.add(OKXAdPriceAttempt(slot_id=slot.id,state='UNKNOWN'));db.commit()
    monkeypatch.setattr(vis,'read_owned',AsyncMock(side_effect=rows))
    assert send(client,t).status_code==400
    exchange_io.assert_not_awaited()


def test_timeout_is_durable_fences_new_tokens_and_check_never_reposts(client,exchange_io,monkeypatch):
    before=ox(True)
    read=AsyncMock(return_value=before);monkeypatch.setattr(vis,'read_owned',read)
    exchange_io.side_effect=TimeoutError('SECRET PROVIDER URL')
    t=token(client,'OKX',before)
    r=send(client,t)
    assert r.status_code==200 and 'Chưa xác minh' in r.json()['message'] and 'SECRET' not in r.text
    with client.factory() as db:
        assert db.get(vis.AdVisibilityGuard,'OKX:123').state=='UNKNOWN'
        with pytest.raises(ValueError):vis.assert_clear(db,'OKX','123',lock=True)
    assert send(client,token(client,'OKX',before)).status_code==400
    r=client.post('/dashboard/api/control/visibility-check/OKX/BUY/123')
    assert 'Chưa khớp' in r.json()['message']
    read.return_value=ox(False)
    r=client.post('/dashboard/api/control/visibility-check/OKX/BUY/123')
    assert 'Đã xác minh' in r.json()['message']
    assert exchange_io.await_count==1


def test_replay_after_success_does_not_resend_even_if_status_was_changed_back(client,exchange_io,monkeypatch):
    before=ox(True);t=token(client,'OKX',before)
    monkeypatch.setattr(vis,'read_owned',AsyncMock(side_effect=[before,before,ox(False),before]))
    assert send(client,t).status_code==200
    assert send(client,t).status_code==400
    assert exchange_io.await_count==1


@pytest.mark.parametrize('exchange',['OKX','MEXC'])
def test_hidden_or_exhausted_ad_turns_auto_off_without_exchange_write(client,exchange_io,exchange):
    with client.factory() as db:
        slot=db.scalar(select(OKXSlot)) if exchange=='OKX' else db.get(MexcPriceSlot,'SELL:1')
        if exchange=='MEXC':slot.enabled=True;slot.paused_by_floor=True
        assert vis.stop_unavailable(db,exchange,slot,ox(True) if exchange=='OKX' else ad(status='CLOSE'))
        assert not (slot.auto_enabled if exchange=='OKX' else slot.enabled)
    exchange_io.assert_not_awaited()


def test_hidden_auto_stop_does_not_change_secondary_account(client,monkeypatch):
    monkeypatch.setattr(vis.settings,'database_schema','kayhap')
    with client.factory() as db:
        slot=db.scalar(select(OKXSlot))
        assert not vis.stop_unavailable(db,'OKX',slot,ox(True))
        assert slot.auto_enabled


def test_list_lights_hidden_empty_and_unknown_and_never_exposes_bank_data(client,exchange_io,monkeypatch):
    rows=[ad(),dict(ad(status='CLOSE'),advNo='a222'),dict(ad(),advNo='a333',availableQuantity='0'),dict(ad(),advNo='a444',availableQuantity=None)]
    rows[0]['paymentInfo']=[{'accountNo':'PRIVATE-ACCOUNT'}]
    monkeypatch.setattr(vis,'owned_list',AsyncMock(return_value=rows))
    r=client.get('/dashboard/api/control/advertisements/MEXC/SELL')
    assert r.status_code==200
    cards={x['ad_id']:x for x in r.json()['rows']}
    assert cards['a123']['on'] is True
    assert cards['a222']['on'] is False and cards['a222']['can_show']
    assert cards['a333']['on'] is False and not cards['a333']['can_show']
    assert cards['a444']['on'] is None and cards['a444']['token'] is None
    assert 'PRIVATE-ACCOUNT' not in r.text
    exchange_io.assert_not_awaited()


def test_okx_list_does_not_assume_active_list_means_visible(client,exchange_io,monkeypatch):
    raw=ox();raw.pop('isHidden')
    monkeypatch.setattr(vis,'owned_list',AsyncMock(return_value=[raw]))
    read=AsyncMock(return_value=ox(True));monkeypatch.setattr(vis,'read_owned',read)
    r=client.get('/dashboard/api/control/advertisements/OKX/BUY')
    assert r.status_code==200 and r.json()['rows'][0]['on'] is False
    read.assert_awaited_once()


@pytest.mark.parametrize('exchange',['OKX','MEXC'])
@pytest.mark.parametrize('exhausted',[False,True])
def test_real_worker_stops_before_planning_or_writing(client,exchange_io,monkeypatch,exchange,exhausted):
    from app import okx_ad_worker as okx_worker,mexc_pricing as mexc_worker
    worker=okx_worker if exchange=='OKX' else mexc_worker
    monkeypatch.setattr(worker,'valid_webhook',lambda _:True)
    source=ox(hidden=not exhausted,available='0' if exhausted else '100') if exchange=='OKX' else dict(ad(status='OPEN' if exhausted else 'CLOSE'),availableQuantity='0' if exhausted else '100')
    monkeypatch.setattr(worker,'read_ad',AsyncMock(return_value=source))
    plan=AsyncMock();monkeypatch.setattr(worker,'read_rank_plan' if exchange=='OKX' else 'rank_plan',plan)
    with client.factory() as db:
        slot=db.scalar(select(OKXSlot)) if exchange=='OKX' else db.get(MexcPriceSlot,'SELL:1')
        if exchange=='MEXC':slot.enabled=True;slot.paused_by_floor=True;db.commit()
        asyncio.run(okx_worker.process_ad_pricing(db,None) if exchange=='OKX' else mexc_worker.process_pricing(db,None,None))
        db.refresh(slot)
        assert not (slot.auto_enabled if exchange=='OKX' else slot.enabled)
    plan.assert_not_awaited();exchange_io.assert_not_awaited()

import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.database import Base
from app.models import OKXSlot
from app.mexc_pricing import MexcPriceSlot, rank_plan
from app.nickname_target import save, target, clear, plan_options
from app.nickname_offers import fingerprint, open_picker, interact, OfferPicker
from app.okx_rank import plan_rank_price
from app import nickname_offers as ui


def rows():
    return [dict(nick='Dgiaodich30s', price=p, min=m, max=500000000,
                 payMethod='1', payMethodCodes=['1'])
            for p, m in ((25988, 8686868), (26339, 9869866))]


def board(values, exchange='okx'):
    return dict(ok=True, exchange=exchange, side='sell', panel=exchange+'_sell',
                saved_ts=time.time(), age_sec=0, cache='fresh', rows=values)


@pytest.fixture
def db():
    engine=create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for n in (1, 2):
            session.add(OKXSlot(side='SELL', slot_number=n, adv_id=str(n), auto_enabled=False))
            session.add(MexcPriceSlot(key=f'SELL:{n}', ad=f'a{n}', nickname='own', enabled=False))
        session.commit()
        yield session
    engine.dispose()


def test_identity_ignores_price_rank_max_but_not_terms():
    a, b = rows()
    assert fingerprint(a) != fingerprint(b)
    assert fingerprint(a) == fingerprint({**a, 'price':25000, 'max':400000000})
    assert fingerprint(a) != fingerprint({**a, 'min':123})
    assert fingerprint(a) != fingerprint({**a, 'payMethodCodes':['2']})
    assert fingerprint({'nick':'x','price':1}) is None


def test_follow_exact_row_across_price_and_order_change():
    a, b=rows(); selector=fingerprint(a)
    a={**a, 'price':26001, 'max':1}
    result=plan_rank_price(board([b,a]), 'SELL', target_nickname='Dgiaodich30s',
        target_offer=selector, price_offset=-1, price_bound=25600, enforce_cost=False)
    assert result['proposed_price']=='26000'
    for changed in ([b], [a,a], [{**a, 'min':123},b]):
        assert plan_rank_price(board(changed), 'SELL', target_nickname='Dgiaodich30s',
            target_offer=selector, enforce_cost=False)['action']=='BLOCKED'
    stale=board([a,b]);stale['saved_ts']-=60
    assert plan_rank_price(stale,'SELL',target_nickname='Dgiaodich30s',target_offer=selector,enforce_cost=False)['action']=='BLOCKED'


def button(panel, index=0, n=1):
    return panel['components'][index]['components'][n]['custom_id']


def click(code):
    return {'data':{'custom_id':code}}


@pytest.mark.parametrize('exchange',['OKX','MEXC'])
def test_select_green_persists_and_leaves_auto_untouched(db,monkeypatch,exchange):
    values=rows()
    monkeypatch.setattr(ui,'read_offers',AsyncMock(return_value=values))
    save(db,exchange,'SELL',{'slot':'1','target':'Dgiaodich30s','offset':'-1','bound':'25600'},'owner')
    panel=asyncio.run(open_picker(db,exchange,'SELL','1','owner'))
    selected=asyncio.run(interact(click(button(panel)),db,'owner',exchange))
    assert selected['components'][0]['components'][1]['style']==3
    follow=target(db,exchange,'SELL',1)
    assert follow[4]==fingerprint(values[0]) and follow[1]==-1 and follow[3]==25600
    assert target(db,exchange,'SELL',2) is None
    assert not db.scalar(select(OKXSlot).where(OKXSlot.slot_number==1)).auto_enabled
    assert not db.get(MexcPriceSlot,'SELL:1').enabled
    # Replayed or outdated buttons cannot overwrite the selection.
    with pytest.raises(ValueError):asyncio.run(interact(click(button(panel,1)),db,'owner',exchange))
    if exchange=='MEXC':
        source=AsyncMock(); source.get.return_value.json=lambda: board([values[1],{**values[0],'price':26001}], 'mexc')
        source.get.return_value.raise_for_status=lambda: None
        result=asyncio.run(rank_plan(source,db.get(MexcPriceSlot,'SELL:1'),None,follow,enforce_cost=False))
        assert result['proposed_price']=='26000'
    save(db,exchange,'SELL',{'slot':'1','target':'Dgiaodich30s','offset':'2'},'owner')
    assert target(db,exchange,'SELL',1)[4]==fingerprint(values[0])
    save(db,exchange,'SELL',{'slot':'1','target':'other'},'owner')
    assert 'target_offer' in plan_options(target(db,exchange,'SELL',1))
    assert plan_options(target(db,exchange,'SELL',1))['target_offer'] is None
    clear(db,exchange,'SELL',1);db.commit()
    assert target(db,exchange,'SELL',1) is None


@pytest.mark.parametrize('failure',['owner','exchange','expire','change','collision','missing'])
def test_selection_fails_closed(db,monkeypatch,failure):
    values=rows()
    read=AsyncMock(return_value=values)
    monkeypatch.setattr(ui,'read_offers',read)
    save(db,'OKX','SELL',{'slot':'1','target':'Dgiaodich30s'},'owner')
    panel=asyncio.run(open_picker(db,'OKX','SELL',1,'owner'))
    if failure=='expire':
        db.query(OfferPicker).update({'expires':0});db.commit()
    if failure=='change':save(db,'OKX','SELL',{'slot':'1','target':'changed'},'owner')
    if failure=='collision':read.return_value=[values[0],values[0]]
    if failure=='missing':read.return_value=[values[1]]
    with pytest.raises(ValueError):
        asyncio.run(interact(click(button(panel)),db,'other' if failure=='owner' else 'owner', 'MEXC' if failure=='exchange' else 'OKX'))
    assert len(target(db,'OKX','SELL',1))==4


def test_pagination_and_duplicate_buttons_disabled(db,monkeypatch):
    values=rows()*3
    monkeypatch.setattr(ui,'read_offers',AsyncMock(return_value=values))
    save(db,'OKX','SELL',{'slot':'1','target':'Dgiaodich30s'},'owner')
    panel=asyncio.run(open_picker(db,'OKX','SELL',1,'owner'))
    assert len(panel['components'])==5
    assert panel['components'][0]['components'][1]['disabled']
    code=panel['components'][-1]['components'][-1]['custom_id']
    second=asyncio.run(interact(click(code),db,'owner','OKX'))
    assert '2/2' in second['content']


@pytest.mark.parametrize('exchange',['OKX','MEXC'])
def test_automatic_picker_posts_once_without_exchange_writes(db,monkeypatch,exchange):
    from types import SimpleNamespace
    from app.config import settings
    from app.nickname_offers import publish_ambiguity
    monkeypatch.setattr(settings,'discord_bot_token','test-token')
    monkeypatch.setattr(settings,'discord_allowed_user_ids','123')
    monkeypatch.setattr(settings,'discord_command_channel_ids','456')
    monkeypatch.setattr(settings,'discord_mexc_pricing_channel_id','789')
    save(db,exchange,'SELL',{'slot':'1','target':'Dgiaodich30s'},'123')
    follow=target(db,exchange,'SELL',1)
    client=AsyncMock();client.post.return_value=SimpleNamespace(is_success=True)
    factory=AsyncMock();factory.__aenter__.return_value=client
    monkeypatch.setattr(ui.httpx,'AsyncClient',lambda **kw:factory)
    plan={'nickname_offer_choices':rows()}
    assert asyncio.run(publish_ambiguity(db,exchange,'SELL',1,follow,plan))
    assert not asyncio.run(publish_ambiguity(db,exchange,'SELL',1,follow,plan))
    assert client.post.await_count==1
    assert client.post.call_args.args[0].endswith(('/789/messages' if exchange=='MEXC' else '/456/messages'))
    outgoing=client.post.call_args.kwargs['json']
    assert len(outgoing['components'][0]['components'])==3
    assert target(db,exchange,'SELL',1)==follow

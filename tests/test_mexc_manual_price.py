import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app import mexc_manual_price as manual
from app import mexc_price_panel as panel
from app.mexc_pricing import MexcPriceSlot, cost_settings


@pytest.fixture
def setup(monkeypatch):
    engine=create_engine('sqlite://');Base.metadata.create_all(engine)
    monkeypatch.setattr(manual.settings,'database_schema','public')
    monkeypatch.setattr(manual.settings,'mexc_ad_pricing_enabled',True)
    with Session(engine,expire_on_commit=False) as db:
        slot=MexcPriceSlot(key='SELL:1',ad='a123',enabled=True,rank=10,nickname='Own')
        db.add(slot);cost_settings(db).cost=Decimal('26050');db.commit()
        row=dict(advNo='a123',side='SELL',advStatus='CLOSE',coinName='USDT',coinId='usdt',fiatUnit='VND',
            price='26080',quantity='100',payTimeLimit=15,minSingleTransAmount='100000',maxSingleTransAmount='2000000',
            userAllTradeCountMin=0,userAllTradeCountMax=0,paymentInfo=[{'id':77}],payMethod='1',tradeTerms='unchanged')
        async def read(*args):return dict(row)
        monkeypatch.setattr(manual,'read_ad',read)
        api=AsyncMock()
        api.__aenter__.return_value=api
        async def write(path,params,method):
            assert method=='POST'
            row['price']=params['price']
            row['quantity']=str(Decimal(row['quantity'])+Decimal(params.get('supplyQuantity','0'))-Decimal(params.get('reduceQuantity','0')))
            for key in ('minSingleTransAmount','maxSingleTransAmount','advStatus'):row[key]=params[key]
            return 'a123'
        api._request_once=AsyncMock(side_effect=write)
        monkeypatch.setattr(manual,'MexcP2PClient',lambda:api)
        yield db,slot,row,api
    engine.dispose()


def preview(setup,price='26070',terms=None):
    db,slot,row,api=setup
    result=asyncio.run(manual.preview(db,'owner','SELL',1,Decimal(price),terms or {}))
    return db.query(manual.MexcManualPriceDraft).one(),result


def test_preview_never_writes_or_disables_auto(setup):
    db,slot,row,api=setup
    draft,result=preview(setup)
    api._request_once.assert_not_awaited()
    assert slot.enabled and 'Xác nhận' in result['content']


@pytest.mark.parametrize('quantity,delta_key,delta',[('120','supplyQuantity','20'),('80','reduceQuantity','20')])
def test_manual_update_keeps_closed_state_and_confirms_terms_once(setup,quantity,delta_key,delta):
    db,slot,row,api=setup
    draft,_=preview(setup,terms={'quantity':quantity,'minimum':'120000','maximum':'1500000'})
    result=asyncio.run(manual.confirm(db,'owner',draft.id))
    assert 'Đã xác minh' in result and slot.state=='IDLE' and not slot.enabled
    body=api._request_once.call_args.kwargs['params']
    assert body[delta_key]==delta and body['initQuantity']=='100'
    assert body['advStatus']=='CLOSE' and body['payMethod']=='77' and body['tradeTerms']=='unchanged'
    assert row['quantity']==quantity and row['minSingleTransAmount']=='120000'
    with pytest.raises(ValueError):asyncio.run(manual.confirm(db,'owner',draft.id))
    api._request_once.assert_awaited_once()


@pytest.mark.parametrize('case',['owner','expiry','snapshot','unknown','ad','floor'])
def test_invalid_or_stale_confirmation_never_posts(setup,case):
    db,slot,row,api=setup;draft,_=preview(setup);owner='owner'
    if case=='owner':owner='other'
    if case=='expiry':draft.expires=1
    if case=='snapshot':row['price']='26090'
    if case=='unknown':slot.state='UNKNOWN'
    if case=='ad':slot.ad='a999'
    if case=='floor':cost_settings(db).cost=Decimal('27000')
    db.commit()
    with pytest.raises(ValueError):asyncio.run(manual.confirm(db,owner,draft.id))
    api._request_once.assert_not_awaited()


def test_timeout_retains_unknown_and_stops_resubmission(setup):
    db,slot,row,api=setup;draft,_=preview(setup)
    api._request_once=AsyncMock(side_effect=httpx.ReadTimeout('secret signed URL'))
    result=asyncio.run(manual.confirm(db,'owner',draft.id))
    assert slot.state=='UNKNOWN' and not slot.enabled and 'secret signed URL' not in result
    with pytest.raises(ValueError):asyncio.run(manual.confirm(db,'owner',draft.id))
    api._request_once.assert_awaited_once()


def test_cancel_draft_does_not_send(setup):
    db,slot,row,api=setup;draft,_=preview(setup)
    asyncio.run(panel.execute_panel({'type':3,'data':{'custom_id':'mprice:cancel:'+draft.id}},db,'owner'))
    with pytest.raises(ValueError):asyncio.run(manual.confirm(db,'owner',draft.id))
    api._request_once.assert_not_awaited()


def test_manual_modals_fit_discord_limits_and_have_separate_buy_sell_buttons():
    labels=[button['label'] for row in panel.panel()['components'] for button in row['components']]
    assert {'Đặt giá Mua','Đặt giá Bán'}<=set(labels)
    for side in ('BUY','SELL'):
        fields=panel.modal('price',side)['data']['components']
        assert len(fields)==5
        assert all(len(row['components'][0]['label'])<=45 for row in fields)

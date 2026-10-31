import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock
from types import SimpleNamespace
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.database import Base
from app.models import OKXSlot, OKXAuditLog
from app import okx_buy_budget as b
from app import okx_ad_worker as w

@pytest.fixture
def db():
    engine=create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(OKXSlot(side='BUY',slot_number=1,adv_id='200',auto_enabled=True))
        db.add(OKXAuditLog(action='OKX_AD_PRICE_CONFIRMED',actor='test',detail='BUY 100 -> 200; action=UPDATE_PRICE; price=26495; hidden=True'))
        db.commit()
        yield db
    engine.dispose()

def source():
    return dict(adId='200',side='buy',type='limit',status='new',isHidden=True,unitPrice='26495',
        availableAmount='6000',completedOrderCryptoAmount='0',completedOrderFiatAmount='0',onHoldAmount='0',
        minOrderSize='20000000',maxOrderSize='158532000')

def order(state='completed'):
    return dict(orderId='900',adId='100',side='buy',orderStatus=state,paymentStatus='confirmed',
        cryptoCurrency='USDT',fiatCurrency='VND',cryptoAmount='5000',fiatAmount='132475000',createdTimestamp='1793400000000')

def slot(db): return db.scalar(select(OKXSlot))
def prepare(db, ad=None, price='26495'):
    return asyncio.run(b.prepare_buy(db,None,slot(db),ad or source(),Decimal(price)))

def test_legacy_replacements_exact_budget_and_restart(db,monkeypatch):
    reader=AsyncMock(return_value=[order()]);monkeypatch.setattr(b,'read_order_page',reader)
    result=prepare(db)
    assert result['terms']=={'cryptoAmount':'983.46861673','minOrderLimit':'20000000','maxOrderLimit':'26057000'}
    assert result['left']=='26057000'
    db.add(b.OKXPendingBuyBudget(slot_id=slot(db).id,root_ad_id='100',old_ad_id='200',quantity=1000));db.commit()
    b.bind_result(db,slot(db).id,'200','300');slot(db).adv_id='300';db.commit();db.expire_all()
    final=source();final.update(adId='300',availableAmount='1000',maxOrderSize='26057000')
    again=prepare(db,final,'27000')
    assert Decimal(again['terms']['cryptoAmount']) < 1000
    assert again['terms']['maxOrderLimit']=='26057000'
    assert len(db.scalars(select(b.OKXBuyBudgetOrder)).all())==1

@pytest.mark.parametrize('state',['new','cancelled'])
def test_pending_blocks_and_cancelled_does_not_spend(db,monkeypatch,state):
    monkeypatch.setattr(b,'read_order_page',AsyncMock(return_value=[order(state)]))
    if state=='new':
        with pytest.raises(b.BuyBudgetError,match='đang chờ'): prepare(db)
    else:
        assert prepare(db)['terms']['maxOrderLimit']=='158532000'

@pytest.mark.parametrize('field,value',[('completedOrderFiatAmount','1'),('onHoldAmount','1'),('availableAmount',None)])
def test_inconsistent_source_never_plans_write(db,monkeypatch,field,value):
    monkeypatch.setattr(b,'read_order_page',AsyncMock(return_value=[order()]))
    ad=source();ad[field]=value
    with pytest.raises(b.BuyBudgetError): prepare(db,ad)

def test_duplicate_history_page_rejected(db,monkeypatch):
    monkeypatch.setattr(b,'read_order_page',AsyncMock(return_value=[order(),order()]))
    with pytest.raises(b.BuyBudgetError,match='trùng'):prepare(db)

def test_changed_completed_amount_rejected(db,monkeypatch):
    monkeypatch.setattr(b,'read_order_page',AsyncMock(return_value=[order()]))
    prepare(db)
    db.get(b.OKXBuyBudget,'100').scanned_at=None;db.commit()
    changed=order();changed['fiatAmount']='132475001'
    monkeypatch.setattr(b,'read_order_page',AsyncMock(return_value=[changed]))
    with pytest.raises(b.BuyBudgetError,match='bất thường'):prepare(db)

def test_exhausted_never_creates_negative_limits(db,monkeypatch):
    full=order();full.update(cryptoAmount='6000',fiatAmount='158532000')
    monkeypatch.setattr(b,'read_order_page',AsyncMock(return_value=[full]))
    result=prepare(db)
    assert result['exhausted'] and result['terms']=={}

@pytest.mark.parametrize('quantity,accepted',[('6000',False),('1000',True)])
def test_readback_must_not_refill_quantity(db,quantity,accepted):
    budget=b.budget_for(db,slot(db),source())
    db.add(b.OKXPendingBuyBudget(slot_id=slot(db).id,root_ad_id=budget.root_ad_id,old_ad_id='200',quantity=1000));db.commit()
    ad=source();ad['availableAmount']=quantity
    assert b.pending_matches(db,slot(db).id,SimpleNamespace(action='UPDATE_PRICE',old_ad_id='200'),ad)==accepted

def test_worker_sends_remaining_not_initial_quantity_and_keeps_hidden(db,monkeypatch):
    from app.okx_cost import save_cost
    monkeypatch.setattr(w.settings,'okx_ad_pricing_enabled',True)
    monkeypatch.setattr(w.settings,'database_schema','public')
    monkeypatch.setattr(w,'valid_webhook',lambda _:True)
    monkeypatch.setattr(w,'send_discord_message',AsyncMock(return_value=True))
    monkeypatch.setattr(b,'read_order_page',AsyncMock(return_value=[order()]))
    final=source();final.update(adId='300',unitPrice='26475',availableAmount='984.21155808',maxOrderSize='26057000')
    monkeypatch.setattr(w,'read_ad',AsyncMock(side_effect=[source(),source(),final]))
    monkeypatch.setattr(w,'read_rank_plan',AsyncMock(return_value={'action':'UPDATE_PRICE','proposed_price':'26475'}))
    write=AsyncMock(return_value={'oldAdId':'200','newAdId':'300'});monkeypatch.setattr(w,'request',write)
    asyncio.run(w.process_ad_pricing(db,None))
    body=write.call_args.args[3]
    assert Decimal(body['cryptoAmount']) < 1000 and body['maxOrderLimit']=='26057000'
    assert 'online' not in body and slot(db).adv_id=='300'
    assert db.get(w.OKXAdPriceAttempt,slot(db).id).state=='IDLE'

def test_worker_does_not_post_if_order_arrives_between_reads(db,monkeypatch):
    monkeypatch.setattr(w.settings,'okx_ad_pricing_enabled',True)
    monkeypatch.setattr(w.settings,'database_schema','public')
    monkeypatch.setattr(w,'valid_webhook',lambda _:True)
    monkeypatch.setattr(b,'read_order_page',AsyncMock(return_value=[order()]))
    changed=source();changed['onHoldAmount']='100'
    monkeypatch.setattr(w,'read_ad',AsyncMock(side_effect=[source(),changed]))
    monkeypatch.setattr(w,'read_rank_plan',AsyncMock(return_value={'action':'UPDATE_PRICE','proposed_price':'26475'}))
    write=AsyncMock();monkeypatch.setattr(w,'request',write)
    asyncio.run(w.process_ad_pricing(db,None))
    write.assert_not_awaited()
    assert db.get(w.OKXAdPriceAttempt,slot(db).id).state=='IDLE'

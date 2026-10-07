import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import settings
from app.database import Base
from app.models import P2POrder, BankTransaction, PaymentMatch
from app.mexc import MexcP2PClient
from app.mexc_release import MexcOrderReceipt, MexcReleaseAttempt, process_mexc_releases


@pytest.fixture
def setup(monkeypatch):
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        order = P2POrder(order_code='MEXC001', fiat_amount=Decimal('100000'),
            crypto_amount=Decimal('4'), counterparty_name='NGUYEN VAN BA', status='PAYMENT_DETECTED')
        tx = BankTransaction(bank='MB', transaction_id='TEST', amount=Decimal('100000'),
            description='BA VAN NGUYEN', occurred_at=datetime.now(timezone.utc))
        db.add_all([order, tx, MexcOrderReceipt(order_code='MEXC001', api_side='SELL')])
        db.commit()
        db.add(PaymentMatch(order_id=order.id, transaction_id=tx.id, score=100, decision='AUTO_MATCHED', reasons='test'))
        db.commit()
        monkeypatch.setattr(settings,'mexc_auto_release_enabled',True)
        monkeypatch.setattr(settings,'mexc_p2p_live_writes',True)
        monkeypatch.setattr('app.mexc_release.notify_event',AsyncMock(return_value=True))
        row = dict(advOrderNo='MEXC001', side='SELL', coinName='USDT',fiatUnit='VND', amount='100000',
            tradableQuantity='4', userInfo={'realName':'BA NGUYEN VAN'},state='PROCESSING',
            complained=False,blockUser=False,confirmPaymentInfo={'bankName':'MBBank'})
        yield db,order,tx,row
    engine.dispose()


def run(db, handler):
    async def execute():
        async with MexcP2PClient(api_key='test',api_secret='test',transport=httpx.MockTransport(handler)) as client:
            await process_mexc_releases(db,client)
    asyncio.run(execute())


def test_success_query_post_confirm_done_once(setup):
    db,order,tx,row=setup
    calls=[]
    def handler(request):
        calls.append(request.method)
        if request.method=='POST':
            assert request.url.path=='/api/v3/fiat/release_coin'
            assert parse_qs(request.url.query.decode())['advOrderNo']==['MEXC001']
            assert not request.content
            row['state']='DONE'
            return httpx.Response(200,json={'code':0,'data':None})
        return httpx.Response(200,json={'code':0,'data':row})
    run(db,handler);run(db,handler)
    assert calls.count('POST')==1
    assert order.status=='RELEASED'


@pytest.mark.parametrize('field,value',[('amount','99999'),('tradableQuantity','5'),('complained',True),
    ('blockUser',None),('coinName','BTC'),('fiatUnit','USD'),('side','BUY'),('state','CANCEL'),
    ('confirmPaymentInfo',{'bankName':'ACB'}),('userInfo',{'realName':'NGUYEN BA'})])
def test_unsafe_detail_never_posts(setup,field,value):
    db,order,tx,row=setup;row[field]=value
    def handler(request):
        assert request.method=='GET'
        return httpx.Response(200,json={'code':0,'data':row})
    run(db,handler)
    assert db.get(MexcReleaseAttempt,order.id).state=='REVIEW_REQUIRED'


@pytest.mark.parametrize('failure',['timeout','500','bad_json','missing_code'])
def test_uncertainty_never_resubmits(setup,failure):
    db,order,tx,row=setup;posts=[]
    def handler(request):
        if request.method=='POST':
            posts.append(request)
            if failure=='timeout':raise httpx.ReadTimeout('test')
            if failure=='500':return httpx.Response(500,json={'code':501})
            if failure=='bad_json':return httpx.Response(200,text='bad')
            return httpx.Response(200,json={'data':None})
        return httpx.Response(200,json={'code':0,'data':row})
    run(db,handler);run(db,handler)
    assert len(posts)==1
    assert db.get(MexcReleaseAttempt,order.id).state=='UNKNOWN'


def test_wait_for_buyer_then_release(setup):
    db,order,tx,row=setup;row['state']='NOT_PAID';posts=[]
    def handler(request):
        if request.method=='POST':
            posts.append(request);row['state']='DONE'
            return httpx.Response(200,json={'code':0,'data':None})
        return httpx.Response(200,json={'code':0,'data':row})
    run(db,handler)
    assert not posts
    assert db.get(MexcReleaseAttempt,order.id).state=='WAITING_BUYER_PAYMENT'
    row['state']='PROCESSING';run(db,handler)
    assert len(posts)==1 and order.status=='RELEASED'


@pytest.mark.parametrize('flag',['mexc_auto_release_enabled','mexc_p2p_live_writes'])
def test_disabled_no_network(setup,monkeypatch,flag):
    db,order,tx,row=setup;monkeypatch.setattr(settings,flag,False)
    run(db,lambda request:pytest.fail('Disabled release called API'))
    assert db.get(MexcReleaseAttempt,order.id) is None


def test_code_only_memo_supported(setup):
    db,order,tx,row=setup;tx.description='MEXC001';db.commit()
    def handler(request):
        if request.method=='POST':
            row['state']='DONE';return httpx.Response(200,json={'code':0,'data':None})
        return httpx.Response(200,json={'code':0,'data':row})
    run(db,handler)
    assert order.status=='RELEASED'


def test_no_receipt_no_release(setup):
    db,order,tx,row=setup;db.delete(db.get(MexcOrderReceipt,order.order_code));db.commit()
    run(db,lambda request:pytest.fail('Missing receipt called API'))


def test_wrong_payment_never_calls_api(setup):
    db,order,tx,row=setup;tx.amount=Decimal('99999');db.commit()
    run(db,lambda request:pytest.fail('Invalid payment called API'))


def test_acknowledgement_does_not_mark_released(setup):
    db,order,tx,row=setup;posts=[]
    def handler(request):
        if request.method=='POST':
            posts.append(request);return httpx.Response(200,json={'code':0,'data':None})
        return httpx.Response(200,json={'code':0,'data':row})
    run(db,handler);run(db,handler)
    assert len(posts)==1 and order.status=='PAYMENT_DETECTED'
    assert db.get(MexcReleaseAttempt,order.id).state=='SUBMITTED'


def test_unknown_can_reconcile_done_without_post(setup):
    db,order,tx,row=setup
    db.add(MexcReleaseAttempt(order_id=order.id,transaction_id=tx.id,state='UNKNOWN'))
    db.commit();row['state']='DONE'
    def handler(request):
        assert request.method=='GET'
        return httpx.Response(200,json={'code':0,'data':row})
    run(db,handler)
    assert order.status=='RELEASED'


def test_okx_source_collision_never_calls_mexc(setup):
    from app.okx_sync import OKXOrderReceipt
    db,order,tx,row=setup
    db.add(OKXOrderReceipt(order_id=order.order_code));db.commit()
    run(db,lambda request:pytest.fail('Cross-exchange receipt called MEXC'))


def test_buy_api_side_requires_paid(setup):
    db,order,tx,row=setup
    db.get(MexcOrderReceipt,order.order_code).api_side='BUY';db.commit()
    row.update(side='BUY',state='PAID')
    def handler(request):
        if request.method=='POST':
            row['state']='DONE';return httpx.Response(200,json={'code':0,'data':None})
        return httpx.Response(200,json={'code':0,'data':row})
    run(db,handler)
    assert order.status=='RELEASED'

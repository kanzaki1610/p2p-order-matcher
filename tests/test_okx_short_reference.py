import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.database import Base
from app.models import P2POrder, BankTransaction
from app.matching import match_transaction, okx_short_reference, payment_for_order
from app.okx_sync import OKXOrderReceipt, upsert, refresh_inactive_orders
from app.mexc_release import MexcOrderReceipt

@pytest.fixture
def db():
    engine=create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine,expire_on_commit=False) as value:
        yield value
    engine.dispose()


def add_order(db,code,amount=350000,exchange='OKX'):
    order=P2POrder(order_code=code,side='SELL',fiat_amount=Decimal(amount),crypto_amount=Decimal(14),counterparty_name='NGUYỄN TIẾN TOÀN',status='WAITING_PAYMENT',created_at=datetime.now(timezone.utc))
    db.add(order)
    if exchange=='OKX':db.add(OKXOrderReceipt(order_id=code))
    else:db.add(MexcOrderReceipt(order_code=code,api_side='BUY'))
    db.commit()
    return order


def tx(db,memo='NGUYEN TIEN TOAN chuyen tien 24613-GD-34EC297971-141026-20:52:19'):
    value=BankTransaction(bank='ACB',transaction_id='TEST3452',amount=Decimal(350000),direction='CREDIT',description=memo,occurred_at=datetime.now(timezone.utc))
    db.add(value);db.commit();return value


def row(order,state):
    return dict(orderId=order.order_code,side='sell',orderStatus=state,paymentStatus='paid',cryptoCurrency='USDT',fiatCurrency='VND',fiatAmount=str(order.fiat_amount),cryptoAmount=str(order.crypto_amount),createdTimestamp=str(int(order.created_at.timestamp()*1000)),counterpartyDetail={'realName':order.counterparty_name})


def test_exact_user_case_cancelled_excluded(db):
    old=add_order(db,'261014214507148',500000)
    new=add_order(db,'261014215124613')
    upsert(db,row(old,'cancelled'))
    credit=tx(db)
    assert old.status=='CANCELLED'
    assert match_transaction(db,credit)[1].id==new.id
    assert credit.status=='AUTO_MATCHED'
    assert okx_short_reference(new,credit)
    assert payment_for_order(db,new,credit).amount==Decimal(350000)


def test_short_reference_disambiguates_even_before_old_cancellation_sync(db):
    old=add_order(db,'261014214507148',500000)
    new=add_order(db,'261014215124613')
    credit=tx(db)
    assert match_transaction(db,credit)[1].id==new.id
    assert old.status=='WAITING_PAYMENT' and new.status=='PAYMENT_DETECTED'


@pytest.mark.parametrize('memo',['NGUYEN TIEN TOAN 124613','NGUYEN TIEN TOAN 246130','24613','OTHER PERSON 24613'])
def test_suffix_boundaries_and_full_name_are_required(db,memo):
    add_order(db,'261014214507148',350000)
    target=add_order(db,'261014215124613')
    credit=tx(db,memo)
    assert not okx_short_reference(target,credit)
    assert match_transaction(db,credit)[0]!='AUTO_MATCHED'


def test_same_suffix_is_still_ambiguous(db):
    add_order(db,'261014210024613')
    add_order(db,'261014215124613')
    assert match_transaction(db,tx(db))[0]=='REVIEW_REQUIRED'


def test_mexc_short_reference_does_not_disambiguate(db):
    add_order(db,'261014214507148',350000,'MEXC')
    target=add_order(db,'261014215124613',350000,'MEXC')
    credit=tx(db)
    assert not okx_short_reference(target,credit)
    assert match_transaction(db,credit)[0]=='REVIEW_REQUIRED'


def test_missing_pending_order_verified_cancelled_by_detail(db,monkeypatch):
    old=add_order(db,'261014214507148',500000)
    detail=AsyncMock(return_value=row(old,'cancelled'))
    monkeypatch.setattr('app.okx_release.detail',detail)
    asyncio.run(refresh_inactive_orders(db,object(),set()))
    assert old.status=='CANCELLED'
    assert detail.await_count==1


def test_api_failure_does_not_guess_cancellation(db,monkeypatch):
    order=add_order(db,'261014214507148')
    monkeypatch.setattr('app.okx_release.detail',AsyncMock(side_effect=RuntimeError('test')))
    asyncio.run(refresh_inactive_orders(db,object(),set()))
    assert order.status=='WAITING_PAYMENT'


def test_wrong_detail_does_not_cancel_order(db,monkeypatch):
    order=add_order(db,'261014214507148')
    value=row(order,'cancelled');value['fiatAmount']='1'
    monkeypatch.setattr('app.okx_release.detail',AsyncMock(return_value=value))
    asyncio.run(refresh_inactive_orders(db,object(),set()))
    assert order.status=='WAITING_PAYMENT'


@pytest.mark.parametrize('status',['CANCELLED','CONFIRMED','RELEASED'])
def test_terminal_reference_cannot_be_reused_for_future_same_name_order(db,status):
    old=add_order(db,'261014215124613');old.status=status;db.commit()
    new=add_order(db,'261014219999999')
    decision,_,_,reasons=match_transaction(db,tx(db))
    assert decision=='REVIEW_REQUIRED'
    assert new.status=='WAITING_PAYMENT'
    assert 'Không chuyển khoản tiền này sang lệnh mới' in reasons[0]

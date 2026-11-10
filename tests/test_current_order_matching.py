"""Only currently actionable orders participate in identity ambiguity checks."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import Base
from app.matching import match_transaction
from app.models import BankTransaction, P2POrder, PaymentAllocation
from app import mexc_sync, okx_sync
from app.mexc_release import MexcOrderReceipt


@pytest.fixture
def db():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def make_order(db, code, amount, **kwargs):
    order = P2POrder(order_code=code, side='SELL', fiat_amount=Decimal(amount),
        crypto_amount=Decimal(amount) / 26000, counterparty_name='NGUYEN VAN A',
        created_at=datetime.now(timezone.utc), status='WAITING_PAYMENT', **kwargs)
    db.add(order); db.commit()
    return order


def transfer(db, memo='NGUYEN VAN A chuyen tien'):
    tx = BankTransaction(transaction_id='BANK-1', bank='ACB', amount=156000000,
        direction='CREDIT', description=memo, occurred_at=datetime.now(timezone.utc))
    db.add(tx); db.commit()
    return tx


@pytest.mark.parametrize('state', ['CANCELLED', 'CONFIRMED', 'RELEASED', 'REJECTED'])
def test_terminal_5000_does_not_block_active_6000(db, state):
    old = make_order(db, 'OLD-5000', 130000000); old.status=state; db.commit()
    current = make_order(db, 'NEW-6000', 156000000)
    tx = transfer(db)
    assert match_transaction(db, tx)[:2] == ('AUTO_MATCHED', current)
    assert db.get(PaymentAllocation, tx.id).order_id == current.id


def test_cancelled_reference_is_not_reassigned(db):
    old=make_order(db, 'OLD-5000', 130000000);old.status='CANCELLED';db.commit()
    current=make_order(db, 'NEW-6000', 156000000)
    tx=transfer(db, 'NGUYEN VAN A OLD-5000')
    assert match_transaction(db, tx)[0] == 'REVIEW_REQUIRED'
    assert current.status == 'WAITING_PAYMENT'
    assert db.get(PaymentAllocation, tx.id) is None


def test_expired_unpaid_order_does_not_block_current(db):
    make_order(db, 'OLD-5000', 130000000, expires_at=datetime.now(timezone.utc)-timedelta(seconds=1))
    current=make_order(db, 'NEW-6000', 156000000)
    assert match_transaction(db, transfer(db))[:2] == ('AUTO_MATCHED', current)


def test_two_active_orders_still_require_review_then_cancel_resolves(db):
    old=make_order(db, 'OLD-5000', 130000000)
    current=make_order(db, 'NEW-6000', 156000000);tx=transfer(db)
    assert match_transaction(db, tx)[0] == 'REVIEW_REQUIRED'
    old.status='CANCELLED';db.commit()
    assert match_transaction(db, tx)[:2] == ('AUTO_MATCHED', current)
    assert match_transaction(db, tx)[1] == current
    assert db.query(PaymentAllocation).count() == 1


def test_okx_authenticated_cancel_removes_stale_candidate(db, monkeypatch):
    old=make_order(db, 'OLD-5000', 130000000)
    current=make_order(db, 'NEW-6000', 156000000);tx=transfer(db)
    db.add(okx_sync.OKXOrderReceipt(order_id=old.order_code));db.commit()
    row=dict(orderId=old.order_code, side='sell', cryptoCurrency='USDT', fiatCurrency='VND',
        fiatAmount=str(old.fiat_amount), cryptoAmount=str(old.crypto_amount),
        counterpartyDetail={'realName':old.counterparty_name}, orderStatus='cancelled')
    monkeypatch.setattr('app.okx_release.detail', AsyncMock(return_value=row))
    asyncio.run(okx_sync.refresh_inactive_orders(db, object(), set()))
    assert old.status == 'CANCELLED'
    assert match_transaction(db, tx)[:2] == ('AUTO_MATCHED',current)


@pytest.mark.parametrize('payment', ['paid','unreceived'])
def test_okx_paid_order_not_expired_by_old_countdown(db, payment):
    row=dict(orderId='PAID-1',side='sell',orderStatus='new',paymentStatus=payment,
        fiatAmount='156000000',cryptoAmount='6000',createdTimestamp=str(int(datetime.now(timezone.utc).timestamp()*1000)),
        expiryTimeInSeconds=str(int((datetime.now(timezone.utc)-timedelta(minutes=5)).timestamp())),
        counterpartyDetail={'realName':'NGUYEN VAN A'})
    order,_=okx_sync.upsert(db,row)
    assert order.expires_at is None
    assert match_transaction(db,transfer(db))[:2] == ('AUTO_MATCHED',order)


def test_mexc_missing_cancelled_order_refreshed_before_reconciliation(db,monkeypatch):
    old=make_order(db,'OLD-5000',130000000);current=make_order(db,'NEW-6000',156000000)
    for order in (old,current):db.add(MexcOrderReceipt(order_code=order.order_code,api_side='SELL'))
    db.commit();tx=transfer(db)
    assert match_transaction(db,tx)[0]=='REVIEW_REQUIRED'
    def detail(order,state):
        return dict(advOrderNo=order.order_code,side='SELL',state=state,coinName='USDT',fiatUnit='VND',
            amount=str(order.fiat_amount),tradableQuantity=str(order.crypto_amount),
            createTime=int(order.created_at.timestamp()*1000),userInfo={'realName':order.counterparty_name})
    client=AsyncMock()
    client.get_orders.return_value=[{'advOrderNo':current.order_code}]
    client.get_order_detail.side_effect=lambda code:detail(old,'CANCEL') if code==old.order_code else detail(current,'PAID')
    monkeypatch.setattr(settings,'mexc_p2p_enabled',True)
    monkeypatch.setattr(settings,'mexc_p2p_incoming_side','SELL')
    monkeypatch.setattr(settings,'mexc_p2p_api_incoming_side','SELL')
    monkeypatch.setattr(mexc_sync,'notify_match',AsyncMock(return_value=True))
    result=asyncio.run(mexc_sync.sync_mexc_orders(db,client))
    assert old.status=='CANCELLED'
    assert current.status=='PAYMENT_DETECTED'
    assert result['transactions_reconciled']==1
    assert db.get(PaymentAllocation,tx.id).order_id==current.id


def test_mexc_cancel_does_not_overwrite_released_order(db):
    order=make_order(db,'OLD-5000',130000000);order.status='RELEASED';db.commit()
    mexc_sync._upsert_order(db,{'advOrderNo':order.order_code,'state':'CANCEL'})
    assert order.status=='RELEASED'


def test_expired_reference_is_not_reassigned(db):
    make_order(db, 'OLD-5000', 130000000, expires_at=datetime.now(timezone.utc)-timedelta(seconds=1))
    current=make_order(db, 'NEW-6000', 156000000)
    assert match_transaction(db, transfer(db, 'NGUYEN VAN A OLD-5000'))[0] == 'REVIEW_REQUIRED'
    assert current.status == 'WAITING_PAYMENT'


def test_only_expired_order_stays_unmatched(db):
    old=make_order(db,'OLD-5000',130000000,expires_at=datetime.now(timezone.utc)-timedelta(seconds=1))
    assert match_transaction(db,transfer(db))[0]=='UNMATCHED'
    assert old.status=='WAITING_PAYMENT'


def test_allocated_partial_credit_is_not_reused_after_cancel(db):
    old=make_order(db,'OLD-5000',200000000);tx=transfer(db)
    assert match_transaction(db,tx)[0]=='PARTIAL_PAYMENT'
    old.status='CANCELLED';db.commit()
    current=make_order(db,'NEW-6000',156000000)
    assert match_transaction(db,tx)[0]=='REVIEW_REQUIRED'
    assert current.status=='WAITING_PAYMENT'
    assert db.get(PaymentAllocation,tx.id).order_id==old.id

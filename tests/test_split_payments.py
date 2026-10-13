import asyncio
import json
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.database import Base
from app.models import P2POrder,BankTransaction,PaymentMatch,PaymentAllocation
from app.matching import match_transaction,payment_for_order
from app.config import settings
from app import notifications,okx_release,mexc_release
from app.okx_sync import OKXOrderReceipt
from app.mexc import MexcP2PClient

@pytest.fixture
def db(monkeypatch):
    engine=create_engine('sqlite://');Base.metadata.create_all(engine)
    monkeypatch.setattr(notifications,'notify_event',AsyncMock(return_value=True))
    monkeypatch.setattr(okx_release,'notify_event',AsyncMock(return_value=True))
    monkeypatch.setattr(mexc_release,'notify_event',AsyncMock(return_value=True))
    with Session(engine,expire_on_commit=False) as session:yield session
    engine.dispose()

def order(db,code='ORDER13M'):
    value=P2POrder(side='SELL',order_code=code,fiat_amount=Decimal(13000000),crypto_amount=Decimal(500),counterparty_name='NGUYEN VAN BA',expected_bank='MB',status='WAITING_PAYMENT',created_at=datetime.now(timezone.utc))
    db.add(value);db.commit();return value

def transfer(db,amount,memo='ORDER13M',code=None,bank='MB'):
    value=BankTransaction(bank=bank,transaction_id=code or 'TX'+str(db.query(BankTransaction).count()+1),amount=Decimal(amount),description=memo,direction='CREDIT',occurred_at=datetime.now(timezone.utc))
    db.add(value);db.commit();return value

def test_two_transfers_total_13m_match_once_and_cannot_be_reused(db):
    o=order(db);first=transfer(db,1300000)
    assert match_transaction(db,first)[0]=='PARTIAL_PAYMENT'
    assert o.status=='WAITING_PAYMENT'
    assert match_transaction(db,first)[0]=='PARTIAL_PAYMENT'
    assert db.query(PaymentAllocation).count()==1
    second=transfer(db,11700000)
    assert match_transaction(db,second)[0]=='AUTO_MATCHED'
    assert o.status=='PAYMENT_DETECTED'
    assert payment_for_order(db,o,second).amount==Decimal(13000000)
    assert db.query(PaymentMatch).filter_by(decision='AUTO_MATCHED').count()==1
    other=order(db,'SECOND13M')
    match_transaction(db,first);match_transaction(db,second)
    assert other.status=='WAITING_PAYMENT'
    assert db.query(PaymentAllocation).count()==2

def test_name_permutation_and_overpayment_can_accumulate(db):
    o=order(db);first=transfer(db,1300000,'BA NGUYEN VAN')
    assert match_transaction(db,first)[0]=='PARTIAL_PAYMENT'
    second=transfer(db,11700001,'NGUYEN BA VAN')
    assert match_transaction(db,second)[0]=='AUTO_MATCHED'
    assert payment_for_order(db,o,second).amount==Decimal(13000001)

def test_partial_same_buyer_multiple_orders_is_ambiguous(db):
    order(db);order(db,'SECOND13M');tx=transfer(db,1300000,'BA VAN NGUYEN')
    assert match_transaction(db,tx)[0]=='REVIEW_REQUIRED'
    assert db.query(PaymentAllocation).count()==0
    assert db.query(PaymentMatch).filter_by(decision='AUTO_MATCHED').count()==0

def test_full_code_disambiguates_same_buyer(db):
    o=order(db);other=order(db,'SECOND13M');tx=transfer(db,1300000,'BA VAN NGUYEN ORDER13M')
    assert match_transaction(db,tx)[1].id==o.id
    assert db.get(PaymentAllocation,tx.id).order_id==o.id
    assert other.status=='WAITING_PAYMENT'

def test_unknown_identity_not_added_to_valid_later_transfer(db):
    o=order(db);first=transfer(db,1300000,'chuyen tien')
    assert match_transaction(db,first)[0]=='UNMATCHED'
    second=transfer(db,11700000)
    assert match_transaction(db,second)[0]=='PARTIAL_PAYMENT'
    assert payment_for_order(db,o,second).amount==Decimal(11700000)

def test_changed_identity_or_bank_invalidates_release_evidence(db):
    o=order(db);first=transfer(db,1300000);match_transaction(db,first)
    second=transfer(db,11700000);match_transaction(db,second)
    first.description='khong co ma hay ten';db.commit()
    assert payment_for_order(db,o,second).amount==0

def test_wrong_bank_cannot_complete_partial_order(db):
    o=order(db);first=transfer(db,1300000);match_transaction(db,first)
    second=transfer(db,11700000,bank='ACB')
    assert match_transaction(db,second)[0]!='AUTO_MATCHED'
    assert db.query(PaymentAllocation).count()==1
    assert o.status=='WAITING_PAYMENT'

def test_disabled_by_threshold_does_not_become_auto(db,monkeypatch):
    o=order(db);monkeypatch.setattr(settings,'auto_match_threshold',101)
    tx=transfer(db,13000000)
    assert match_transaction(db,tx)[0]=='REVIEW_REQUIRED'
    assert o.status=='WAITING_PAYMENT'

@pytest.mark.parametrize('exchange',['OKX','MEXC'])
def test_split_payment_releases_once_only_after_total_and_safe_exchange_status(db,monkeypatch,exchange):
    o=order(db);first=transfer(db,1300000);match_transaction(db,first)
    monkeypatch.setattr(settings,'okx_auto_release_enabled',True)
    monkeypatch.setattr(settings,'mexc_auto_release_enabled',True)
    monkeypatch.setattr(settings,'mexc_p2p_live_writes',True)
    if exchange=='OKX':db.add(OKXOrderReceipt(order_id=o.order_code))
    else:db.add(mexc_release.MexcOrderReceipt(order_code=o.order_code,api_side='SELL'))
    db.commit();posts=[]
    okxrow=dict(orderId=o.order_code,side='sell',fiatAmount='13000000',cryptoAmount='500',cryptoCurrency='USDT',fiatCurrency='VND',orderStatus='new',paymentStatus='paid',isFrozen=False,disputeStatus='0',counterpartyDetail={'realName':'BA VAN NGUYEN'})
    mexcrow=dict(advOrderNo=o.order_code,side='SELL',coinName='USDT',fiatUnit='VND',amount='13000000',tradableQuantity='500',userInfo={'realName':'BA NGUYEN VAN'},state='PROCESSING',complained=False,blockUser=False,confirmPaymentInfo={'bankName':'MB'})
    def handler(request):
        if request.method=='POST':
            posts.append(request.url.path)
            if exchange=='OKX':
                assert json.loads(request.content)['amount']=='13000000'
                okxrow.update(orderStatus='completed',paymentStatus='confirmed')
                return httpx.Response(200,json={'code':'0','data':[{'orderId':o.order_code}]})
            mexcrow['state']='DONE';return httpx.Response(200,json={'code':0,'data':None})
        return httpx.Response(200,json={'code':0,'data':[okxrow] if exchange=='OKX' else mexcrow})
    async def run():
        if exchange=='OKX':
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:await okx_release.process_releases(db,client)
        else:
            async with MexcP2PClient(api_key='fake',api_secret='fake',transport=httpx.MockTransport(handler)) as client:await mexc_release.process_mexc_releases(db,client)
    asyncio.run(run());assert posts==[]
    second=transfer(db,11700000);match_transaction(db,second)
    asyncio.run(run());asyncio.run(run())
    assert len(posts)==1 and o.status=='RELEASED'

def test_review_notice_no_repeat_and_no_score_or_false_name_mismatch(db,monkeypatch):
    o=order(db);tx=transfer(db,13000000,'ORDER13M')
    sender=AsyncMock(return_value=True);monkeypatch.setattr(notifications,'notify_event',sender)
    reasons=['Không auto: kiểm tra ngân hàng.','Số tiền khớp +50','Ngân hàng không trả tên người chuyển']
    asyncio.run(notifications.notify_match('REVIEW_REQUIRED',o,tx,50,reasons))
    asyncio.run(notifications.notify_match('REVIEW_REQUIRED',o,tx,50,reasons))
    assert sender.await_count==1
    text=sender.call_args.args[1]
    assert 'đúng mã lệnh' in text and 'Điểm khớp' not in text and '+50' not in text and 'KHÔNG KHỚP' not in text

def test_completed_order_suppresses_stale_manual_review(db,monkeypatch):
    o=order(db);tx=transfer(db,1300000);o.status='RELEASED';db.commit()
    sender=AsyncMock(return_value=True);monkeypatch.setattr(notifications,'notify_event',sender)
    asyncio.run(notifications.notify_match('REVIEW_REQUIRED',o,tx,50,['Không auto: lỗi cũ']))
    sender.assert_not_called()

def test_release_warning_same_state_reason_not_repeated_after_transition(db,monkeypatch):
    o=order(db);tx=transfer(db,13000000)
    attempt=okx_release.OKXReleaseAttempt(order_id=o.id,transaction_id=tx.id,state='UNKNOWN')
    db.add(attempt);db.commit();sender=AsyncMock(return_value=True);monkeypatch.setattr(okx_release,'notify_event',sender)
    asyncio.run(okx_release.notify_attempt(db,attempt,o,'Lỗi kiểm thử'))
    attempt.notified_state=None;db.commit()
    asyncio.run(okx_release.notify_attempt(db,attempt,o,'Lỗi kiểm thử'))
    assert sender.await_count==1

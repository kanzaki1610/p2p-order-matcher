import asyncio
import copy
from dataclasses import asdict
from decimal import Decimal
from io import BytesIO
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app import buy_payment_qr as worker
from app import buy_payment_data as data
from app import vietqr


@pytest.fixture
def db(monkeypatch):
    engine=create_engine('sqlite://')
    Base.metadata.create_all(engine)
    monkeypatch.setattr(worker.settings,'database_schema','public')
    monkeypatch.setattr(worker.settings,'buy_payment_qr_enabled',True)
    monkeypatch.setattr(worker.settings,'discord_buy_payment_channel_id','123456789012345678')
    monkeypatch.setattr(worker.settings,'discord_guild_id','223456789012345678')
    monkeypatch.setattr(worker.settings,'discord_application_id','323456789012345678')
    monkeypatch.setattr(worker.settings,'discord_allowed_user_ids','423456789012345678')
    with Session(engine,expire_on_commit=False) as session:yield session
    engine.dispose()


def okx():
    return dict(orderId='261109123456789',side='buy',cryptoCurrency='USDT',fiatCurrency='VND',
        fiatAmount='50000000',orderStatus='new',paymentStatus='unpaid',isFrozen=False,disputeStatus='0',
        counterpartyDetail={'userId':'seller1','realName':'NGUYEN VAN A','takerPaymentMethod':{
            'type':'BANK','currency':'VND','isDisabled':False,'accountName':'NGUYEN VAN A',
            'bankName':'MBBank','bankCode':'MB','accountNo':'00123456789'}})


def mexc():
    return dict(advOrderNo='d123456789',advNo='a987654321',side='SELL',coinName='USDT',fiatUnit='VND',
        state='NOT_PAID',complained=False,amount='50000000',payTimeLimit=int((time.time()+600)*1000),
        userInfo={'realName':'NGUYEN VAN A'},paymentInfo=[{'id':77,'payMethod':1,
            'bankName':'MBBank','account':'00123456789','payee':'NGUYEN VAN A'}])


def info():return data.okx_instruction(okx(),'261109123456789')


def test_local_qr_round_trip_and_no_remote_image_url():
    item=info()
    text=vietqr.payload(item.bank_bin,item.account,item.amount,item.note)
    assert text.startswith('000201010212') and '00123456789' in text and '540850000000' in text
    from PIL import Image
    zxing=pytest.importorskip('zxingcpp')
    assert zxing.read_barcode(Image.open(BytesIO(vietqr.png_bytes(text)))).text==text
    body=worker.message_body(item)
    assert body['embeds'][0]['image']['url']=='attachment://payment.png'
    assert body['allowed_mentions']=={'parse':[]}
    assert not body.get('components')


@pytest.mark.parametrize('bank',['MB','MBBank','970422','Ngân hàng TMCP Quân đội'])
def test_exact_bank_aliases(bank):assert vietqr.resolve_bank(bank)['bin']=='970422'


def test_ambiguous_bank_fields_block():
    with pytest.raises(ValueError):vietqr.resolve_bank('MB','ACB')


@pytest.mark.parametrize('value',['12***678','123 456','123-456','',123456,None])
def test_never_guess_account(value):
    with pytest.raises(ValueError):vietqr.account_number(value)


@pytest.mark.parametrize('value',['0','-1','NaN','Infinity','1.5','1000000000000',None])
def test_invalid_amount(value):
    with pytest.raises(ValueError):vietqr.integer_amount(value)


@pytest.mark.parametrize('change',[{'side':'sell'},{'fiatCurrency':'USD'},{'cryptoCurrency':'BTC'},
    {'orderId':'other'},{'isFrozen':True},{'isFrozen':None},{'disputeStatus':'1'},
    {'paymentStatus':'unknown'},{'orderStatus':'unknown'}])
def test_okx_rejects_unverified_or_wrong_order(change):
    row={**okx(),**change}
    with pytest.raises(ValueError):data.okx_instruction(row,'261109123456789')


@pytest.mark.parametrize('change',[{'orderStatus':'cancelled'},{'orderStatus':'completed'},
    {'paymentStatus':'paid'},{'paymentStatus':'unreceived'},{'paymentStatus':'confirmed'}])
def test_okx_terminal_or_paid_removes_qr(change):
    with pytest.raises(data.InactiveOrder):data.okx_instruction({**okx(),**change},'261109123456789')


def test_okx_never_falls_back_to_own_payment_account():
    row=okx();row['makerPaymentMethod']={'accountNo':'99999999'}
    row['counterpartyDetail']['takerPaymentMethod']=None
    with pytest.raises(ValueError):data.okx_instruction(row,'261109123456789')


@pytest.mark.parametrize('change',[{'userId':'other'},{'realName':'TRAN VAN B'}])
def test_okx_extra_counterparty_must_match(change):
    row=okx();party={**row['counterpartyDetail'],**change}
    with pytest.raises(ValueError):data.okx_instruction(row,row['orderId'],party)


def test_mexc_receiving_account_and_selected_method():
    row=mexc();item=data.mexc_instruction(row,'d123456789','SELL')
    assert item.account=='00123456789' and item.note=='123456789'
    row['confirmPaymentInfo']=dict(row['paymentInfo'][0])
    assert data.mexc_instruction(row,'d123456789','SELL')==item
    row['confirmPaymentInfo']['account']='9999999'
    with pytest.raises(ValueError):data.mexc_instruction(row,'d123456789','SELL')


def test_mexc_does_not_choose_first_of_multiple_accounts():
    row=mexc();row['paymentInfo'].append({**row['paymentInfo'][0],'id':78,'account':'9999999'})
    with pytest.raises(ValueError):data.mexc_instruction(row,'d123456789','SELL')


@pytest.mark.parametrize('change',[{'side':'BUY'},{'fiatUnit':'USD'},{'complained':True},
    {'state':'unknown'},{'payTimeLimit':None},{'paymentInfo':[]}])
def test_mexc_wrong_side_or_incomplete_blocks(change):
    with pytest.raises(ValueError):data.mexc_instruction({**mexc(),**change},'d123456789','SELL')


@pytest.mark.parametrize('state',['CANCEL','CANCELLED','DONE','PAID','PROCESSING','WAIT_PROCESS','TIMEOUT'])
def test_mexc_terminal(state):
    with pytest.raises(data.InactiveOrder):data.mexc_instruction({**mexc(),'state':state},'d123456789','SELL')


def test_expired_mexc_is_inactive():
    with pytest.raises(data.InactiveOrder):data.mexc_instruction({**mexc(),'payTimeLimit':int((time.time()-1)*1000)},'d123456789','SELL')


def record(db):
    row=worker.BuyPaymentQR(key='OKX:261109123456789',exchange='OKX',code='261109123456789',
        channel=worker.settings.discord_buy_payment_channel_id,created=time.time())
    db.add(row);db.commit();return row


def test_same_order_is_not_notified_twice(db,monkeypatch):
    row=record(db);send=SimpleNamespace(send=AsyncMock(return_value=True))
    monkeypatch.setattr(worker,'read_payment',AsyncMock(return_value=info()))
    asyncio.run(worker.process(db,None,None,send,row))
    row.message='555';db.commit()
    asyncio.run(worker.process(db,None,None,send,row))
    assert send.send.await_count==1


@pytest.mark.parametrize('error',[data.InactiveOrder('Lệnh đã hủy'),ValueError('Tài khoản đã đổi'),RuntimeError('SECRET_KEY=private')])
def test_inactive_changed_or_unreadable_order_clears_existing_image(db,monkeypatch,error):
    row=record(db);row.message='555';db.commit()
    send=SimpleNamespace(send=AsyncMock(return_value=True))
    monkeypatch.setattr(worker,'read_payment',AsyncMock(side_effect=error))
    asyncio.run(worker.process(db,None,None,send,row))
    body=send.send.call_args.args[2]
    assert body['attachments']==[] and body['embeds']==[]
    assert 'SECRET_KEY' not in body['content']
    assert row.terminal is isinstance(error,data.InactiveOrder)


def test_state_changes_between_reads_never_send_payable_qr(db,monkeypatch):
    row=record(db)
    monkeypatch.setattr(worker,'read_payment',AsyncMock(side_effect=[info(),data.InactiveOrder('Đã hủy')]))
    send=SimpleNamespace(send=AsyncMock(return_value=True))
    asyncio.run(worker.process(db,None,None,send,row))
    send.send.assert_not_awaited()
    assert row.terminal


def test_feature_cannot_run_for_kayhap(db,monkeypatch):
    assert worker.enabled()
    monkeypatch.setattr(worker.settings,'database_schema','kayhap')
    assert not worker.enabled()


def test_secondary_package_cannot_activate_even_with_flag(db,monkeypatch):
    monkeypatch.setattr(worker,'__package__','app_kayhap')
    assert not worker.enabled()


def test_okx_missing_method_uses_only_authenticated_counterparty_get(db,monkeypatch):
    from app import okx_release
    row=okx();party=copy.deepcopy(row['counterpartyDetail'])
    row['counterpartyDetail']['takerPaymentMethod']=None
    monkeypatch.setattr(okx_release,'detail',AsyncMock(return_value=row))
    request=AsyncMock(return_value=party);monkeypatch.setattr(okx_release,'request',request)
    result=asyncio.run(worker.read_payment(None,None,record(db)))
    assert result.account=='00123456789'
    assert request.call_args.args[1]=='GET'
    assert '/counterparty-user-info?' in request.call_args.args[2]


def test_mexc_owned_ad_and_order_binding_are_checked_before_qr(db,monkeypatch):
    from app import mexc_ads
    monkeypatch.setattr(worker.settings,'mexc_p2p_api_incoming_side','BUY')
    ad_read=AsyncMock(return_value={'side':'BUY'});monkeypatch.setattr(mexc_ads,'read_ad',ad_read)
    api=SimpleNamespace(request=AsyncMock(return_value=mexc()))
    row=worker.BuyPaymentQR(exchange='MEXC',code='d123456789')
    assert asyncio.run(worker.read_payment(None,api,row)).bank=='MBBank'
    ad_read.assert_awaited_once_with(api,'a987654321','BUY')
    assert api.request.call_args.args[0]=='/api/v3/fiat/order/detail'
    ad_read.side_effect=ValueError('Not owned')
    with pytest.raises(ValueError):asyncio.run(worker.read_payment(None,api,row))


def test_discovery_is_separate_from_sell_matching_and_deduplicated(db,monkeypatch):
    from app import okx_sync
    monkeypatch.setattr(worker.settings,'okx_p2p_enabled',True)
    monkeypatch.setattr(worker.settings,'mexc_p2p_enabled',True)
    monkeypatch.setattr(worker.settings,'mexc_p2p_api_incoming_side','BUY')
    row=okx();row['createdTimestamp']=int(time.time()*1000)
    okx_read=AsyncMock(return_value=[row,{**row,'orderId':'261109999999999','side':'sell'}])
    monkeypatch.setattr(okx_sync,'read_orders',okx_read)
    m=mexc();m['createTime']=int(time.time()*1000)
    api=SimpleNamespace(request=AsyncMock(return_value=[m,{**m,'side':'BUY','advOrderNo':'d111111111'}]))
    asyncio.run(worker.discover(db,None,api));asyncio.run(worker.discover(db,None,api))
    rows=db.query(worker.BuyPaymentQR).all()
    assert {x.key for x in rows}=={'OKX:261109123456789','MEXC:d123456789'}
    assert api.request.call_args.kwargs['params']['side']=='SELL'
    # One exchange reader failing must not prevent the other from discovering.
    okx_read.side_effect=RuntimeError('offline')
    api.request.return_value=[{**m,'advOrderNo':'d222222222'}]
    asyncio.run(worker.discover(db,None,api))
    assert db.get(worker.BuyPaymentQR,'MEXC:d222222222') is not None


def test_discord_timeout_is_reconciled_not_reposted(db):
    row=record(db);calls=[]
    async def handle(request):
        calls.append(request.method)
        if request.method=='POST':raise httpx.ReadTimeout('private',request=request)
        return httpx.Response(200,json=[])
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            delivery=worker.DiscordDelivery(client)
            assert not await delivery.send(db,row,{'content':'test'})
            assert row.delivery_unknown
            assert not await delivery.send(db,row,{'content':'test'})
    asyncio.run(run())
    assert calls==['POST','GET']


@pytest.mark.parametrize('public,outsider,wrong_guild',[(True,False,False),(False,True,False),(False,False,True),(False,False,False)])
def test_private_channel_binding(db,public,outsider,wrong_guild):
    async def handle(request):
        if request.url.path.endswith('/roles'):
            return httpx.Response(200,json=[{'id':'botrole','tags':{'bot_id':worker.settings.discord_application_id}}])
        overwrites=[{'id':worker.settings.discord_guild_id,'deny':'0' if public else '1024','allow':'0'},
                    {'id':'botrole','allow':'1024','deny':'0'}]
        if outsider:overwrites.append({'id':'stranger','allow':'1024','deny':'0'})
        return httpx.Response(200,json={'type':0,'guild_id':'wrong' if wrong_guild else worker.settings.discord_guild_id,
                                        'permission_overwrites':overwrites})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            await worker.DiscordDelivery(client).validate(worker.settings.discord_buy_payment_channel_id)
    if public or outsider or wrong_guild:
        with pytest.raises(ValueError):asyncio.run(run())
    else:asyncio.run(run())

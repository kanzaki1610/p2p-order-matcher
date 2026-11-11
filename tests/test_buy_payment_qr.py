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


def test_okx_real_lowercase_currency_codes_keep_valid_payment_instruction():
    row=okx();row.update(cryptoCurrency='usdt',fiatCurrency='vnd')
    row['counterpartyDetail']['takerPaymentMethod'].update(type='bank',currency='vnd')
    result=data.okx_instruction(row,row['orderId'])
    assert result.exchange=='OKX' and result.account=='00123456789'
    row['paymentStatus']='paid'
    with pytest.raises(data.InactiveOrder):data.okx_instruction(row,row['orderId'])


def test_discover_accepts_lowercase_okx_symbols_without_guessing_other_currencies(db,monkeypatch):
    from app import okx_sync
    monkeypatch.setattr(worker.settings,'okx_p2p_enabled',True)
    monkeypatch.setattr(worker.settings,'mexc_p2p_enabled',False)
    row=dict(okx(),cryptoCurrency='usdt',fiatCurrency='vnd',createdTimestamp=str(int(time.time()*1000)))
    monkeypatch.setattr(okx_sync,'read_orders',AsyncMock(return_value=[row,dict(row,orderId='261109999999999',cryptoCurrency='btc')]))
    asyncio.run(worker.discover(db,None,None))
    assert db.get(worker.BuyPaymentQR,'OKX:'+row['orderId']) is not None
    assert db.get(worker.BuyPaymentQR,'OKX:261109999999999') is None


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


@pytest.mark.parametrize('exchange',['OKX','MEXC'])
def test_payment_qr_omits_memo_template_but_keeps_bank_account_and_amount(exchange):
    item=info() if exchange=='OKX' else data.mexc_instruction(mexc(),'d123456789','SELL')
    assert item.note==''
    text=vietqr.payload(item.bank_bin,item.account,item.amount)
    fields={};offset=0
    while offset<len(text):
        tag=text[offset:offset+2];size=int(text[offset+2:offset+4]);offset+=4
        fields[tag]=text[offset:offset+size];offset+=size
    assert '62' not in fields
    assert fields['54']==item.amount and item.account in fields['38'] and item.bank_bin in fields['38']
    assert item.code not in text
    body=worker.message_body(item)
    assert not any('Nội dung' in x['name'] for x in body['embeds'][0]['fields'])
    assert any(x['name']=='Lệnh' and x['value']==item.code for x in body['embeds'][0]['fields'])


def test_existing_unpaid_qr_with_old_order_memo_is_updated_in_same_message(db,monkeypatch):
    from dataclasses import replace
    row=record(db);row.message='555'
    row.fingerprint=worker.fingerprint(asdict(replace(info(),note=info().code)))
    db.commit()
    send=SimpleNamespace(send=AsyncMock(return_value=True))
    monkeypatch.setattr(worker,'read_payment',AsyncMock(return_value=info()))
    asyncio.run(worker.process(db,None,None,send,row))
    assert send.send.await_count==1 and row.message=='555'
    assert row.fingerprint==worker.fingerprint(asdict(info()))
    zxingcpp=pytest.importorskip('zxingcpp')
    from PIL import Image
    decoded=zxingcpp.read_barcode(Image.open(BytesIO(send.send.call_args.args[3]))).text
    assert info().code not in decoded


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
    assert item.account=='00123456789' and item.note==''
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


@pytest.mark.parametrize('state',['CANCEL','CANCELLED','DONE','PAID','PROCESSING','TIMEOUT'])
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


@pytest.mark.parametrize('state',['NOT_PAID','WAIT_PROCESS'])
def test_mexc_verified_buy_waiting_for_payment_supports_qr(state):
    row={**mexc(),'state':state}
    result=data.mexc_instruction(row,'d123456789','SELL')
    assert result.exchange=='MEXC' and result.amount=='50000000' and result.note==''
    assert result.account=='00123456789'


def test_mexc_wait_process_wrong_side_is_not_payable():
    row={**mexc(),'state':'WAIT_PROCESS','side':'BUY'}
    with pytest.raises(ValueError):data.mexc_instruction(row,'d123456789','BUY')


def test_mexc_discovery_includes_wait_process_but_not_paid_or_wrong_side(db,monkeypatch):
    monkeypatch.setattr(worker.settings,'okx_p2p_enabled',False)
    monkeypatch.setattr(worker.settings,'mexc_p2p_enabled',True)
    monkeypatch.setattr(worker.settings,'mexc_p2p_api_incoming_side','BUY')
    row={**mexc(),'state':'WAIT_PROCESS','createTime':int(time.time()*1000)}
    api=SimpleNamespace(request=AsyncMock(return_value=[row,
        {**row,'advOrderNo':'d222222222','state':'PROCESSING'},
        {**row,'advOrderNo':'d333333333','side':'BUY'}]))
    asyncio.run(worker.discover(db,None,api))
    assert [x.code for x in db.query(worker.BuyPaymentQR)]==['d123456789']
    assert api.request.call_args.kwargs['params']['orderDealState']=='NOT_PAID,WAIT_PROCESS'


def test_mexc_notice_survives_missing_beneficiary_and_deduplicates(db,monkeypatch):
    from app import notifications
    monkeypatch.setattr(worker.settings,'mexc_p2p_api_incoming_side','BUY')
    row=worker.BuyPaymentQR(key='MEXC:d123456789',exchange='MEXC',code='d123456789',
        channel='123456789012345678',created=time.time())
    db.add(row);db.commit()
    detail={**mexc(),'state':'WAIT_PROCESS','paymentInfo':[]}
    monkeypatch.setattr(worker,'read_mexc_buy_order',AsyncMock(return_value=detail))
    sender=AsyncMock(return_value=True);monkeypatch.setattr(notifications,'notify_event',sender)
    delivery=SimpleNamespace(send=AsyncMock(return_value=True))
    asyncio.run(worker.process(db,None,None,delivery,row))
    asyncio.run(worker.process(db,None,None,delivery,row))
    sender.assert_awaited_once()
    assert sender.call_args.args[0]=='ORDER_CREATED'
    assert 'MEXC' in sender.call_args.args[1] and '50,000,000' in sender.call_args.args[1]
    assert '00123456789' not in sender.call_args.args[1]
    assert delivery.send.call_args.args[3] is None


def test_mexc_wait_process_verified_twice_before_qr_send(db,monkeypatch):
    from app import mexc_ads,notifications
    monkeypatch.setattr(worker.settings,'mexc_p2p_api_incoming_side','BUY')
    row=worker.BuyPaymentQR(key='MEXC:d123456789',exchange='MEXC',code='d123456789',
        channel='123456789012345678',created=time.time())
    db.add(row);db.commit()
    api=SimpleNamespace(request=AsyncMock(return_value={**mexc(),'state':'WAIT_PROCESS'}))
    owned=AsyncMock(return_value={'side':'BUY'});monkeypatch.setattr(mexc_ads,'read_ad',owned)
    monkeypatch.setattr(notifications,'notify_event',AsyncMock(return_value=True))
    delivery=SimpleNamespace(send=AsyncMock(return_value=True))
    asyncio.run(worker.process(db,None,api,delivery,row))
    assert owned.await_count==2 and api.request.await_count==2
    assert delivery.send.call_args.args[3].startswith(b'\x89PNG')


def test_mexc_processing_after_initial_read_invalidates_qr(db,monkeypatch):
    from app import mexc_ads,notifications
    monkeypatch.setattr(worker.settings,'mexc_p2p_api_incoming_side','BUY')
    row=worker.BuyPaymentQR(key='MEXC:d123456789',exchange='MEXC',code='d123456789',
        channel='123456789012345678',created=time.time())
    db.add(row);db.commit()
    api=SimpleNamespace(request=AsyncMock(side_effect=[{**mexc(),'state':'WAIT_PROCESS'},
        {**mexc(),'state':'PROCESSING'}]))
    monkeypatch.setattr(mexc_ads,'read_ad',AsyncMock(return_value={'side':'BUY'}))
    monkeypatch.setattr(notifications,'notify_event',AsyncMock(return_value=True))
    delivery=SimpleNamespace(send=AsyncMock(return_value=True))
    asyncio.run(worker.process(db,None,api,delivery,row))
    assert not delivery.send.called and row.terminal


@pytest.mark.parametrize('payments',[None,[]])
def test_mexc_selected_account_without_list_is_verified_directly(payments):
    row=mexc();row['confirmPaymentInfo']=dict(row['paymentInfo'][0])
    row['paymentInfo']=payments;row['state']='WAIT_PROCESS'
    result=data.mexc_instruction(row,'d123456789','SELL')
    assert result.account=='00123456789' and result.bank=='MBBank' and result.note==''


@pytest.mark.parametrize('change',[{'id':None},{'payee':'OTHER PERSON'},{'account':'12***789'},
    {'payMethod':2},{'bankName':'UNKNOWN BANK'}])
def test_mexc_selected_only_account_never_bypasses_identity_or_bank(change):
    row=mexc();row['confirmPaymentInfo']={**row['paymentInfo'][0],**change}
    row['paymentInfo']=[];row['state']='WAIT_PROCESS'
    with pytest.raises(ValueError):data.mexc_instruction(row,'d123456789','SELL')


def test_mexc_person_name_in_bank_field_reports_order_but_no_qr(db,monkeypatch):
    from app import notifications
    monkeypatch.setattr(worker.settings,'mexc_p2p_api_incoming_side','BUY')
    row=worker.BuyPaymentQR(key='MEXC:d123456789',exchange='MEXC',code='d123456789',
        channel='123456789012345678',created=time.time())
    db.add(row);db.commit()
    detail={**mexc(),'state':'WAIT_PROCESS','paymentInfo':[],
        'confirmPaymentInfo':{**mexc()['paymentInfo'][0],'bankName':'NGUYEN VAN A'}}
    monkeypatch.setattr(worker,'read_mexc_buy_order',AsyncMock(return_value=detail))
    sender=AsyncMock(return_value=True);monkeypatch.setattr(notifications,'notify_event',sender)
    delivery=SimpleNamespace(send=AsyncMock(return_value=True))
    asyncio.run(worker.process(db,None,None,delivery,row))
    sender.assert_awaited_once()
    assert sender.call_args.args[0]=='ORDER_CREATED'
    assert delivery.send.call_args.args[3] is None
    assert 'MEXC chưa cung cấp tên/mã ngân hàng nhận hợp lệ' in delivery.send.call_args.args[2]['content']


def test_mexc_expired_wait_process_never_creates_payable_notice(db,monkeypatch):
    from app import mexc_ads,notifications
    monkeypatch.setattr(worker.settings,'mexc_p2p_api_incoming_side','BUY')
    row=worker.BuyPaymentQR(key='MEXC:d123456789',exchange='MEXC',code='d123456789',
        channel='123456789012345678',created=time.time())
    db.add(row);db.commit()
    api=SimpleNamespace(request=AsyncMock(return_value={**mexc(),'state':'WAIT_PROCESS',
        'payTimeLimit':int((time.time()-1)*1000)}))
    ad=AsyncMock();monkeypatch.setattr(mexc_ads,'read_ad',ad)
    sender=AsyncMock();monkeypatch.setattr(notifications,'notify_event',sender)
    delivery=SimpleNamespace(send=AsyncMock(return_value=True))
    asyncio.run(worker.process(db,None,api,delivery,row))
    assert row.terminal
    ad.assert_not_awaited();sender.assert_not_awaited();delivery.send.assert_not_awaited()

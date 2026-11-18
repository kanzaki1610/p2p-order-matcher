import asyncio
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import okx_auto_messages as messages
from app.database import Base
from app.models import P2POrder
from app.okx_sync import OKXOrderReceipt


@pytest.fixture
def db(monkeypatch):
    for name, value in dict(primary_web_control_enabled=True, database_schema='public', okx_p2p_enabled=True,
            okx_api_key='test-key', okx_api_secret='test-secret', okx_api_passphrase='test-pass').items():
        monkeypatch.setattr(messages.settings, name, value)
    clock = [datetime(2026, 11, 18, 0, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(messages, 'now', lambda: clock[0])
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False, expire_on_commit=False) as session:
        session.clock = clock
        yield session
    engine.dispose()


def activate(db, side='BUY', text='Xin chào\nNội dung thử nghiệm'):
    cfg = messages.config_row(db, side)
    messages.save(db, side, True, text, cfg.revision)
    return cfg


def seed(db, side='BUY', code='123456789012345', seconds=1):
    stamp = messages.now() + timedelta(seconds=seconds)
    row = dict(orderId=code, side=side.lower(), cryptoCurrency='usdt', fiatCurrency='vnd',
               fiatAmount='1000000', cryptoAmount='40', orderStatus='new', paymentStatus='unpaid',
               isFrozen=False, createdTimestamp=str(int(stamp.timestamp()*1000)),
               _checked_at=messages.now().timestamp())
    db.add(P2POrder(order_code=code, side=side, fiat_amount=1000000, crypto_amount=40,
                   counterparty_name='TEST PERSON', created_at=stamp, status='WAITING_PAYMENT'))
    db.add(OKXOrderReceipt(order_id=code, raw_order=json.dumps(row)))
    db.commit()
    return row


def mocks(monkeypatch, row, result=None):
    read = AsyncMock(return_value=row)
    send = AsyncMock(return_value={'isSentSuccessfully': True} if result is None else result)
    monkeypatch.setattr(messages, 'detail', read)
    monkeypatch.setattr(messages, 'request', send)
    return read, send


def run(db): asyncio.run(messages.cycle(db, None))


@pytest.mark.parametrize('side', ['BUY', 'SELL'])
def test_new_order_gets_one_message_of_its_own_side_after_durable_claim(db, monkeypatch, side):
    cfg = activate(db, side, 'Nội dung ' + side)
    row = seed(db, side)
    read, send = mocks(monkeypatch, row)
    async def post(client, method, path, body):
        assert db.get(messages.AutoMessageDelivery, row['orderId']).state == 'SUBMITTING'
        assert method == 'POST' and path == '/api/v5/p2p/chat/send-message'
        assert body == {'orderId':row['orderId'], 'content':cfg.body, 'contentType':'101'}
        return {'isSentSuccessfully': True}
    send.side_effect = post
    run(db); db.expire_all(); run(db)
    send.assert_awaited_once(); read.assert_awaited_once()
    assert db.get(messages.AutoMessageDelivery, row['orderId']).state == 'SENT'


def test_buy_sell_settings_are_separate_and_defaults_off(db, monkeypatch):
    state = messages.view(db)
    assert all(not r['enabled'] and not r['text'] for r in state['settings'])
    activate(db, 'SELL', 'Tin bán')
    buy = seed(db, 'BUY'); sell = seed(db, 'SELL', '123456789012346')
    read, send = mocks(monkeypatch, sell)
    run(db)
    assert db.get(messages.AutoMessageDelivery, buy['orderId']) is None
    assert send.call_args.args[3]['content'] == 'Tin bán'


@pytest.mark.parametrize('case', ['old', 'stale', 'future', 'mexc', 'closed', 'wrong_currency'])
def test_no_backlog_other_exchange_or_stale_source_messages(db, monkeypatch, case):
    activate(db)
    row = seed(db, seconds=-60 if case == 'old' else 1)
    if case == 'stale': row['_checked_at'] -= 61
    if case == 'future': row['createdTimestamp'] = str(int((messages.now().timestamp()+60)*1000))
    if case == 'mexc': row['orderId'] = 'd123456789'
    if case == 'closed': row['orderStatus'] = 'completed'
    if case == 'wrong_currency': row['fiatCurrency'] = 'USD'
    db.get(OKXOrderReceipt, '123456789012345').raw_order = json.dumps(row); db.commit()
    read, send = mocks(monkeypatch, row)
    run(db)
    read.assert_not_awaited(); send.assert_not_awaited()


@pytest.mark.parametrize('field,value', [('orderStatus','cancelled'),('orderStatus','completed'),
    ('side','sell'),('paymentStatus','confirmed'),('isFrozen',True),('fiatCurrency','USD')])
def test_read_order_again_before_send_and_skip_closed_or_changed_orders(db, monkeypatch, field, value):
    activate(db); row = seed(db); row[field] = value
    read, send = mocks(monkeypatch, row)
    run(db)
    send.assert_not_awaited()
    assert db.get(messages.AutoMessageDelivery, row['orderId']).state == 'SKIPPED'


def test_failed_get_is_read_again_but_no_post_is_retried(db, monkeypatch):
    activate(db); row = seed(db)
    read, send = mocks(monkeypatch, row)
    read.side_effect = [httpx.ReadTimeout('secret signed URL'), row]
    run(db)
    item = db.get(messages.AutoMessageDelivery, row['orderId'])
    assert item.state == 'PENDING' and 'secret' not in item.reason
    send.assert_not_awaited()
    run(db); run(db)
    send.assert_awaited_once()
    assert item.state == 'SENT'


@pytest.mark.parametrize('result,state', [({'isSentSuccessfully':False},'FAILED'),
    ({'isSentSuccessfully':'true'},'UNKNOWN'), ({},'UNKNOWN')])
def test_unconfirmed_chat_response_never_replays(db, monkeypatch, result, state):
    activate(db); row = seed(db); read, send = mocks(monkeypatch, row, result)
    run(db); run(db)
    assert db.get(messages.AutoMessageDelivery, row['orderId']).state == state
    send.assert_awaited_once()


def test_timeout_after_post_stays_unknown_across_restart_and_content_edit(db, monkeypatch):
    cfg = activate(db); row = seed(db); read, send = mocks(monkeypatch, row)
    send.side_effect = httpx.ReadTimeout('DO NOT EXPOSE THIS URL')
    run(db); db.expire_all()
    messages.save(db, 'BUY', True, 'Đổi lời nhắn', cfg.revision)
    run(db); run(db)
    item = db.get(messages.AutoMessageDelivery, row['orderId'])
    assert item.state == 'UNKNOWN' and 'DO NOT EXPOSE' not in messages.view(db)['deliveries'][0]['reason']
    send.assert_awaited_once()


@pytest.mark.parametrize('change', ['disable', 'edit'])
def test_configuration_changed_during_get_cancels_unsent_message(db, monkeypatch, change):
    cfg = activate(db); row = seed(db); read, send = mocks(monkeypatch, row)
    async def changed(*args):
        messages.save(db, 'BUY', change != 'disable', 'Nội dung mới', cfg.revision)
        return row
    read.side_effect = changed
    run(db)
    send.assert_not_awaited()
    assert db.get(messages.AutoMessageDelivery, row['orderId']).state == 'SKIPPED'


def test_cancelled_process_after_claim_does_not_replay_and_stale_claim_shows_unknown(db, monkeypatch):
    activate(db); row = seed(db); read, send = mocks(monkeypatch, row)
    send.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError): run(db)
    assert db.get(messages.AutoMessageDelivery, row['orderId']).state == 'SUBMITTING'
    db.clock[0] += timedelta(seconds=121)
    run(db)
    assert messages.view(db)['deliveries'][0]['state'] == 'UNKNOWN'
    send.assert_awaited_once()


def test_stale_preflight_cannot_reset_another_workers_durable_claim(db):
    activate(db); row = seed(db); messages.enqueue(db)
    item = db.get(messages.AutoMessageDelivery, row['orderId'])
    item.state = 'SUBMITTING'; db.commit()
    messages.finish(db, row['orderId'], 'PENDING', 'Stale read failed')
    db.expire_all()
    assert db.get(messages.AutoMessageDelivery, row['orderId']).state == 'SUBMITTING'


@pytest.mark.parametrize('scope', ['secondary_schema', 'secondary_package', 'web_disabled', 'api_disabled'])
def test_feature_cannot_send_for_secondary_or_when_unconfigured(db, monkeypatch, scope):
    activate(db); row = seed(db); read, send = mocks(monkeypatch, row)
    if scope == 'secondary_schema': monkeypatch.setattr(messages.settings, 'database_schema', 'kayhap')
    if scope == 'secondary_package': monkeypatch.setattr(messages, '__package__', 'app_kayhap')
    if scope == 'web_disabled': monkeypatch.setattr(messages.settings, 'primary_web_control_enabled', False)
    if scope == 'api_disabled': monkeypatch.setattr(messages.settings, 'okx_p2p_enabled', False)
    run(db); asyncio.run(messages.process(db, None, row['orderId']))
    read.assert_not_awaited(); send.assert_not_awaited()


def test_read_and_chat_post_use_existing_signed_api_with_no_other_write(db, monkeypatch):
    from app.okx_release import request as signed_request, detail as signed_detail
    activate(db); row = seed(db); calls = []
    def handler(req):
        calls.append(req)
        assert req.headers['OK-ACCESS-KEY'] == 'test-key' and req.headers['OK-ACCESS-SIGN']
        if req.method == 'GET':
            assert req.url.path == '/api/v5/p2p/order'
            return httpx.Response(200, json={'code':'0','data':[row]})
        assert req.url.path == '/api/v5/p2p/chat/send-message'
        assert json.loads(req.content) == {'orderId':row['orderId'], 'content':'Xin chào\nNội dung thử nghiệm', 'contentType':'101'}
        return httpx.Response(200, json={'code':'0','data':{'isSentSuccessfully':True}})
    monkeypatch.setattr(messages, 'request', signed_request); monkeypatch.setattr(messages, 'detail', signed_detail)
    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await messages.cycle(db, client)
    asyncio.run(execute())
    assert [r.method for r in calls] == ['GET', 'POST']


@pytest.mark.parametrize('text', ['', 'x'*1001, 'bad\x00text'])
def test_invalid_message_is_not_enabled(db, text):
    cfg=messages.config_row(db, 'BUY')
    with pytest.raises(ValueError): messages.save(db, 'BUY', True, text, cfg.revision)
    assert not cfg.enabled


def test_new_orders_are_throttled_without_losing_pending_work(db, monkeypatch):
    activate(db)
    first=seed(db); second=seed(db, code='123456789012346')
    monkeypatch.setattr(messages, 'detail', AsyncMock(side_effect=lambda client,code: first if code==first['orderId'] else second))
    send=AsyncMock(return_value={'isSentSuccessfully':True}); monkeypatch.setattr(messages,'request',send)
    run(db)
    assert send.await_count == 1
    db.clock[0] += timedelta(seconds=3)
    run(db)
    assert send.await_count == 2
    run(db)
    assert send.await_count == 2

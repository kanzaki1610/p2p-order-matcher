import asyncio
import json
import time
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app import mexc_price_adoption as adoption
from app import mexc_price_panel as panel
from app.models import OKXAuditLog
from app.mexc_price_state import MexcPendingPrice, record_pending
from test_mexc_pricing import ad, db, setup


@pytest.fixture
def scenario(db, setup, monkeypatch):
    slot, row = setup
    slot.state, slot.action, slot.attempt_id = 'UNKNOWN', 'PRICE', 'old-attempt'
    slot.last_action = Decimal(str(time.time() - 3600))
    slot.reason = 'Chưa rõ kết quả; danh sách cũ sai cấu trúc.'
    record_pending(db, slot, row, {**adoption.source_snapshot(row)['body'], 'advStatus': 'OPEN', 'price': '26070'})
    db.commit()
    # Manual changes to price, stock, limits and receiving account are allowed,
    # but only after the owner previews and explicitly adopts this new state.
    row.update(price='26110', quantity='200', availableQuantity='150', maxSingleTransAmount='3000000')
    row['paymentInfo'] = [{'id': 888, 'payMethod': 2}]
    read = AsyncMock(side_effect=lambda *args: dict(row))
    monkeypatch.setattr(adoption, 'read_ad', read)
    monkeypatch.setattr(adoption.asyncio, 'sleep', AsyncMock())
    return slot, row, read


def preview(db):
    result = asyncio.run(adoption.preview(db, 'owner', '1'))
    nonce = result['components'][0]['components'][0]['custom_id'].split(':')[-1]
    return result, nonce


def test_adopt_changed_sell_source_archives_without_exchange_write_or_auto(db, scenario):
    slot, row, read = scenario
    before = db.get(MexcPendingPrice, slot.key).body_json
    result, nonce = preview(db)
    assert '26,110' in result['content'] and 'Auto giữ TẮT' in result['content']
    assert slot.enabled is False and slot.state == 'UNKNOWN'
    message = asyncio.run(adoption.confirm(db, 'owner', nonce))
    assert 'Auto vẫn TẮT' in message
    assert slot.state == 'IDLE' and not slot.enabled and not slot.paused_by_floor
    assert db.get(MexcPendingPrice, slot.key) is None
    audit = db.scalar(select(OKXAuditLog).where(OKXAuditLog.action == 'MEXC_SLOT_ADOPTED'))
    detail = json.loads(audit.detail)
    assert detail['previous']['pending']['body_json'] == before
    assert detail['adopted_source']['body']['payMethod'] == '888'
    assert detail['old_result'] == 'UNRESOLVED' and not detail['exchange_write']
    assert read.await_count == 4  # Two GET readbacks at each operator step.
    with pytest.raises(ValueError, match='đã dùng'):
        asyncio.run(adoption.confirm(db, 'owner', nonce))


@pytest.mark.parametrize('field,value', [
    ('price', '26111'), ('quantity', '201'), ('availableQuantity', '149'),
    ('frozenQuantity', '1'), ('minSingleTransAmount', '200000'),
    ('maxSingleTransAmount', '2999999'), ('advStatus', 'CLOSE'),
    ('tradeTerms', 'changed'), ('paymentInfo', [{'id': 999, 'payMethod': 2}])])
def test_source_changed_after_preview_keeps_lock(db, scenario, field, value):
    slot, row, _ = scenario
    _, nonce = preview(db)
    row[field] = value
    with pytest.raises(ValueError):
        asyncio.run(adoption.confirm(db, 'owner', nonce))
    assert slot.state == 'UNKNOWN' and not slot.enabled
    assert db.get(MexcPendingPrice, slot.key) is not None


@pytest.mark.parametrize('change', ['owner', 'expired', 'cancelled', 'revision', 'pending'])
def test_one_use_confirmation_bound_to_owner_request_and_revision(db, scenario, change):
    slot, _, _ = scenario
    _, nonce = preview(db)
    draft = db.get(adoption.MexcAdoptionDraft, nonce)
    owner = 'other' if change == 'owner' else 'owner'
    if change == 'expired': draft.expires = 0
    if change == 'cancelled': draft.used = True
    if change == 'revision': slot.revision = 'new'
    if change == 'pending': db.get(MexcPendingPrice, slot.key).body_json = '{}'
    db.commit()
    with pytest.raises(ValueError): asyncio.run(adoption.confirm(db, owner, nonce))
    assert slot.state == 'UNKNOWN'


@pytest.mark.parametrize('change', ['young', 'submitting', 'buy', 'relative', 'absolute', 'visibility', 'missing', 'frozen'])
def test_ineligible_intents_never_offer_adoption(db, scenario, change):
    slot, row, _ = scenario
    pending = db.get(MexcPendingPrice, slot.key)
    body = json.loads(pending.body_json)
    if change == 'young': slot.last_action = Decimal(str(time.time()))
    if change == 'submitting': slot.state = 'SUBMITTING'
    if change == 'buy': slot.key = 'BUY:1'
    if change == 'relative': body['supplyQuantity'] = '1'
    if change == 'absolute': body['initQuantity'] = '101'
    if change == 'visibility': slot.action = 'HIDE'
    if change == 'missing': db.delete(pending)
    if change == 'frozen': row['frozenQuantity'] = '1'
    if change not in {'missing', 'buy'}: pending.body_json = json.dumps(body)
    db.commit()
    with pytest.raises(ValueError): preview(db)
    assert slot.state != 'IDLE'


def test_change_between_two_reads_keeps_lock(db, scenario, monkeypatch):
    slot, row, read = scenario
    async def unstable(*args):
        result = dict(row)
        row['availableQuantity'] = '149'
        return result
    read.side_effect = unstable
    with pytest.raises(ValueError, match='vừa thay đổi'): preview(db)
    assert slot.state == 'UNKNOWN'


def test_concurrent_change_after_confirm_reads_keeps_lock(db, scenario):
    slot, _, read = scenario
    _, nonce = preview(db)
    old_read = read.side_effect
    def race(*args):
        result = old_read(*args)
        slot.revision = 'concurrent'
        db.commit()
        return result
    read.side_effect = race
    with pytest.raises(ValueError): asyncio.run(adoption.confirm(db, 'owner', nonce))
    assert slot.state == 'UNKNOWN'


def test_cancel_is_owner_bound_and_keeps_pending(db, scenario):
    slot, _, _ = scenario
    _, nonce = preview(db)
    payload = {'type': 3, 'data': {'custom_id': 'mprice:adoptcancel:' + nonce}}
    asyncio.run(panel.execute_panel(payload, db, 'other'))
    assert not db.get(adoption.MexcAdoptionDraft, nonce).used
    asyncio.run(panel.execute_panel(payload, db, 'owner'))
    assert db.get(adoption.MexcAdoptionDraft, nonce).used
    assert db.get(MexcPendingPrice, slot.key) is not None and slot.state == 'UNKNOWN'


def test_closed_ad_adopted_without_reopening(db, scenario):
    slot, row, _ = scenario
    row['advStatus'] = 'CLOSE'
    _, nonce = preview(db)
    asyncio.run(adoption.confirm(db, 'owner', nonce))
    assert row['advStatus'] == 'CLOSE' and not slot.paused_by_floor and not slot.enabled

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import okx_ad_worker as worker
from app.models import OKXSlot, OKXAuditLog, OKXPricePreferences, OKXPricingSwitch


@pytest.fixture
def db():
    engine = create_engine('sqlite://')
    for table in (OKXSlot.__table__, OKXAuditLog.__table__, OKXPricePreferences.__table__, OKXPricingSwitch.__table__, worker.OKXAdPriceAttempt.__table__): table.create(engine)
    with Session(engine) as session: yield session
    engine.dispose()


@pytest.fixture
def setup(monkeypatch, db):
    monkeypatch.setattr(worker.settings, 'okx_ad_pricing_enabled', True)
    monkeypatch.setattr(worker.settings, 'database_schema', 'public')
    monkeypatch.setattr(worker.settings, 'discord_webhook_pricing_alerts', 'test')
    monkeypatch.setattr(worker, 'valid_webhook', lambda _: True)
    monkeypatch.setattr(worker, 'send_discord_message', AsyncMock(return_value=True))
    monkeypatch.setattr(worker, 'read_last_buy_cost', AsyncMock(return_value={'unit_price':'26050', 'order_id':'9'}))
    slot = OKXSlot(side='SELL', slot_number=1, auto_enabled=True, adv_id='123')
    db.add(slot); db.commit()
    return slot


def ad(code='123', price='26080', hidden=False, side='sell'):
    return dict(adId=code, side=side, type='limit', status='new', isHidden=hidden, unitPrice=price)


def plan(action='UPDATE_PRICE', price='26070'):
    return dict(action=action, proposed_price=price, cost_basis='26050', minimum_sell_price='26060', reason='test')


def run(db): asyncio.run(worker.process_ad_pricing(db, None))


@pytest.mark.parametrize('guard', ['disabled', 'friend', 'missing_hook'])
def test_disabled_or_wrong_account_never_calls_exchange(db, setup, monkeypatch, guard):
    if guard == 'disabled': monkeypatch.setattr(worker.settings, 'okx_ad_pricing_enabled', False)
    if guard == 'friend': monkeypatch.setattr(worker.settings, 'database_schema', 'kayhap')
    if guard == 'missing_hook': monkeypatch.setattr(worker, 'valid_webhook', lambda _: False)
    reader=AsyncMock(); monkeypatch.setattr(worker,'read_ad',reader)
    run(db); reader.assert_not_awaited()


def test_replacement_id_verified_and_saved(db, setup, monkeypatch):
    monkeypatch.setattr(worker,'read_ad',AsyncMock(side_effect=[ad(),ad('456','26070')]))
    monkeypatch.setattr(worker,'read_rank_plan',AsyncMock(return_value=plan()))
    write=AsyncMock(return_value={'oldAdId':'123','newAdId':'456'}); monkeypatch.setattr(worker,'request',write)
    run(db)
    assert setup.adv_id=='456' and setup.current_price==Decimal('26070')
    assert db.get(worker.OKXAdPriceAttempt,setup.id).state=='IDLE'
    assert write.call_args.args[3]=={'adId':'123','unitPrice':'26070'}


def test_hide_then_resume_only_bot_paused_ad(db, setup, monkeypatch):
    reader=AsyncMock(side_effect=[ad(),ad(hidden=True),ad(hidden=True),ad('456','26070')])
    monkeypatch.setattr(worker,'read_ad',reader)
    rank=AsyncMock(side_effect=[plan('HIDE_AD','26059'),plan()]); monkeypatch.setattr(worker,'read_rank_plan',rank)
    write=AsyncMock(side_effect=[{'adId':'123'},{'oldAdId':'123','newAdId':'456'}]); monkeypatch.setattr(worker,'request',write)
    run(db)
    attempt=db.get(worker.OKXAdPriceAttempt,setup.id)
    assert attempt.paused_by_floor is True
    attempt.last_action_at=None; db.commit(); run(db)
    assert attempt.paused_by_floor is False
    assert write.call_args.args[3]['online']=='true'


def test_manually_hidden_ad_stays_hidden(db, setup, monkeypatch):
    monkeypatch.setattr(worker,'read_ad',AsyncMock(side_effect=[ad(hidden=True),ad('456','26070',True)]))
    monkeypatch.setattr(worker,'read_rank_plan',AsyncMock(return_value=plan()))
    write=AsyncMock(return_value={'oldAdId':'123','newAdId':'456'}); monkeypatch.setattr(worker,'request',write)
    run(db); assert 'online' not in write.call_args.args[3]


def test_timeout_never_resubmits_after_restart(db, setup, monkeypatch):
    monkeypatch.setattr(worker,'read_ad',AsyncMock(return_value=ad()))
    monkeypatch.setattr(worker,'read_rank_plan',AsyncMock(return_value=plan()))
    write=AsyncMock(side_effect=TimeoutError()); monkeypatch.setattr(worker,'request',write)
    run(db); run(db)
    assert write.await_count==1 and db.get(worker.OKXAdPriceAttempt,setup.id).state=='UNKNOWN'


def test_manual_slot_change_does_not_get_overwritten(db, setup, monkeypatch):
    attempt=worker.OKXAdPriceAttempt(slot_id=setup.id,state='CONFIRMING',old_ad_id='999',result_ad_id='456',price=26070,hidden=False)
    db.add(attempt); db.commit()
    reader=AsyncMock(); monkeypatch.setattr(worker,'read_ad',reader)
    run(db); assert setup.adv_id=='123'; reader.assert_not_awaited()


def test_cost_failure_never_hides_or_writes(db, setup, monkeypatch):
    monkeypatch.setattr(worker,'read_ad',AsyncMock(return_value=ad()))
    monkeypatch.setattr(worker,'read_last_buy_cost',AsyncMock(side_effect=ValueError('private payload')))
    write=AsyncMock(); monkeypatch.setattr(worker,'request',write)
    run(db); write.assert_not_awaited()
    assert 'private payload' not in worker.send_discord_message.call_args.args[1]


def test_new_slot_ad_does_not_inherit_resume_permission(db, setup, monkeypatch):
    attempt=worker.OKXAdPriceAttempt(slot_id=setup.id,state='IDLE',old_ad_id='999',result_ad_id='999',paused_by_floor=True)
    db.add(attempt); db.commit()
    monkeypatch.setattr(worker,'read_ad',AsyncMock(side_effect=[ad(hidden=True),ad('456','26070',True)]))
    monkeypatch.setattr(worker,'read_rank_plan',AsyncMock(return_value=plan()))
    write=AsyncMock(return_value={'oldAdId':'123','newAdId':'456'}); monkeypatch.setattr(worker,'request',write)
    run(db); assert 'online' not in write.call_args.args[3]


def test_replacement_collision_does_not_overwrite(db, setup, monkeypatch):
    db.add(OKXSlot(side='BUY',slot_number=1,auto_enabled=True,adv_id='456'))
    attempt=worker.OKXAdPriceAttempt(slot_id=setup.id,state='CONFIRMING',old_ad_id='123',result_ad_id='456',price=26070,hidden=False)
    db.add(attempt); db.commit()
    monkeypatch.setattr(worker,'read_ad',AsyncMock(return_value=ad('456','26070')))
    assert asyncio.run(worker.confirm(db,None,attempt,setup)) is False
    assert setup.adv_id=='123'


def test_worker_uses_persisted_side_rank(db,setup,monkeypatch):
    from app.okx_price_controls import save_ranks
    save_ranks(db,{'sell_rank':6})
    monkeypatch.setattr(worker,'read_ad',AsyncMock(side_effect=[ad(),ad('456','26070')]))
    source=AsyncMock(return_value=plan());monkeypatch.setattr(worker,'read_rank_plan',source)
    monkeypatch.setattr(worker,'request',AsyncMock(return_value={'oldAdId':'123','newAdId':'456'}))
    run(db);assert source.call_args.kwargs['target_rank']==6


def test_rank_changed_while_reading_cancels_stale_write(db,setup,monkeypatch):
    from app.okx_price_controls import save_ranks
    async def changed(*args,**kwargs):
        save_ranks(db,{'sell_rank':7})
        return plan()
    monkeypatch.setattr(worker,'read_ad',AsyncMock(return_value=ad()))
    monkeypatch.setattr(worker,'read_rank_plan',changed)
    write=AsyncMock();monkeypatch.setattr(worker,'request',write)
    run(db);write.assert_not_awaited()

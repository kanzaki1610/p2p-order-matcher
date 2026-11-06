import asyncio
import json
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app import mexc_auto_terms as terms
from app import mexc_pricing as worker
from app import mexc_price_adoption as adoption
from app.mexc_price_state import expected_for, matches, record_pending, MexcPendingPrice
from test_mexc_pricing import ad, db, setup, run


def test_historical_total_does_not_replenish_sold_usdt_or_exceed_input_limit(db, monkeypatch):
    slot=worker.MexcPriceSlot(key='SELL:1',ad='a123');db.add(slot);db.commit()
    row=ad();row.update(quantity='38218.601',availableQuantity='7990.5931',price='26110',
        minSingleTransAmount='50000000',maxSingleTransAmount='208634385')
    monkeypatch.setattr(terms,'read_ad_limits',AsyncMock(return_value={k:Decimal(v) for k,v in
        dict(minPrice='10000',maxPrice='60000',minQuantity='100',maxQuantity='20000',
            minSingleTransAmount='140000',maxSingleTransAmount='27000000000').items()}))
    result=asyncio.run(terms.prepare(db,None,slot,row,Decimal('26098')))
    body=result['body']
    assert body['initQuantity']=='7990.5931'
    assert body['maxSingleTransAmount']=='208538497'
    assert 'supplyQuantity' not in body and 'reduceQuantity' not in body
    expected={**expected_for(row,body),**result['expected_extra']}
    assert expected['quantity']=='38218.601'
    final={**row,'price':'26098','maxSingleTransAmount':'208538497'}
    assert matches(final,expected)
    assert not matches({**final,'quantity':'7990.5931'},expected)
    assert not matches({**final,'availableQuantity':'7991'},expected)
    assert not matches({**final,'frozenQuantity':'1'},expected)
    assert matches({**final,'availableQuantity':'7890','frozenQuantity':'100'},expected)


def test_worker_readback_and_unchanged_check_use_cumulative_total(db,setup,monkeypatch):
    slot,row=setup
    row.update(quantity='40000',availableQuantity='5000')
    run(db)
    assert worker.write_price.await_count==1 and slot.state=='IDLE'
    body=worker.write_price.call_args.kwargs['body']
    assert body['initQuantity']=='5000'
    slot.last_action=0;db.commit()
    run(db)
    assert worker.write_price.await_count==1


def test_pending_intent_persists_both_stock_and_total(db,setup,monkeypatch):
    slot,row=setup
    row.update(quantity='40000',availableQuantity='5000')
    monkeypatch.setattr(worker,'write_price',AsyncMock(side_effect=ValueError('not confirmed')))
    run(db)
    pending=db.get(MexcPendingPrice,slot.key)
    assert json.loads(pending.body_json)['initQuantity']=='5000'
    expected=json.loads(pending.expected_json)
    assert expected['quantity']=='40000' and expected['stock_ceiling']=='5000'
    slot.last_action=0;db.commit()
    adoption.eligible(slot,pending)
    expected['stock_ceiling']='6000';pending.expected_json=json.dumps(expected)
    with pytest.raises(ValueError):adoption.eligible(slot,pending)


def test_remaining_stock_above_limit_blocks_without_write(db,setup,monkeypatch):
    slot,row=setup
    row.update(quantity='40000',availableQuantity='30000')
    monkeypatch.setattr(terms,'read_ad_limits',AsyncMock(return_value={k:Decimal(v) for k,v in
        dict(minPrice='1',maxPrice='60000',minQuantity='100',maxQuantity='20000',
            minSingleTransAmount='1',maxSingleTransAmount='27000000000').items()}))
    run(db)
    worker.write_price.assert_not_awaited()
    assert slot.state=='IDLE'

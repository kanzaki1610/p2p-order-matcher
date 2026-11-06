import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app import mexc_auto_terms as terms
from app import mexc_price_panel as panel
from app import mexc_price_adoption as adoption
from app.config import Settings
from app.mexc_price_state import expected_for, matches
from app.mexc_pricing import MexcPriceSlot
from test_mexc_pricing import ad, db


@pytest.mark.parametrize('price',['25900','26100'])
def test_price_only_preserves_limits_stock_and_existing_budget(db,monkeypatch,price):
    monkeypatch.setattr(terms.settings,'mexc_buy_price_only',True)
    history=AsyncMock(side_effect=AssertionError('Must not calculate budget'))
    monkeypatch.setattr(terms,'remaining',history)
    row=ad('BUY');row.update(quantity='13000',availableQuantity='7300',
        minSingleTransAmount='50000000',maxSingleTransAmount='180000000')
    slot=MexcPriceSlot(key='BUY:1',ad='a123')
    result=asyncio.run(terms.prepare(db,None,slot,row,Decimal(price)))
    body=result['body']
    assert body['initQuantity']=='13000'
    assert body['minSingleTransAmount']=='50000000'
    assert body['maxSingleTransAmount']=='180000000'
    assert not {'supplyQuantity','reduceQuantity','advStatus'} & body.keys()
    assert result['remaining_vnd'] is None and not result['exhausted']
    history.assert_not_awaited()
    expected={**expected_for(row,body),**result['expected_extra']}
    assert matches({**row,'price':price},expected)
    assert not matches({**row,'price':price,'availableQuantity':'7301'},expected)
    assert not matches({**row,'price':price,'maxSingleTransAmount':'180000001'},expected)


@pytest.mark.parametrize('change',[{'frozenQuantity':'1'},
    {'maxSingleTransAmount':'200000000'}, {'minSingleTransAmount':'0'},
    {'quantity':'0'}, {'availableQuantity':'14000'}])
def test_invalid_unchanged_terms_block_without_adjusting(db,monkeypatch,change):
    monkeypatch.setattr(terms.settings,'mexc_buy_price_only',True)
    row=ad('BUY');row.update(quantity='13000',availableQuantity='7300',
        minSingleTransAmount='50000000',maxSingleTransAmount='180000000')
    row.update(change)
    original=dict(row)
    with pytest.raises(ValueError):
        asyncio.run(terms.prepare(db,None,MexcPriceSlot(key='BUY:1',ad='a123'),row,Decimal('26000')))
    assert row==original


def test_mode_is_account_configuration_and_default_is_budget(monkeypatch):
    assert not Settings(_env_file=None).mexc_buy_price_only
    monkeypatch.setattr(panel.settings,'mexc_buy_price_only',True)
    assert 'BUY chỉ đổi giá' in panel.panel()['content']
    slot=MexcPriceSlot(key='BUY:1',ad='a123',enabled=False)
    assert adoption.record(slot,None,object())['buy_price_only'] is True
    monkeypatch.setattr(panel.settings,'mexc_buy_price_only',False)
    assert 'BUY giữ ngân sách' in panel.panel()['content']

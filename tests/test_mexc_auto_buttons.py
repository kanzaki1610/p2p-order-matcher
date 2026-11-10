import asyncio
from unittest.mock import AsyncMock

import pytest

from app import mexc_price_panel as panel
from app.mexc_pricing import MexcPriceSlot
from test_mexc_pricing import ad, db, setup


@pytest.mark.parametrize('side',['BUY','SELL'])
def test_enable_button_validates_price_without_local_name_shadowing(db,setup,monkeypatch,side):
    sell,_=setup
    slot=sell if side=='SELL' else MexcPriceSlot(key='BUY:1',ad='a456',nickname='VNTrader',enabled=False)
    if side=='BUY':db.add(slot)
    slot.enabled=False;db.commit()
    monkeypatch.setattr(panel,'valid_webhook',lambda _:True)
    read=AsyncMock(return_value=ad(side))
    monkeypatch.setattr(panel,'read_ad',read)
    response=asyncio.run(panel.execute_panel({'type':3,'data':{'custom_id':f'mprice:setauto:{side}:1:1'}},db,'owner'))
    assert slot.enabled and 'Auto bật' in response['content']
    read.assert_awaited_once()


def test_enable_button_still_rejects_closed_ad(db,setup,monkeypatch):
    slot,_=setup;slot.enabled=False;db.commit()
    monkeypatch.setattr(panel,'valid_webhook',lambda _:True)
    monkeypatch.setattr(panel,'read_ad',AsyncMock(return_value=ad(status='CLOSE')))
    with pytest.raises(ValueError,match='chưa OPEN'):
        asyncio.run(panel.execute_panel({'type':3,'data':{'custom_id':'mprice:setauto:SELL:1:1'}},db,'owner'))
    assert not slot.enabled

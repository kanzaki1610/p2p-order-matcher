import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.database import Base
from app.config import settings
from app.okx_cost import save_cost, read_pricing_cost, cost_revision, cost_summary
from app.discord_price_panel import panel, modal, execute_panel
from app.models import OKXManualCost

@pytest.fixture
def db():
    engine=create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine,expire_on_commit=False) as session:
        yield session
    engine.dispose()


def test_manual_cost_persists_and_skips_exchange_read(db):
    reply=save_cost(db,Decimal(26050),'discord:test')
    assert '26,060' in reply
    db.expire_all()
    reader=AsyncMock(side_effect=AssertionError('must not read OKX'))
    result=asyncio.run(read_pricing_cost(db,None,reader=reader))
    assert result['unit_price']=='26050' and result['mode']=='MANUAL'
    assert result['settings_revision']==cost_revision(db)
    reader.assert_not_awaited()


def test_last_completed_buy_cannot_replace_manual_cost(db):
    save_cost(db,Decimal(26050),'discord:test')
    old=cost_revision(db)
    with pytest.raises(ValueError):save_cost(db,None,'discord:test')
    reader=AsyncMock(return_value={'unit_price':'26070','mode':'LAST_COMPLETED_BUY','order_id':'test'})
    result=asyncio.run(read_pricing_cost(db,None,reader=reader))
    assert result['unit_price']=='26050'
    assert result['settings_revision']==old
    reader.assert_not_awaited()


@pytest.mark.parametrize('value',['0','-1','NaN','26050.5','1000000000000000'])
def test_bad_cost_cannot_overwrite_valid_value(db,value):
    save_cost(db,Decimal(26050),'discord:test')
    with pytest.raises(ValueError):save_cost(db,Decimal(value),'discord:test')
    assert db.get(OKXManualCost,1).unit_price==26050


def test_buttons_and_modal_save_without_any_exchange_write(db):
    buttons=[x for row in panel()['components'] for x in row['components']]
    assert 'Nhập giá vốn' in set(x['label'] for x in buttons)
    assert 'Dùng giá mua OKX' not in set(x['label'] for x in buttons)
    assert modal('cost','SELL')['type']==9
    payload={'data':{'custom_id':'price:form:cost:SELL','components':[{'components':[{'custom_id':'value','value':'26.050'}]}]}}
    reply=asyncio.run(execute_panel(payload,db,'test'))
    assert '26,060' in reply['content']
    assert db.get(OKXManualCost,1).enabled
    asyncio.run(execute_panel({'data':{'custom_id':'price:costauto'}},db,'test'))
    assert db.get(OKXManualCost,1).enabled


def test_unconfigured_cost_blocks_without_reading_history(db):
    reader=AsyncMock(side_effect=AssertionError('must not read history'))
    with pytest.raises(ValueError,match='thủ công'):asyncio.run(read_pricing_cost(db,None,reader=reader))
    reader.assert_not_awaited()


def test_friend_cannot_change_original_cost(db,monkeypatch):
    monkeypatch.setattr(settings,'database_schema','kayhap')
    with pytest.raises(ValueError):save_cost(db,Decimal(26050),'discord:test')
    assert db.get(OKXManualCost,1) is None

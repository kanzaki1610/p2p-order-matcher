import asyncio
import time
from decimal import Decimal
from unittest.mock import AsyncMock
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.database import Base
from app.config import settings
from app import release_price_guard as guard


def source(exchange='OKX', prices=None, **overrides):
    return dict({'ok':True,'exchange':exchange.lower(),'side':'sell','panel':exchange.lower()+'_sell',
        'saved_ts':1000,'age_sec':0,'cache':'fresh',
        'rows': prices if prices is not None else [{'nick':'Competitor','price':26000}]}, **overrides)


@pytest.mark.parametrize('exchange',['OKX','MEXC'])
@pytest.mark.parametrize('price,allowed',[('25500',False),('25499',False),('25501',True),('26001',True),('25500.01',True)])
def test_exact_500_boundary(exchange,price,allowed):
    result=guard.evaluate(source(exchange),exchange,price,'VNTrader',now=1000)
    assert result['allowed'] is allowed
    assert result['difference']==Decimal('26000')-Decimal(price)


def test_excludes_all_own_ads_before_first_competitor_without_sorting():
    data=source(prices=[{'nick':' vnTRADER ','price':25000},{'nick':'OTHER_OWN','price':25100},
        {'nick':'Competitor','price':26000},{'nick':'Other','price':25900}])
    result=guard.evaluate(data,'OKX',25500,'VNTrader,other_own',now=1000)
    assert result['reference']==26000 and not result['allowed']


@pytest.mark.parametrize('overrides',[{'saved_ts':969},{'saved_ts':1006},{'saved_ts':float('nan')},
    {'age_sec':31},{'cache':'stale'},{'side':'buy'},{'exchange':'mexc'},{'panel':'okx_buy'},
    {'ok':False},{'rows':[]},{'rows':[{'nick':'VNTrader','price':26000}]},
    {'rows':[{'nick':'','price':26000}]},{'rows':[{'nick':'Other','price':'NaN'}]}])
def test_bad_reference_never_allows(overrides):
    with pytest.raises(ValueError): guard.evaluate(source(**overrides),'OKX',25500,'VNTrader',now=1000)


def test_missing_own_nickname_blocks():
    with pytest.raises(ValueError): guard.evaluate(source(),'OKX',25500,'',now=1000)


def test_mexc_new_cache_is_accepted():
    assert guard.evaluate(source('MEXC',cache='new'),'MEXC',26000,'Me',now=1000)['allowed']


def test_check_errors_are_safe_and_disabled_never_fetches(monkeypatch):
    monkeypatch.setattr(settings,'release_price_guard_enabled',False)
    assert asyncio.run(guard.check('OKX',{}))=={'allowed':True,'kind':'DISABLED'}
    monkeypatch.setattr(settings,'release_price_guard_enabled',True)
    monkeypatch.setattr(settings,'okx_merchant_nicknames','VNTrader')
    assert asyncio.run(guard.check('OKX',{}))=={'allowed':False,'kind':'SOURCE_UNAVAILABLE'}


def test_alert_is_compact_and_deduplicated_per_order_kind(monkeypatch):
    engine=create_engine('sqlite://'); Base.metadata.create_all(engine)
    monkeypatch.setattr(settings,'discord_bot_token','')
    sent=AsyncMock(return_value=True)
    monkeypatch.setattr(guard,'notify_event',sent)
    result=guard.evaluate(source(),'OKX',25500,'VNTrader',now=1000)
    with Session(engine) as db:
        for _ in range(2): asyncio.run(guard.alert(db,'OKX',SimpleNamespace(id=1,order_code='TEST'),result))
    assert sent.await_count==1
    assert sent.await_args.args[0]=='REVIEW_REQUIRED_OKX'
    assert '500' in sent.await_args.args[1] and 'CẢNH BÁO GIÁ' in sent.await_args.args[1]
    engine.dispose()

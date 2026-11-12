import asyncio
from unittest.mock import AsyncMock

import pytest
from app import manual_stock as stock, okx_ads, mexc_ads
from app.models import OKXSlot
from test_web_control import client


@pytest.mark.parametrize('value', [None, '-1', 'NaN', 'Infinity', 'bad', '1000000000000000'])
def test_invalid_quantity_blocked(value):
    with pytest.raises(ValueError):stock.quantity(value)


def test_all_converts_wallet_remaining_to_total_and_preserves_reserved_history():
    ad={'quantity':'6000','available':'1000','frozen':'200'}
    assert stock.fill_quantity(ad,'3000')=={'quantity':'8000','sell_available':'3000'}
    assert stock.fill_quantity(ad,'3000.123456789')['sell_available']=='3000.12345678'
    with pytest.raises(ValueError):stock.fill_quantity({**ad,'available':'5900'},'3000')


def test_okx_balance_is_authenticated_usdt_only_and_caps_security_limit(monkeypatch):
    request=AsyncMock(return_value=dict(currencySymbol='USDT',availableAmount='123.5',onHoldAmount='4',
        isSecurityLimitApplicable=True,sellOrTransferLimit='100'))
    monkeypatch.setattr(stock,'request',request)
    result=asyncio.run(stock.wallet(object(),'OKX'))
    assert result['available']=='100' and result['held']=='4'
    assert request.call_args.args[1:] == ('GET','/api/v5/p2p/user/balance?currencySymbol=USDT')
    request.return_value['currencySymbol']='BTC'
    with pytest.raises(ValueError):asyncio.run(stock.wallet(object(),'OKX'))
    request.return_value['currencySymbol']='USDT';request.return_value.pop('sellOrTransferLimit')
    with pytest.raises(ValueError):asyncio.run(stock.wallet(object(),'OKX'))


def test_mexc_never_substitutes_spot_or_ad_stock_for_wallet(monkeypatch):
    request=AsyncMock();monkeypatch.setattr(stock,'request',request)
    result=asyncio.run(stock.wallet(object(),'MEXC'))
    assert not result['supported'] and result['available'] is None
    request.assert_not_awaited()


def test_stock_endpoint_auth_scope_context_and_no_mutations(client,monkeypatch):
    with client.factory() as db:
        db.add(OKXSlot(side='SELL',slot_number=1,adv_id='456',auto_enabled=True));db.commit()
    monkeypatch.setattr(okx_ads,'read_ad',AsyncMock(return_value=dict(side='sell',cryptoAmount='6000',
        availableAmount='1000',onHoldAmount='200')))
    monkeypatch.setattr(stock,'wallet',AsyncMock(return_value={'supported':True,'available':'3000','held':'200','reason':''}))
    url='/dashboard/api/control/manual-stock/OKX/SELL/1'
    assert client.get(url,headers={'X-Admin-Key':'wrong'}).status_code==401
    result=client.get(url)
    assert result.status_code==200 and result.headers['cache-control']=='no-store'
    data=result.json()
    assert data['fill']=={'quantity':'8000','sell_available':'3000'}
    assert (data['exchange'],data['side'],data['slot'],data['ad_id'])==('OKX','SELL',1,'456')
    assert client.get('/dashboard/api/control/manual-stock/OKX/SELL/2').status_code==400
    with client.factory() as db:
        assert db.query(OKXSlot).filter_by(side='SELL').one().auto_enabled
    monkeypatch.setattr(okx_ads,'read_ad',AsyncMock(return_value={'side':'buy'}))
    assert client.get(url).status_code==400


def test_wallet_read_failure_does_not_present_ad_stock_as_wallet(client,monkeypatch):
    monkeypatch.setattr(okx_ads,'read_ad',AsyncMock(return_value=dict(side='buy',cryptoAmount='6000',
        availableAmount='1000',onHoldAmount='200')))
    monkeypatch.setattr(stock,'wallet',AsyncMock(side_effect=ValueError('Không đọc được ví P2P.')))
    data=client.get('/dashboard/api/control/manual-stock/OKX/BUY/1').json()
    assert data['ad']['available']=='1000'
    assert data['wallet']['available'] is None and data['fill'] is None



def test_wallet_mode_web_preview_only_and_rejects_wrong_exchange_side(client,monkeypatch):
    from app import web_control as web
    from decimal import Decimal
    preview=AsyncMock(return_value={'content':'preview only','components':[]})
    monkeypatch.setattr(web.okx_ui,'manual_preview',preview)
    body={'action':'manual','exchange':'OKX','side':'SELL','slot':1,
          'fields':{'price':'26500','quantity':'3000','quantity_mode':'remaining','maximum':'79500000'}}
    assert client.post('/dashboard/api/control/action',json=body).status_code==200
    assert preview.call_args.kwargs['remaining']==Decimal('3000')
    assert 'cryptoAmount' not in preview.call_args.args[-1]
    for changes in ({'side':'BUY'},{'exchange':'MEXC'}):
        assert client.post('/dashboard/api/control/action',json={**body,**changes}).status_code==400


def test_remaining_preview_preserves_history_and_confirm_checks_wallet_again(client,monkeypatch):
    from decimal import Decimal
    from app import discord_price_panel as ui
    with client.factory() as db:
        db.add(OKXSlot(side='SELL',slot_number=1,adv_id='456',auto_enabled=True));db.commit()
        ad=dict(adId='456',side='sell',type='limit',status='new',isHidden=False,unitPrice='26000',
                cryptoAmount='6000',availableAmount='1000',onHoldAmount='200',minOrderSize='100',maxOrderSize='79500000')
        monkeypatch.setattr(ui,'read_ad',AsyncMock(return_value=ad))
        monkeypatch.setattr(ui,'read_pricing_cost',AsyncMock(return_value={'mode':'DISABLED'}))
        balances=AsyncMock(side_effect=[{'available':'3000'},{'available':'2999'}])
        monkeypatch.setattr(stock,'wallet',balances)
        write=AsyncMock();monkeypatch.setattr(ui,'request',write)
        result=asyncio.run(ui.manual_preview(db,'owner','SELL',1,Decimal('26500'),
            {'maxOrderLimit':'79500000'},remaining=Decimal('3000')))
        assert '3000' in result['content']
        draft=db.query(ui.ManualPriceDraft).one()
        import json
        terms=json.loads(db.get(ui.ManualAdTerms,draft.id).terms_json)
        assert terms['cryptoAmount']=='8000'
        with pytest.raises(ValueError,match='Số dư ví P2P đã giảm'):
            asyncio.run(ui.manual_confirm(db,'owner',draft.id))
        write.assert_not_awaited()
        assert db.query(OKXSlot).filter_by(side='SELL').one().auto_enabled

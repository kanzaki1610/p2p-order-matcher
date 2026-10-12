import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from app import okx_cost as cost


def order(code='1', stamp=1000, price='26050'):
    return dict(orderId=code, side='buy', orderStatus='completed', paymentStatus='confirmed',
        cryptoCurrency='usdt', fiatCurrency='vnd', completionTimestamp=stamp, unitPrice=price)


@pytest.mark.parametrize('wrapper', [lambda rows: rows, lambda rows: {'orders': rows}, lambda rows: [{'orders': rows}]])
def test_history_known_wrappers_and_malformed_rows(wrapper):
    assert cost.history_rows(wrapper([order()])) == [order()]
    with pytest.raises(ValueError):
        cost.history_rows(wrapper([order(), {'orderStatus': 'completed'}]))


def test_completion_aliases_and_conflicting_timestamps():
    assert cost.completion_time({'completedTimestamp': 1000}) == 1000
    with pytest.raises(ValueError):
        cost.completion_time({'completionTimestamp': 1000, 'completedTimestamp': 2000})


def test_latest_completion_across_pages_not_creation_order(monkeypatch):
    first = [order(str(i), 1000+i) for i in range(20)]
    latest = order('21', 2000)
    history = AsyncMock(side_effect=[first, [latest, dict(order('22', 3000), orderStatus='canceled')]])
    detail = AsyncMock(return_value=latest)
    monkeypatch.setattr(cost, 'history_page', history)
    monkeypatch.setattr(cost, 'request', detail)
    result = asyncio.run(cost.read_last_buy_cost(None))
    assert result['order_id'] == '21' and result['unit_price'] == '26050'
    assert history.await_count == 2


def test_duplicate_pages_fail_closed(monkeypatch):
    rows = [order(str(i)) for i in range(20)]
    monkeypatch.setattr(cost, 'history_page', AsyncMock(side_effect=[rows, rows]))
    with pytest.raises(ValueError): asyncio.run(cost.read_last_buy_cost(None))


def test_detail_price_must_match_history(monkeypatch):
    monkeypatch.setattr(cost, 'history_page', AsyncMock(return_value=[order()]))
    monkeypatch.setattr(cost, 'request', AsyncMock(return_value=order(price='26051')))
    with pytest.raises(ValueError): asyncio.run(cost.read_last_buy_cost(None))


def test_history_signed_read_and_credential_destination():
    def handler(req):
        assert req.method == 'GET' and req.url.host == 'www.okx.com'
        assert req.url.params['completionStatus'] == 'completed'
        assert 'OK-ACCESS-SIGN' in req.headers
        return httpx.Response(200, json={'code': '0', 'data': [order()]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await cost.history_page(client, 1)
    assert asyncio.run(run())[0]['orderId'] == '1'

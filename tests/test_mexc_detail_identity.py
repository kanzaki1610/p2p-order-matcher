"""Compatibility with documented and observed MEXC P2P detail IDs."""
import asyncio

import httpx
import pytest

from app.mexc import MexcAPIError, MexcP2PClient


def detail(row):
    async def execute():
        def handler(request):
            assert request.method == "GET"
            assert request.url.params["advOrderNo"] == "d123"
            return httpx.Response(200, json={"code": 0, "data": row})

        async with MexcP2PClient(api_key="test", api_secret="test",
                transport=httpx.MockTransport(handler)) as client:
            return await client.get_order_detail("d123")

    return asyncio.run(execute())


def test_documented_order_id_is_normalized_without_losing_other_fields():
    row = {"advNo": "d123", "advOrderNo": "a456", "state": "PAID"}
    result = detail(row)
    assert result == {"advNo": "d123", "advOrderNo": "d123", "state": "PAID"}
    assert row["advOrderNo"] == "a456"


def test_observed_live_order_id_remains_supported():
    row = {"advOrderNo": "d123", "state": "PAID"}
    assert detail(row) == row


@pytest.mark.parametrize("row", [
    {}, {"advNo": "d999", "advOrderNo": "a456"},
    {"advNo": None, "advOrderNo": "d999"},
])
def test_wrong_or_missing_identity_is_rejected(row):
    with pytest.raises(MexcAPIError, match="khác mã lệnh"):
        detail(row)

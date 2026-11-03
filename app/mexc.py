import asyncio
import logging
import hashlib
import hmac
import time
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, quote

import httpx

from .config import settings

logger = logging.getLogger("uvicorn.error.mexc")


class MexcAPIError(RuntimeError):
    """Safe-to-display MEXC error that never contains credentials."""

    def __init__(self, message, *, http_status=None, api_code=None):
        super().__init__(message)
        self.http_status = http_status
        value = str(api_code) if api_code is not None else ''
        self.api_code = value if re.fullmatch(r'-?[0-9]{1,12}', value) else None


@dataclass(frozen=True)
class MexcCredentials:
    api_key: str
    api_secret: str

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_secret)


class MexcP2PClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.credentials = MexcCredentials(
            api_key=api_key if api_key is not None else settings.mexc_p2p_api_key,
            api_secret=api_secret if api_secret is not None else settings.mexc_p2p_api_secret,
        )
        self.base_url = (base_url or settings.mexc_p2p_base_url).rstrip("/")
        self.transport = transport
        self._client = None

    def _new_client(self):
        return httpx.AsyncClient(
            base_url=self.base_url, timeout=httpx.Timeout(30, connect=15),
            transport=self.transport, limits=httpx.Limits(max_connections=5, max_keepalive_connections=5),
        )

    async def __aenter__(self):
        self._client = self._new_client()
        return self

    async def __aexit__(self, *args):
        await self._client.aclose()
        self._client = None

    async def request(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        for attempt in range(3):
            try:
                return await self._request_once(path, params=params)
            except MexcAPIError as exc:
                cause = exc.__cause__
                retry = isinstance(cause, httpx.TransportError) or (
                    isinstance(cause, httpx.HTTPStatusError)
                    and cause.response.status_code in {429, 500, 502, 503, 504}
                )
                if not retry or attempt == 2:
                    raise
                delay = 2 ** attempt
                if isinstance(cause, httpx.HTTPStatusError):
                    try:
                        delay = min(60, max(delay, float(cause.response.headers.get('Retry-After', delay))))
                    except ValueError:
                        pass
                logger.warning('MEXC request retry | attempt=%s/3 | error=%s', attempt + 1, type(cause).__name__)
                await asyncio.sleep(delay)

    @property
    def configured(self) -> bool:
        return self.credentials.configured

    def _signature(self, query_string: str) -> str:
        return hmac.new(
            self.credentials.api_secret.encode(),
            query_string.encode(),
            hashlib.sha256,
        ).hexdigest()

    async def _request_once(self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET") -> Any:
        if not self.configured:
            raise MexcAPIError("Chưa cấu hình đủ API Key và Secret Key của MEXC")
        signed_params = [(key, str(value)) for key, value in (params or {}).items() if value is not None]
        signed_params.extend(
            [
                ("recvWindow", "10000"),
                ("timestamp", str(int(time.time() * 1000))),
            ]
        )
        # MEXC verifies RFC3986 query encoding. Form-style '+' for spaces is
        # rejected (700002), including spaces in advertisement terms/replies.
        # Sign exactly the percent-encoded bytes sent on the wire.
        query = urlencode(signed_params, quote_via=quote)
        signature = self._signature(query)
        request_path = f"{path}?{query}&signature={signature}"
        try:
            async def fetch(client):
                return await client.request(method, request_path, headers={
                    'X-MEXC-APIKEY': self.credentials.api_key, 'Accept': 'application/json',
                })
            if self._client is not None:
                response = await fetch(self._client)
            else:
                async with self._new_client() as client:
                    response = await fetch(client)
            response.raise_for_status()
            result = response.json()
        except httpx.HTTPStatusError as exc:
            code = "UNKNOWN"
            try:
                error_data = exc.response.json()
                if isinstance(error_data, dict):
                    value = str(error_data.get("code", ''))
                    if re.fullmatch(r'-?[0-9]{1,12}',value): code=value
            except ValueError: pass
            # Never echo response messages: providers may include signed URLs,
            # API keys, submitted terms or payment-account information in them.
            raise MexcAPIError(f"MEXC HTTP {exc.response.status_code} · {code}: Sàn trả lỗi yêu cầu.",
                http_status=exc.response.status_code,api_code=code) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise MexcAPIError(f"Không thể kết nối MEXC: {type(exc).__name__}") from exc

        if method == "POST" and (not isinstance(result, dict) or str(result.get("code")) != "0"):
            value = str(result.get('code','')) if isinstance(result,dict) else ''
            code = value if re.fullmatch(r'-?[0-9]{1,12}',value) else 'UNKNOWN'
            raise MexcAPIError(f"MEXC HTTP {response.status_code} · {code}: Chưa xác nhận yêu cầu ghi; không gửi lại.",
                http_status=response.status_code,api_code=code)
        if isinstance(result, dict) and str(result.get("code", "0")) not in {"0", "200", "None"}:
            value=str(result.get('code',''));code=value if re.fullmatch(r'-?[0-9]{1,12}',value) else 'UNKNOWN'
            raise MexcAPIError(f"MEXC HTTP {response.status_code} · {code}: Sàn trả lỗi yêu cầu.",
                http_status=response.status_code,api_code=code)
        return result.get("data") if isinstance(result, dict) and "data" in result else result

    async def release_coin(self, order_code: str) -> None:
        # Never retry a financial POST, including HTTP 429/5xx or timeouts.
        # The coordinator persists UNKNOWN before calling this method.
        if not (settings.mexc_auto_release_enabled and settings.mexc_p2p_live_writes):
            raise MexcAPIError("MEXC auto release đang tắt")
        await self._request_once("/api/v3/fiat/release_coin",
            params={"advOrderNo": order_code}, method="POST")

    async def get_orders(
        self,
        *,
        start_time: int,
        end_time: int,
        side: str | None = None,
        states: str | None = None,
        limit: int = 50,
        maker_view: bool = True,
    ) -> Any:
        return await self.request(
            (
                "/api/v3/fiat/merchant/order/paginationV2"
                if maker_view
                else "/api/v3/fiat/market/order/paginationV2"
            ),
            params={
                "side": side,
                "orderDealState": states,
                "startTime": start_time,
                "endTime": end_time,
                "limit": max(1, min(int(limit), 100)),
            },
        )

    async def get_order_detail(self, order_code: str) -> dict[str, Any]:
        data = await self.request(
            "/api/v3/fiat/order/detail",
            params={"advOrderNo": order_code},
        )
        if not isinstance(data, dict):
            raise MexcAPIError("MEXC trả dữ liệu chi tiết lệnh không hợp lệ")
        # The current P2P documentation names the order ID advNo and the
        # advertisement ID advOrderNo. Some live responses use advOrderNo
        # for the order instead. Normalize only an exact requested-ID match.
        if str(data.get("advOrderNo") or "") != order_code:
            if str(data.get("advNo") or "") != order_code:
                raise MexcAPIError("MEXC trả chi tiết khác mã lệnh yêu cầu")
            data = {**data, "advOrderNo": order_code}
        return data

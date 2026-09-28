import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import settings


class MexcAPIError(RuntimeError):
    """Safe-to-display MEXC error that never contains credentials."""


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

    @property
    def configured(self) -> bool:
        return self.credentials.configured

    def _signature(self, query_string: str) -> str:
        return hmac.new(
            self.credentials.api_secret.encode(),
            query_string.encode(),
            hashlib.sha256,
        ).hexdigest()

    async def request(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        if not self.configured:
            raise MexcAPIError("Chưa cấu hình đủ API Key và Secret Key của MEXC")
        signed_params = [(key, str(value)) for key, value in (params or {}).items() if value is not None]
        signed_params.extend(
            [
                ("recvWindow", "10000"),
                ("timestamp", str(int(time.time() * 1000))),
            ]
        )
        query = urlencode(signed_params)
        signature = self._signature(query)
        request_path = f"{path}?{query}&signature={signature}"
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=15,
                transport=self.transport,
            ) as client:
                response = await client.get(
                    request_path,
                    headers={
                        "X-MEXC-APIKEY": self.credentials.api_key,
                        "Accept": "application/json",
                    },
                )
            response.raise_for_status()
            result = response.json()
        except httpx.HTTPStatusError as exc:
            code = "UNKNOWN"
            message = "Không có nội dung lỗi"
            try:
                error_data = exc.response.json()
                if isinstance(error_data, dict):
                    code = str(error_data.get("code") or code)[:50]
                    message = str(error_data.get("msg") or error_data.get("message") or message)[:300]
            except ValueError:
                if exc.response.text.strip():
                    message = exc.response.text.strip()[:300]
            raise MexcAPIError(f"MEXC HTTP {exc.response.status_code} · {code}: {message}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise MexcAPIError(f"Không thể kết nối MEXC: {type(exc).__name__}") from exc

        if isinstance(result, dict) and str(result.get("code", "0")) not in {"0", "200", "None"}:
            raise MexcAPIError(
                f"MEXC {str(result.get('code', 'UNKNOWN'))[:50]}: "
                f"{str(result.get('msg') or result.get('message') or 'API error')[:300]}"
            )
        return result.get("data") if isinstance(result, dict) and "data" in result else result

    async def get_orders(
        self,
        *,
        start_time: int,
        end_time: int,
        side: str = "SELL",
        states: str | None = None,
        limit: int = 50,
    ) -> Any:
        return await self.request(
            "/api/v3/fiat/market/order/paginationV2",
            params={
                "coinId": "USDT",
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
        return data

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import settings


class BitgetAPIError(RuntimeError):
    """A safe-to-display Bitget API error without credential material."""


@dataclass(frozen=True)
class BitgetCredentials:
    api_key: str
    api_secret: str
    passphrase: str

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_secret and self.passphrase)


class BitgetP2PClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        passphrase: str | None = None,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.credentials = BitgetCredentials(
            api_key=api_key if api_key is not None else settings.bitget_p2p_api_key,
            api_secret=api_secret if api_secret is not None else settings.bitget_p2p_api_secret,
            passphrase=passphrase if passphrase is not None else settings.bitget_p2p_api_passphrase,
        )
        self.base_url = (base_url or settings.bitget_p2p_base_url).rstrip("/")
        self.transport = transport

    @property
    def configured(self) -> bool:
        return self.credentials.configured

    def _signature(self, timestamp: str, method: str, request_path: str, body: str = "") -> str:
        prehash = f"{timestamp}{method.upper()}{request_path}{body}"
        digest = hmac.new(
            self.credentials.api_secret.encode(),
            prehash.encode(),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode()

    def _headers(self, timestamp: str, method: str, request_path: str, body: str = "") -> dict[str, str]:
        return {
            "ACCESS-KEY": self.credentials.api_key,
            "ACCESS-SIGN": self._signature(timestamp, method, request_path, body),
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-PASSPHRASE": self.credentials.passphrase,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "locale": "en-US",
        }

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        if not self.configured:
            raise BitgetAPIError("Chưa cấu hình đủ API Key, Secret Key và API Passphrase của Bitget")
        query = urlencode([(key, str(value)) for key, value in (params or {}).items() if value is not None])
        request_path = f"{path}?{query}" if query else path
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) if payload else ""
        timestamp = str(int(time.time() * 1000))
        headers = self._headers(timestamp, method, request_path, body)
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=15,
                transport=self.transport,
            ) as client:
                response = await client.request(
                    method.upper(),
                    request_path,
                    headers=headers,
                    content=body or None,
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
                plain_text = exc.response.text.strip()
                if plain_text:
                    message = plain_text[:300]
            raise BitgetAPIError(
                f"Bitget HTTP {exc.response.status_code} · {code}: {message}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise BitgetAPIError(f"Không thể kết nối Bitget: {type(exc).__name__}") from exc
        if str(result.get("code")) != "00000":
            code = str(result.get("code", "UNKNOWN"))
            message = str(result.get("msg", "Bitget API error"))[:300]
            raise BitgetAPIError(f"Bitget {code}: {message}")
        return result.get("data")

    async def get_currencies(self) -> dict[str, Any]:
        return await self.request("GET", "/api/v3/p2p/currencies")

    async def get_user_info(self) -> dict[str, Any]:
        return await self.request("GET", "/api/v3/p2p/user-info")

    async def get_balance(self, token: str = "USDT") -> dict[str, Any]:
        return await self.request("GET", "/api/v3/p2p/balance", params={"token": token})

    async def get_ad_list(
        self,
        *,
        dashboard_side: str,
        token: str = "USDT",
        fiat: str = "VND",
        limit: int = 10,
        amount: str | None = None,
    ) -> list[dict[str, Any]]:
        side = dashboard_side.upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("dashboard_side phải là BUY hoặc SELL")
        # Bitget side is the advertisement owner's direction. To buy USDT we read sell ads;
        # to sell USDT we read buy ads.
        api_side = "sell" if side == "BUY" else "buy"
        data = await self.request(
            "GET",
            "/api/v3/p2p/ad-list",
            params={
                "token": token,
                "fiat": fiat,
                "side": api_side,
                "pageNum": 1,
                "limit": max(1, min(int(limit), 10)),
                "amount": amount,
            },
        )
        return list(data or [])


def normalize_bitget_offers(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    offers: list[dict[str, Any]] = []
    for item in items:
        try:
            price = str(item.get("price") or "0")
            if float(price) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        offers.append(
            {
                "nickname": str(item.get("merchantName") or item.get("merchantId") or "Bitget Merchant"),
                "price": price,
                "min_amount": str(item.get("minAmount") or "0"),
                "max_amount": str(item.get("maxAmount") or "0"),
                "available_usdt": str(item.get("quantity")) if item.get("quantity") is not None else None,
                "account_days": 0,
                "completed_orders": int(float(item.get("completedOrderNum") or 0)),
                "total_orders": int(float(item.get("completedOrderNum") or 0)),
            }
        )
    return offers

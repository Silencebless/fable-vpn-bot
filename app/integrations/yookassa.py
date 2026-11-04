"""Клиент ЮKassa API v3 (https://yookassa.ru/developers).

Аутентификация — Basic (shop_id : secret_key), идемпотентность — заголовок Idempotence-Key.
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://api.yookassa.ru/v3"

class YooKassaError(Exception):
    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


def fmt_amount(value: Decimal | int | str) -> dict:
    return {"value": f"{Decimal(str(value)):.2f}", "currency": "RUB"}


class YooKassaClient:
    def __init__(self, shop_id: str, secret_key: str, *, timeout: float = 20.0) -> None:
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            auth=(shop_id, secret_key),
            timeout=httpx.Timeout(timeout, connect=8.0),
            headers={"User-Agent": "fable-vpn-bot/1.0"},
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        idempotence_key: str | None = None,
        retries: int = 2,
    ) -> dict:
        headers = {"Idempotence-Key": idempotence_key} if idempotence_key else {}
        attempt = 0
        while True:
            try:
                resp = await self._http.request(method, path, json=json, headers=headers)
            except httpx.TransportError as e:
                # безопасно повторять: у POST есть Idempotence-Key
                if attempt < retries and (method == "GET" or idempotence_key):
                    attempt += 1
                    await asyncio.sleep(0.8 * attempt)
                    continue
                raise YooKassaError(f"ЮKassa недоступна ({type(e).__name__})") from e
            if resp.status_code == 202 and attempt < retries + 2:
                # запрос принят в обработку — повторить позже с тем же ключом
                attempt += 1
                await asyncio.sleep(1.5)
                continue
            break

        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400:
            desc = data.get("description") if isinstance(data, dict) else None
            code = data.get("code") if isinstance(data, dict) else None
            raise YooKassaError(f"{method} {path}: {desc or resp.text[:200]}", resp.status_code, code)
        return data if isinstance(data, dict) else {}

    async def create_payment(
        self,
        *,
        amount: Decimal,
        description: str,
        return_url: str,
        metadata: dict,
        idempotence_key: str,
        receipt: dict | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "amount": fmt_amount(amount),
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": return_url},
            "description": description[:128],
            "metadata": metadata,
        }
        if receipt:
            body["receipt"] = receipt
        return await self._request("POST", "/payments", json=body, idempotence_key=idempotence_key)

    async def get_payment(self, payment_id: str) -> dict:
        return await self._request("GET", f"/payments/{payment_id}")

    async def create_refund(
        self, *, payment_id: str, amount: Decimal, idempotence_key: str, description: str = ""
    ) -> dict:
        body: dict[str, Any] = {"payment_id": payment_id, "amount": fmt_amount(amount)}
        if description:
            body["description"] = description[:250]
        return await self._request("POST", "/refunds", json=body, idempotence_key=idempotence_key)

    async def me(self) -> dict:
        """Информация о магазине (используется для проверки ключей в диагностике)."""
        return await self._request("GET", "/me", retries=1)

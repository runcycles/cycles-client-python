"""Synchronous and asynchronous HTTP clients for the Cycles API."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from pydantic import BaseModel

from runcycles._constants import (
    API_KEY_HEADER,
    BALANCES_PATH,
    DECIDE_PATH,
    EVENTS_PATH,
    IDEMPOTENCY_KEY_HEADER,
    RESERVATIONS_PATH,
)
from runcycles.config import CyclesConfig
from runcycles.response import CyclesResponse

logger = logging.getLogger(__name__)


def _serialize_body(body: BaseModel | dict[str, Any] | Any) -> dict[str, Any]:
    """Convert a request body to a dict suitable for JSON serialization."""
    if isinstance(body, BaseModel):
        return body.model_dump(exclude_none=True)
    if isinstance(body, dict):
        return body
    raise TypeError(f"Unsupported body type: {type(body)}")


def _extract_idempotency_key(body: dict[str, Any]) -> str | None:
    """Extract idempotency_key from the request body for the header."""
    return body.get("idempotency_key")


class CyclesClient:
    """Synchronous Cycles API client.

    Usage::

        config = CyclesConfig(base_url="http://localhost:7878", api_key="your-key")
        with CyclesClient(config) as client:
            response = client.create_reservation(request)
    """

    def __init__(self, config: CyclesConfig) -> None:
        self._config = config
        self._http = httpx.Client(
            base_url=config.base_url,
            headers={API_KEY_HEADER: config.api_key},
            timeout=httpx.Timeout(connect=config.connect_timeout, read=config.read_timeout, write=5.0, pool=5.0),
        )

    def create_reservation(self, request: BaseModel | dict[str, Any]) -> CyclesResponse:
        return self._post(RESERVATIONS_PATH, request)

    def commit_reservation(self, reservation_id: str, request: BaseModel | dict[str, Any]) -> CyclesResponse:
        return self._post(f"{RESERVATIONS_PATH}/{reservation_id}/commit", request)

    def release_reservation(self, reservation_id: str, request: BaseModel | dict[str, Any]) -> CyclesResponse:
        return self._post(f"{RESERVATIONS_PATH}/{reservation_id}/release", request)

    def extend_reservation(self, reservation_id: str, request: BaseModel | dict[str, Any]) -> CyclesResponse:
        return self._post(f"{RESERVATIONS_PATH}/{reservation_id}/extend", request)

    def decide(self, request: BaseModel | dict[str, Any]) -> CyclesResponse:
        return self._post(DECIDE_PATH, request)

    def list_reservations(self, **query_params: str) -> CyclesResponse:
        return self._get(RESERVATIONS_PATH, query_params)

    def get_reservation(self, reservation_id: str) -> CyclesResponse:
        return self._get(f"{RESERVATIONS_PATH}/{reservation_id}")

    def get_balances(self, **query_params: str) -> CyclesResponse:
        return self._get(BALANCES_PATH, query_params)

    def create_event(self, request: BaseModel | dict[str, Any]) -> CyclesResponse:
        return self._post(EVENTS_PATH, request)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> CyclesClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _post(self, path: str, body: BaseModel | dict[str, Any]) -> CyclesResponse:
        try:
            data = _serialize_body(body)
            headers: dict[str, str] = {}
            idem_key = _extract_idempotency_key(data)
            if idem_key:
                headers[IDEMPOTENCY_KEY_HEADER] = idem_key

            resp = self._http.post(path, json=data, headers=headers)
            return self._handle_response(resp)
        except httpx.HTTPError as e:
            logger.error("Transport error on POST %s: %s", path, e)
            return CyclesResponse.transport_error(e)

    def _get(self, path: str, params: dict[str, str] | None = None) -> CyclesResponse:
        try:
            resp = self._http.get(path, params=params)
            return self._handle_response(resp)
        except httpx.HTTPError as e:
            logger.error("Transport error on GET %s: %s", path, e)
            return CyclesResponse.transport_error(e)

    @staticmethod
    def _handle_response(resp: httpx.Response) -> CyclesResponse:
        try:
            body = resp.json()
        except Exception:
            body = None

        if 200 <= resp.status_code < 300:
            return CyclesResponse.success(resp.status_code, body or {})
        else:
            error_msg = None
            if body and isinstance(body, dict):
                error_msg = body.get("message") or body.get("error")
            return CyclesResponse.http_error(resp.status_code, error_msg or resp.reason_phrase or "Unknown error", body)


class AsyncCyclesClient:
    """Asynchronous Cycles API client.

    Usage::

        config = CyclesConfig(base_url="http://localhost:7878", api_key="your-key")
        async with AsyncCyclesClient(config) as client:
            response = await client.create_reservation(request)
    """

    def __init__(self, config: CyclesConfig) -> None:
        self._config = config
        self._http = httpx.AsyncClient(
            base_url=config.base_url,
            headers={API_KEY_HEADER: config.api_key},
            timeout=httpx.Timeout(connect=config.connect_timeout, read=config.read_timeout, write=5.0, pool=5.0),
        )

    async def create_reservation(self, request: BaseModel | dict[str, Any]) -> CyclesResponse:
        return await self._post(RESERVATIONS_PATH, request)

    async def commit_reservation(self, reservation_id: str, request: BaseModel | dict[str, Any]) -> CyclesResponse:
        return await self._post(f"{RESERVATIONS_PATH}/{reservation_id}/commit", request)

    async def release_reservation(self, reservation_id: str, request: BaseModel | dict[str, Any]) -> CyclesResponse:
        return await self._post(f"{RESERVATIONS_PATH}/{reservation_id}/release", request)

    async def extend_reservation(self, reservation_id: str, request: BaseModel | dict[str, Any]) -> CyclesResponse:
        return await self._post(f"{RESERVATIONS_PATH}/{reservation_id}/extend", request)

    async def decide(self, request: BaseModel | dict[str, Any]) -> CyclesResponse:
        return await self._post(DECIDE_PATH, request)

    async def list_reservations(self, **query_params: str) -> CyclesResponse:
        return await self._get(RESERVATIONS_PATH, query_params)

    async def get_reservation(self, reservation_id: str) -> CyclesResponse:
        return await self._get(f"{RESERVATIONS_PATH}/{reservation_id}")

    async def get_balances(self, **query_params: str) -> CyclesResponse:
        return await self._get(BALANCES_PATH, query_params)

    async def create_event(self, request: BaseModel | dict[str, Any]) -> CyclesResponse:
        return await self._post(EVENTS_PATH, request)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> AsyncCyclesClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()

    async def _post(self, path: str, body: BaseModel | dict[str, Any]) -> CyclesResponse:
        try:
            data = _serialize_body(body)
            headers: dict[str, str] = {}
            idem_key = _extract_idempotency_key(data)
            if idem_key:
                headers[IDEMPOTENCY_KEY_HEADER] = idem_key

            resp = await self._http.post(path, json=data, headers=headers)
            return self._handle_response(resp)
        except httpx.HTTPError as e:
            logger.error("Transport error on POST %s: %s", path, e)
            return CyclesResponse.transport_error(e)

    async def _get(self, path: str, params: dict[str, str] | None = None) -> CyclesResponse:
        try:
            resp = await self._http.get(path, params=params)
            return self._handle_response(resp)
        except httpx.HTTPError as e:
            logger.error("Transport error on GET %s: %s", path, e)
            return CyclesResponse.transport_error(e)

    @staticmethod
    def _handle_response(resp: httpx.Response) -> CyclesResponse:
        try:
            body = resp.json()
        except Exception:
            body = None

        if 200 <= resp.status_code < 300:
            return CyclesResponse.success(resp.status_code, body or {})
        else:
            error_msg = None
            if body and isinstance(body, dict):
                error_msg = body.get("message") or body.get("error")
            return CyclesResponse.http_error(resp.status_code, error_msg or resp.reason_phrase or "Unknown error", body)

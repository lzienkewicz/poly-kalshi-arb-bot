"""
Async HTTP client for the Polymarket CLOB API.

Responsibilities
----------------
- Construct URLs and headers (optional Bearer auth)
- Execute GET requests with exponential-backoff retry
- Distinguish retryable errors (network failures, 429, 5xx) from fatal ones (4xx)
- Structured logging of every request, retry, and terminal error

What it does NOT do
-------------------
- Parse or interpret response bodies (that is _parser.py's job)
- Know anything about markets, orderbooks, or domain models
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

_RETRYABLE_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})
_DEFAULT_MAX_ATTEMPTS = 3
_BASE_DELAY_S = 1.0
_MAX_DELAY_S = 30.0


class PolymarketHTTPError(Exception):
    """Raised when the Polymarket CLOB API returns a non-retryable error response."""

    def __init__(self, status_code: int, body: str, url: str = "") -> None:
        super().__init__(f"HTTP {status_code} from {url!r}: {body[:200]}")
        self.status_code = status_code
        self.url = url


class PolymarketClient:
    """Async HTTP client for the Polymarket CLOB API.

    Usage (production)::

        async with PolymarketClient() as client:
            data = await client.get_markets()

    Usage (tests — inject a pre-configured AsyncClient)::

        async with httpx.AsyncClient(transport=respx_transport) as http:
            client = PolymarketClient(_http=http)
            data = await client.get_markets()
    """

    BASE_URL = "https://clob.polymarket.com"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_s: float = 10.0,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        _http: httpx.AsyncClient | None = None,  # test injection point
    ) -> None:
        self._api_key = api_key
        self._base_url = (base_url or self.BASE_URL).rstrip("/")
        self._timeout = httpx.Timeout(timeout_s)
        self._max_attempts = max_attempts
        self._injected_http = _http      # always used when provided (tests)
        self._owned_http: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Context-manager lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> PolymarketClient:
        if self._injected_http is None:
            self._owned_http = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owned_http is not None:
            await self._owned_http.aclose()
            self._owned_http = None

    # ------------------------------------------------------------------
    # Public API endpoints
    # ------------------------------------------------------------------

    async def get_markets(
        self,
        *,
        next_cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Fetch a page of markets from the CLOB /markets endpoint."""
        params: dict[str, Any] = {"limit": limit}
        if next_cursor:
            params["next_cursor"] = next_cursor
        return await self._get("/markets", params=params)

    async def get_market(self, condition_id: str) -> dict[str, Any]:
        """Fetch a single market by condition_id."""
        return await self._get(f"/markets/{condition_id}")

    async def get_book(self, token_id: str) -> dict[str, Any]:
        """Fetch the full order book for a token (YES or NO side of a market)."""
        return await self._get("/book", params={"token_id": token_id})

    # ------------------------------------------------------------------
    # Internal request + retry machinery
    # ------------------------------------------------------------------

    async def _get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self._base_url + path
        bound = log.bind(url=url, params=params)

        for attempt in range(1, self._max_attempts + 1):
            # ---- network-level errors --------------------------------
            try:
                response = await self._send(url, params=params)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.ConnectError) as exc:
                if attempt == self._max_attempts:
                    bound.error("polymarket_network_error_final", error=str(exc))
                    raise
                delay = _backoff(attempt)
                bound.warning(
                    "polymarket_network_error_retry",
                    attempt=attempt,
                    error=str(exc),
                    delay_s=delay,
                )
                await asyncio.sleep(delay)
                continue

            # ---- retryable HTTP errors --------------------------------
            if response.status_code in _RETRYABLE_STATUSES:
                if attempt == self._max_attempts:
                    bound.error("polymarket_http_error_final", status=response.status_code)
                    raise PolymarketHTTPError(response.status_code, response.text, url)
                delay = _retry_after(response, attempt)
                bound.warning(
                    "polymarket_http_error_retry",
                    attempt=attempt,
                    status=response.status_code,
                    delay_s=delay,
                )
                await asyncio.sleep(delay)
                continue

            # ---- non-retryable HTTP errors ----------------------------
            if response.status_code >= 400:
                bound.error(
                    "polymarket_http_error",
                    status=response.status_code,
                    body=response.text[:200],
                )
                raise PolymarketHTTPError(response.status_code, response.text, url)

            bound.debug("polymarket_ok", status=response.status_code, attempt=attempt)
            return response.json()

        # Unreachable — loop always raises or returns before exhaustion
        raise PolymarketHTTPError(0, "retry loop exhausted", url)  # pragma: no cover

    async def _send(
        self, url: str, *, params: dict[str, Any] | None
    ) -> httpx.Response:
        http = self._injected_http or self._owned_http
        if http is not None:
            return await http.get(
                url, headers=self._headers, params=params, timeout=self._timeout
            )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.get(url, headers=self._headers, params=params)

    @property
    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Accept": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h


# ---------------------------------------------------------------------------
# Backoff helpers
# ---------------------------------------------------------------------------

def _backoff(attempt: int) -> float:
    """Exponential backoff with ±50% jitter, capped at _MAX_DELAY_S."""
    base = min(_BASE_DELAY_S * (2 ** (attempt - 1)), _MAX_DELAY_S)
    return round(base * random.uniform(0.5, 1.5), 3)


def _retry_after(response: httpx.Response, attempt: int) -> float:
    """Parse Retry-After header if present; fall back to computed backoff."""
    header = response.headers.get("retry-after")
    if header:
        try:
            return min(float(header), _MAX_DELAY_S)
        except ValueError:
            pass
    return _backoff(attempt)

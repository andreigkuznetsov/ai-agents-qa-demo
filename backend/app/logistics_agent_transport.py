import asyncio
from dataclasses import dataclass, field
from math import isfinite
from typing import Literal

import httpx2

Method = Literal["GET", "POST"]


@dataclass(frozen=True, slots=True)
class WireResponse:
    status: int
    body: bytes = field(repr=False)


def trusted_origin(value: str) -> httpx2.URL:
    try:
        url = httpx2.URL(value)
    except httpx2.InvalidURL:
        raise ValueError("Invalid Simulator origin") from None
    if (
        url.scheme not in {"http", "https"}
        or not url.host
        or url.userinfo
        or url.raw_path != b"/"
        or url.fragment
    ):
        raise ValueError("Invalid Simulator origin")
    return url


class ToolHTTPTransport:
    def __init__(
        self,
        origin: httpx2.URL,
        timeout: float,
        test_transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        if not isfinite(timeout) or timeout <= 0:
            raise ValueError("Tool timeout must be positive and finite")
        self._timeout = timeout
        self._client = httpx2.AsyncClient(
            base_url=origin,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=test_transport
            if test_transport is not None
            else httpx2.AsyncHTTPTransport(retries=0, trust_env=False),
        )

    async def request(
        self,
        method: Method,
        path: str,
        bearer: str,
        key: bytes | None,
        payload: dict[str, str] | None,
    ) -> WireResponse | None:
        headers = [(b"authorization", b"Bearer " + bearer.encode("ascii"))]
        if key is not None:
            headers.append((b"idempotency-key", key))
        try:
            async with asyncio.timeout(self._timeout):
                response = await self._client.request(
                    method, path, headers=headers, json=payload
                )
        except (httpx2.RequestError, TimeoutError):
            return None
        return WireResponse(response.status_code, response.content)

    async def aclose(self) -> None:
        await self._client.aclose()

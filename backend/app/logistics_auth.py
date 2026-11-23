import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from uuid import UUID

from fastapi import Request
from fastapi.responses import JSONResponse


@dataclass(frozen=True)
class LogisticsContext:
    run_id: UUID
    client_id: str


class Unauthenticated(Exception):
    """The request did not present a recognized test credential."""


def logistics_auth(
    context_by_digest: Mapping[str, LogisticsContext],
) -> Callable[[Request], LogisticsContext]:
    trusted = MappingProxyType(dict(context_by_digest))

    def authenticate(request: Request) -> LogisticsContext:
        values = request.headers.getlist("authorization")
        if len(values) != 1:
            raise Unauthenticated
        parts = values[0].split()
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].isascii():
            raise Unauthenticated
        digest = hashlib.sha256(parts[1].encode("ascii")).hexdigest()
        context = trusted.get(digest)
        if context is None:
            raise Unauthenticated
        return context

    return authenticate


async def unauthenticated_response(_: Request, __: Exception) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": "UNAUTHENTICATED"}},
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )

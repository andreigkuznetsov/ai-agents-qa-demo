import hashlib
import re
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
        scheme, delimiter, token = values[0].strip(" \t").partition(" ")
        token = token.lstrip(" ")
        if (
            not delimiter
            or scheme.lower() != "bearer"
            or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token)
        ):
            raise Unauthenticated
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
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

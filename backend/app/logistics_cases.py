import asyncio
import logging
from collections.abc import Mapping
from datetime import date
from math import isfinite
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from app.case_store import OPERATION, CaseRejected, CaseStore, Operation
from app.logistics_auth import LogisticsContext, logistics_auth
from app.logistics_responses import error_response, not_found

POST_PATH = "/cases"
LIST_PATH = "/orders/{order_id}/cases"
CASE_PATH = "/cases/{case_id}"
MAX_IDEMPOTENCY_KEY_BYTES = 512
logger = logging.getLogger(__name__)


class CaseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    case_id: UUID
    order_id: str
    requested_date: date
    operation_type: Operation
    status: Literal["OPEN"]


class CaseListResponse(BaseModel):
    cases: list[CaseResponse]


def is_database_text(value: str) -> bool:
    return (
        bool(value)
        and "\x00" not in value
        and not any("\ud800" <= character <= "\udfff" for character in value)
    )


def read_idempotency_key(request: Request) -> str:
    values = [
        value
        for name, value in request.headers.raw
        if name.lower() == b"idempotency-key"
    ]
    if len(values) != 1 or len(values[0]) > MAX_IDEMPOTENCY_KEY_BYTES:
        raise ValueError("invalid idempotency key")
    key = values[0].decode("utf-8")
    if not key.strip() or any(
        (ord(character) < 32 and character != "\t") or character == "\x7f"
        for character in key
    ):
        raise ValueError("invalid idempotency key")
    return key


async def case_input(request: Request) -> tuple[str, date, str]:
    key = read_idempotency_key(request)
    payload = await request.json()
    if not isinstance(payload, dict) or set(payload) != {
        "order_id",
        "requested_date",
        "operation_type",
    }:
        raise ValueError("invalid case payload")
    order_id, day = payload["order_id"], payload["requested_date"]
    if (
        not isinstance(order_id, str)
        or not is_database_text(order_id)
        or not isinstance(day, str)
        or payload["operation_type"] != OPERATION
    ):
        raise ValueError("invalid case fields")
    requested_date = date.fromisoformat(day)
    if requested_date.isoformat() != day:
        raise ValueError("invalid date format")
    return order_id, requested_date, key


def case_router(
    context_by_digest: Mapping[str, LogisticsContext],
    write_timeout: float | None,
) -> APIRouter:
    if write_timeout is not None and (
        not isfinite(write_timeout) or write_timeout <= 0
    ):
        raise ValueError("case write timeout must be positive and finite")
    router = APIRouter()
    authenticate = logistics_auth(context_by_digest)

    @router.post(POST_PATH, response_model=CaseResponse, status_code=201)
    async def create_case(
        request: Request,
        response: Response,
        context: Annotated[LogisticsContext, Depends(authenticate)],
    ) -> CaseResponse | JSONResponse:
        try:
            order_id, requested_date, key = await case_input(request)
        except ValueError:
            return error_response("VALIDATION_ERROR", 422)
        if write_timeout is None:
            raise RuntimeError("case write budget is not configured")
        store: CaseStore = request.app.state.case_store
        deadline = asyncio.get_running_loop().time() + write_timeout
        try:
            case, created = await store.create(
                context, order_id, requested_date, key, deadline=deadline
            )
        except CaseRejected as rejection:
            if rejection.code == "RESOURCE_NOT_FOUND":
                return not_found(POST_PATH, logger)
            status = 422 if rejection.code == "DELIVERY_DATE_NOT_ALLOWED" else 409
            return error_response(rejection.code, status)
        response.status_code = 201 if created else 200
        return CaseResponse.model_validate(case)

    @router.get(LIST_PATH, response_model=CaseListResponse)
    async def read_case_list(
        order_id: str,
        request: Request,
        context: Annotated[LogisticsContext, Depends(authenticate)],
    ) -> CaseListResponse | JSONResponse:
        store: CaseStore = request.app.state.case_store
        cases = await store.list_for_order(context, order_id)
        if cases is None:
            return not_found(LIST_PATH, logger)
        return CaseListResponse(
            cases=[CaseResponse.model_validate(case) for case in cases]
        )

    @router.get(CASE_PATH, response_model=CaseResponse)
    async def read_case(
        case_id: str,
        request: Request,
        context: Annotated[LogisticsContext, Depends(authenticate)],
    ) -> CaseResponse | JSONResponse:
        try:
            uid = UUID(case_id)
        except ValueError:
            return not_found(CASE_PATH, logger)
        store: CaseStore = request.app.state.case_store
        case = await store.read(context, uid)
        if case is None:
            return not_found(CASE_PATH, logger)
        return CaseResponse.model_validate(case)

    return router

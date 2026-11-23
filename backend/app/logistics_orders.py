import logging
from collections.abc import Mapping
from datetime import date
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.logistics_auth import LogisticsContext, logistics_auth
from app.order_store import OrderStatus, OrderStore

ORDER_PATH = "/orders/{order_id}"
OPTIONS_PATH = f"{ORDER_PATH}/delivery-options"
logger = logging.getLogger(__name__)


class OrderResponse(BaseModel):
    order_id: str
    status: OrderStatus
    scheduled_delivery_date: date


class DeliveryOptionsResponse(BaseModel):
    order_id: str
    eligible: bool
    reason_code: Literal["ORDER_TRANSFER_BLOCKED", "NO_AVAILABLE_DATES"] | None
    dates: list[date]


def not_found(route_template: str) -> JSONResponse:
    code = "RESOURCE_NOT_FOUND"
    logger.info("route=%s code=%s correlation_id=%s", route_template, code, uuid4())
    return JSONResponse({"error": {"code": code}}, status_code=404)


def order_router(context_by_digest: Mapping[str, LogisticsContext]) -> APIRouter:
    router = APIRouter()
    authenticate = logistics_auth(context_by_digest)

    @router.get(ORDER_PATH, response_model=OrderResponse)
    async def read_order(
        order_id: str,
        request: Request,
        context: Annotated[LogisticsContext, Depends(authenticate)],
    ) -> OrderResponse | JSONResponse:
        store: OrderStore = request.app.state.order_store
        order = await store.read(context, order_id)
        if order is None:
            return not_found(ORDER_PATH)
        return OrderResponse(
            order_id=order.order_id,
            status=order.status,
            scheduled_delivery_date=order.scheduled_delivery_date,
        )

    @router.get(OPTIONS_PATH, response_model=DeliveryOptionsResponse)
    async def read_delivery_options(
        order_id: str,
        request: Request,
        context: Annotated[LogisticsContext, Depends(authenticate)],
    ) -> DeliveryOptionsResponse | JSONResponse:
        store: OrderStore = request.app.state.order_store
        options = await store.delivery_options(context, order_id)
        if options is None:
            return not_found(OPTIONS_PATH)
        reason: Literal["ORDER_TRANSFER_BLOCKED", "NO_AVAILABLE_DATES"] | None = None
        if options.status == "TRANSFER_BLOCKED":
            reason = "ORDER_TRANSFER_BLOCKED"
        elif not options.dates:
            reason = "NO_AVAILABLE_DATES"
        return DeliveryOptionsResponse(
            order_id=order_id,
            eligible=reason is None,
            reason_code=reason,
            dates=options.dates if reason is None else [],
        )

    return router

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from types import TracebackType
from typing import Annotated, Literal, Self
from urllib.parse import quote
from uuid import UUID

import httpx2
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from app.logistics_agent_transport import Method, ToolHTTPTransport, trusted_origin


@dataclass(frozen=True, slots=True)
class TrustedContext:
    run_id: UUID
    client_id: str
    bearer: str = field(repr=False)
    simulator_origin: str = field(repr=False)
    reference_year: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, UUID) or not self.client_id.strip():
            raise ValueError("Invalid trusted context")
        if not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", self.bearer):
            raise ValueError("Invalid trusted credential")
        trusted_origin(self.simulator_origin)


class Operation(StrEnum):
    GET_ORDER = "get_order"
    GET_DELIVERY_OPTIONS = "get_delivery_options"
    GET_ORDER_CASES = "get_order_cases"
    GET_CASE = "get_case"
    CREATE_CASE = "create_case"
    CHECK_CASE_RESULT = "check_case_result"


READ_OPERATIONS = frozenset(
    {
        Operation.GET_ORDER,
        Operation.GET_DELIVERY_OPTIONS,
        Operation.GET_ORDER_CASES,
        Operation.GET_CASE,
    }
)


def _order_segment(value: str) -> str:
    if (
        not value.strip()
        or value in {".", ".."}
        or any(c in "/\\?#%" or ord(c) < 32 or ord(c) == 127 for c in value)
        or any(0xD800 <= ord(c) <= 0xDFFF for c in value)
    ):
        raise ValueError("Invalid order identifier")
    return quote(value, safe="")


class OrderArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    order_id: str

    @field_validator("order_id")
    @classmethod
    def check_order_id(cls, value: str) -> str:
        _order_segment(value)
        return value


class CaseArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    case_id: str

    @field_validator("case_id")
    @classmethod
    def check_case_id(cls, value: str) -> str:
        parsed = UUID(value)
        if str(parsed) != value.lower():
            raise ValueError("Invalid case identifier")
        return str(parsed)


class CreateCaseArgs(OrderArgs):
    requested_date: str

    @field_validator("requested_date")
    @classmethod
    def check_date(cls, value: str) -> str:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError("Invalid requested date")
        return value


class Projection(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)


class OrderView(Projection):
    order_id: str
    status: Literal["ACTIVE", "TRANSFER_BLOCKED"]
    scheduled_delivery_date: date


class DeliveryOptionsView(Projection):
    order_id: str
    eligible: bool
    reason_code: Literal["ORDER_TRANSFER_BLOCKED", "NO_AVAILABLE_DATES"] | None
    dates: list[date]


class CaseView(Projection):
    case_id: UUID
    order_id: str
    requested_date: date
    operation_type: Literal["DELIVERY_RESCHEDULE"]
    status: Literal["OPEN"]

    @field_validator("order_id")
    @classmethod
    def check_order_id(cls, value: str) -> str:
        _order_segment(value)
        return value


class CasesView(Projection):
    cases: list[CaseView]


class CaseFound(Projection):
    outcome: Literal["FOUND"]
    case: CaseView


class CaseNotFound(Projection):
    outcome: Literal["NOT_FOUND"]
    case: None


class _ErrorBody(Projection):
    code: str


class _ErrorEnvelope(Projection):
    error: _ErrorBody


@dataclass(frozen=True, slots=True)
class ToolFailure:
    code: str
    status: int | None = None
    outcome_unknown: bool = False


@dataclass(frozen=True, slots=True)
class CreateCaseResult:
    status: Literal[200, 201]
    case: CaseView


ToolView = OrderView | DeliveryOptionsView | CasesView | CaseView
CaseResult = Annotated[CaseFound | CaseNotFound, Field(discriminator="outcome")]
_RESULT = TypeAdapter[CaseResult](CaseResult)
_ORDER = TypeAdapter(OrderView)
_OPTIONS = TypeAdapter(DeliveryOptionsView)
_CASES = TypeAdapter(CasesView)
_CASE = TypeAdapter(CaseView)

_ERROR_CODES = {
    401: frozenset({"UNAUTHENTICATED"}),
    404: frozenset({"RESOURCE_NOT_FOUND"}),
    409: frozenset({"ORDER_TRANSFER_BLOCKED", "IDEMPOTENCY_KEY_CONFLICT"}),
    422: frozenset({"DELIVERY_DATE_NOT_ALLOWED", "VALIDATION_ERROR"}),
    503: frozenset({"RESULT_CHECK_UNAVAILABLE"}),
}


def _key_bytes(value: str) -> bytes | None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or any((ord(c) < 32 and c != "\t") or ord(c) == 127 for c in value)
    ):
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        return None
    return encoded if len(encoded) <= 512 else None


def _contains_secret(value: object, secrets: tuple[str, ...]) -> bool:
    if isinstance(value, str):
        return any(secret in value for secret in secrets)
    if isinstance(value, (date, UUID)):
        return str(value) in secrets
    if isinstance(value, Mapping):
        # These field values are fixed by Literal validators, never copied as free text.
        public_enums = {"status", "reason_code", "operation_type", "outcome"}
        return any(
            _contains_secret(item, secrets)
            for name, item in value.items()
            if name not in public_enums
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_secret(item, secrets) for item in value)
    return False


class ToolAdapter:
    def __init__(
        self,
        context: TrustedContext,
        *,
        timeout: float,
        test_transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._context = context
        self._http = ToolHTTPTransport(
            trusted_origin(context.simulator_origin), timeout, test_transport
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._http.aclose()

    async def _request[T: Projection](
        self,
        method: Method,
        path: str,
        view: TypeAdapter[T],
        *,
        key: bytes | None = None,
        payload: dict[str, str] | None = None,
    ) -> tuple[int, T] | ToolFailure:
        secrets = (
            (self._context.bearer, key.decode("utf-8"))
            if key is not None
            else (self._context.bearer,)
        )
        response = await self._http.request(
            method, path, self._context.bearer, key, payload
        )
        if response is None:
            return ToolFailure("TRANSPORT_ERROR", outcome_unknown=method == "POST")

        status = response.status
        try:
            if status == 200 or (method == "POST" and status == 201):
                projected = view.validate_json(response.body)
                if _contains_secret(projected.model_dump(), secrets):
                    return self._unexpected(status, write=method == "POST")
                return status, projected
            allowed = {401, 404}
            if method == "POST":
                allowed.update({409, 422})
            elif key is not None:
                allowed.update({422, 503})
            if status in allowed:
                code = _ErrorEnvelope.model_validate_json(response.body).error.code
                if code in _ERROR_CODES[status]:
                    return ToolFailure(code, status)
        except ValidationError:
            pass
        return ToolFailure("UNEXPECTED_RESPONSE", status, method == "POST")

    @staticmethod
    def _unexpected(status: int, *, write: bool = False) -> ToolFailure:
        return ToolFailure("UNEXPECTED_RESPONSE", status, write)

    async def read(
        self, operation: str, arguments: Mapping[str, object]
    ) -> ToolView | ToolFailure:
        if not isinstance(operation, str) or operation not in READ_OPERATIONS:
            return ToolFailure("TOOL_NOT_ALLOWED")
        if not isinstance(arguments, Mapping):
            return ToolFailure("INVALID_ARGUMENTS")
        try:
            if operation == Operation.GET_CASE:
                case_id = CaseArgs.model_validate(arguments).case_id
                if self._context.bearer.lower() in case_id:
                    return ToolFailure("INVALID_ARGUMENTS")
                return await self._get_case(case_id)
            order = OrderArgs.model_validate(arguments).order_id
        except ValidationError:
            return ToolFailure("INVALID_ARGUMENTS")
        if self._context.bearer in order:
            return ToolFailure("INVALID_ARGUMENTS")

        path = f"/orders/{_order_segment(order)}"
        if operation == Operation.GET_ORDER:
            result = await self._request("GET", path, _ORDER)
            if isinstance(result, ToolFailure):
                return result
            status, value = result
            return value if value.order_id == order else self._unexpected(status)
        if operation == Operation.GET_DELIVERY_OPTIONS:
            options = await self._request("GET", path + "/delivery-options", _OPTIONS)
            if isinstance(options, ToolFailure):
                return options
            status, values = options
            coherent = values.eligible == bool(values.dates) and (
                values.eligible == (values.reason_code is None)
            )
            if values.order_id != order or not coherent:
                return self._unexpected(status)
            return values
        cases = await self._request("GET", path + "/cases", _CASES)
        if isinstance(cases, ToolFailure):
            return cases
        status, case_list = cases
        if any(case.order_id != order for case in case_list.cases):
            return self._unexpected(status)
        return case_list

    async def _get_case(self, case_id: str) -> CaseView | ToolFailure:
        result = await self._request("GET", f"/cases/{case_id}", _CASE)
        if isinstance(result, ToolFailure):
            return result
        status, case = result
        return case if str(case.case_id) == case_id else self._unexpected(status)

    async def create_case(
        self, arguments: Mapping[str, object], *, idempotency_key: str
    ) -> CreateCaseResult | ToolFailure:
        # A trusted caller supplies the existing operation key.
        if not isinstance(arguments, Mapping):
            return ToolFailure("INVALID_ARGUMENTS")
        try:
            args = CreateCaseArgs.model_validate(arguments)
        except ValidationError:
            return ToolFailure("INVALID_ARGUMENTS")
        key = _key_bytes(idempotency_key)
        if key is None or _contains_secret(
            (args.order_id, date.fromisoformat(args.requested_date)),
            (self._context.bearer, idempotency_key),
        ):
            return ToolFailure("INVALID_ARGUMENTS")
        payload = {
            "order_id": args.order_id,
            "requested_date": args.requested_date,
            "operation_type": "DELIVERY_RESCHEDULE",
        }
        result = await self._request("POST", "/cases", _CASE, key=key, payload=payload)
        if isinstance(result, ToolFailure):
            return result
        status, case = result
        if (
            case.order_id != args.order_id
            or case.requested_date.isoformat() != args.requested_date
        ):
            return self._unexpected(status, write=True)
        return CreateCaseResult(201 if status == 201 else 200, case)

    async def check_case_result(
        self, arguments: Mapping[str, object], *, idempotency_key: str
    ) -> CaseResult | ToolFailure:
        if not isinstance(arguments, Mapping):
            return ToolFailure("INVALID_ARGUMENTS")
        try:
            order = OrderArgs.model_validate(arguments).order_id
        except ValidationError:
            return ToolFailure("INVALID_ARGUMENTS")
        key = _key_bytes(idempotency_key)
        if key is None or _contains_secret(
            order, (self._context.bearer, idempotency_key)
        ):
            return ToolFailure("INVALID_ARGUMENTS")
        path = f"/orders/{_order_segment(order)}/case-result"
        result = await self._request("GET", path, _RESULT, key=key)
        if isinstance(result, ToolFailure):
            return result
        status, value = result
        if isinstance(value, CaseFound) and value.case.order_id != order:
            return self._unexpected(status)
        return value

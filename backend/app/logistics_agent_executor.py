from dataclasses import dataclass
from typing import Literal
from uuid import UUID

import httpx2

from app.logistics_agent_intent import RequestIntent, resolve_intent
from app.logistics_agent_journal import (
    JournalUnavailable,
    OperationJournal,
    OwnedOperation,
)
from app.logistics_agent_tools import (
    CasesView,
    DeliveryOptionsView,
    Operation,
    OrderView,
    ToolAdapter,
    ToolFailure,
    TrustedContext,
)

Outcome = Literal[
    "CLARIFY", "INFORMATION", "REJECTED", "CREATED", "EXISTING", "UNKNOWN", "BUSY"
]


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    outcome: Outcome
    code: str
    case_id: UUID | None = None
    order: OrderView | None = None


@dataclass(frozen=True, slots=True)
class OperationExecutor:
    journal: OperationJournal
    timeout: float

    async def execute(
        self,
        operation_id: UUID,
        context: TrustedContext,
        text: str,
        *,
        test_transport: httpx2.AsyncBaseTransport | None = None,
    ) -> ExecutionResult:
        try:
            with self.journal.ownership(operation_id, context) as owned:
                if owned is None:
                    return ExecutionResult("BUSY", "OPERATION_IN_PROGRESS")
                state = owned.read()
                if state.started:
                    return ExecutionResult("UNKNOWN", "OPERATION_ALREADY_STARTED")
                owned.start()
                intent = resolve_intent(text, context.reference_year)
                if intent is None:
                    return ExecutionResult("CLARIFY", "ORDER_OR_DATE_REQUIRED")
                async with ToolAdapter(
                    context, timeout=self.timeout, test_transport=test_transport
                ) as tools:
                    return await self._perform(tools, owned, intent)
        except JournalUnavailable:
            return ExecutionResult("UNKNOWN", "OPERATION_JOURNAL_UNAVAILABLE")

    @staticmethod
    async def _perform(
        tools: ToolAdapter, owned: OwnedOperation, intent: RequestIntent
    ) -> ExecutionResult:
        order_args = {"order_id": intent.order_id}
        order = await tools.read(Operation.GET_ORDER, order_args)
        if not isinstance(order, OrderView):
            return _read_failure(order)
        if intent.requested_date is None:
            return ExecutionResult("INFORMATION", "ORDER_READ", order=order)

        options = await tools.read(Operation.GET_DELIVERY_OPTIONS, order_args)
        if not isinstance(options, DeliveryOptionsView):
            return _read_failure(options)
        if (
            order.status == "TRANSFER_BLOCKED"
            or options.reason_code == "ORDER_TRANSFER_BLOCKED"
        ):
            return ExecutionResult("REJECTED", "ORDER_TRANSFER_BLOCKED")
        if not options.eligible:
            return ExecutionResult("REJECTED", "NO_AVAILABLE_DATES")
        if (
            intent.requires_unique_option
            and options.dates.count(intent.requested_date) != 1
        ):
            return ExecutionResult("CLARIFY", "DATE_REQUIRES_CLARIFICATION")
        if intent.requested_date not in options.dates:
            return ExecutionResult("REJECTED", "DELIVERY_DATE_NOT_ALLOWED")

        cases = await tools.read(Operation.GET_ORDER_CASES, order_args)
        if not isinstance(cases, CasesView):
            return _read_failure(cases)
        for case in cases.cases:
            if case.requested_date == intent.requested_date:
                return ExecutionResult("EXISTING", "OPEN_CASE_EXISTS", case.case_id)

        permit = owned.consume(intent.order_id, intent.requested_date)
        if permit is None:
            return ExecutionResult("UNKNOWN", "POST_PERMIT_NOT_CONFIRMED")
        result = await tools.create_case(
            {**order_args, "requested_date": intent.requested_date.isoformat()},
            idempotency_key=permit.key,
        )
        if isinstance(result, ToolFailure):
            if result.code != "INVALID_ARGUMENTS":
                owned.observe_handoff()
            if result.outcome_unknown:
                return ExecutionResult("UNKNOWN", result.code)
            return ExecutionResult("REJECTED", result.code)

        # This observation is recorded only after the adapter handed off a
        # validated request. A process exit in flight leaves it UNOBSERVED.
        owned.observe_handoff()
        owned.confirm(result.case.case_id, result.status)
        if result.status == 201:
            return ExecutionResult("CREATED", "CASE_CREATED", result.case.case_id)
        return ExecutionResult("EXISTING", "OPEN_CASE_EXISTS", result.case.case_id)


def _read_failure(response: object) -> ExecutionResult:
    if isinstance(response, ToolFailure):
        return ExecutionResult("REJECTED", response.code)
    return ExecutionResult("REJECTED", "UNEXPECTED_RESPONSE")

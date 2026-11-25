import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from time import monotonic_ns
from typing import Literal
from uuid import UUID, uuid4

import httpx2

from app.logistics_agent_events import (
    AttemptEvent,
    DiagnosticCode,
    EventSink,
    EvidenceContext,
    Phase,
    write_line,
)
from app.logistics_agent_events import (
    Outcome as EventOutcome,
)
from app.logistics_agent_intent import RequestIntent, resolve_intent
from app.logistics_agent_journal import (
    FOUND_EVIDENCE,
    JournalUnavailable,
    OperationJournal,
    OwnedOperation,
)
from app.logistics_agent_tools import (
    CaseFound,
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
    evidence_incomplete: bool = False
    diagnostics_unavailable: bool = False


@dataclass(frozen=True, slots=True)
class OperationExecutor:
    journal: OperationJournal
    timeout: float
    evidence: tuple[EventSink, EvidenceContext] | None = None

    async def execute(
        self,
        operation_id: UUID,
        context: TrustedContext,
        text: str,
        *,
        test_transport: httpx2.AsyncBaseTransport | None = None,
    ) -> ExecutionResult:
        if self.evidence is None:
            return await self._execute(
                operation_id, context, text, test_transport, lambda _: True, True
            )
        sink, evidence = self.evidence
        attempt = uuid4()
        started = monotonic_ns()
        incomplete = False

        def record(
            phase: Phase,
            outcome: Outcome | None = None,
            diagnostic: DiagnosticCode | None = None,
        ) -> bool:
            nonlocal incomplete
            if not incomplete:
                try:
                    sink.emit(
                        AttemptEvent(
                            evidence,
                            attempt,
                            operation_id,
                            context.run_id,
                            phase,
                            (monotonic_ns() - started) // 1_000_000,
                            EventOutcome(outcome) if outcome is not None else None,
                            diagnostic,
                        )
                    )
                except (OSError, ValueError):
                    incomplete = True
            return not incomplete

        admitted = record(Phase.STARTED)
        result = await self._execute(
            operation_id, context, text, test_transport, record, admitted
        )
        diagnostic = next(
            (code for code in DiagnosticCode if code == result.code), None
        )
        if record(Phase.FINISHED, result.outcome, diagnostic):
            return result
        try:
            write_line(
                sys.stderr,
                {
                    "code": "OPERATION_EVIDENCE_INCOMPLETE",
                    "operation_id": str(operation_id)
                    if type(operation_id) is UUID
                    else None,
                    "journal_available": (
                        None
                        if result.code == "EVENT_SINK_UNAVAILABLE"
                        else result.code != "OPERATION_JOURNAL_UNAVAILABLE"
                    ),
                    "event_sink_available": False,
                },
            )
        except (OSError, ValueError):
            return replace(
                result, evidence_incomplete=True, diagnostics_unavailable=True
            )
        return replace(result, evidence_incomplete=True)

    async def _execute(
        self,
        operation_id: UUID,
        context: TrustedContext,
        text: str,
        test_transport: httpx2.AsyncBaseTransport | None,
        record: Callable[[Phase], bool],
        admitted: bool,
    ) -> ExecutionResult:
        try:
            with self.journal.ownership(operation_id, context) as owned:
                if owned is None:
                    return ExecutionResult("BUSY", "OPERATION_IN_PROGRESS")
                state = owned.read()
                if state.case_id is not None and state.response_status is not None:
                    return _confirmed(state.case_id, state.response_status)
                if state.started:
                    return ExecutionResult("UNKNOWN", "OPERATION_ALREADY_STARTED")
                if not admitted:
                    return ExecutionResult("UNKNOWN", "EVENT_SINK_UNAVAILABLE")
                owned.start()
                intent = resolve_intent(text, context.reference_year)
                if intent is None:
                    return ExecutionResult("CLARIFY", "ORDER_OR_DATE_REQUIRED")
                async with ToolAdapter(
                    context, timeout=self.timeout, test_transport=test_transport
                ) as tools:
                    return await self._perform(tools, owned, intent, record)
        except JournalUnavailable:
            return ExecutionResult("UNKNOWN", "OPERATION_JOURNAL_UNAVAILABLE")

    @staticmethod
    async def _perform(
        tools: ToolAdapter,
        owned: OwnedOperation,
        intent: RequestIntent,
        record: Callable[[Phase], bool],
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
        if not record(Phase.PERMIT_CONSUMED):
            return ExecutionResult("UNKNOWN", "EVENT_SINK_UNAVAILABLE")
        result = await tools.create_case(
            {**order_args, "requested_date": intent.requested_date.isoformat()},
            idempotency_key=permit.key,
        )
        if isinstance(result, ToolFailure):
            if result.code != "INVALID_ARGUMENTS":
                owned.observe_handoff()
                record(Phase.POST_OBSERVED)
            if result.outcome_unknown:
                check = owned.consume_check()
                if check is not None and record(Phase.CHECK_CONSUMED):
                    reconciled = await tools.check_case_result(
                        order_args, idempotency_key=check.key
                    )
                    if isinstance(reconciled, CaseFound) and (
                        reconciled.case.requested_date == intent.requested_date
                    ):
                        owned.confirm(reconciled.case.case_id, FOUND_EVIDENCE)
                        return _confirmed(reconciled.case.case_id, FOUND_EVIDENCE)
                return ExecutionResult("UNKNOWN", result.code)
            return ExecutionResult("REJECTED", result.code)

        # This observation is recorded only after the adapter handed off a
        # validated request. A process exit in flight leaves it UNOBSERVED.
        owned.observe_handoff()
        record(Phase.POST_OBSERVED)
        owned.confirm(result.case.case_id, result.status)
        return _confirmed(result.case.case_id, result.status)


def _confirmed(case_id: UUID, evidence: int) -> ExecutionResult:
    if evidence == 201:
        return ExecutionResult("CREATED", "CASE_CREATED", case_id)
    if evidence in (200, FOUND_EVIDENCE):
        return ExecutionResult("EXISTING", "OPEN_CASE_EXISTS", case_id)
    raise JournalUnavailable


def _read_failure(response: object) -> ExecutionResult:
    if isinstance(response, ToolFailure):
        return ExecutionResult("REJECTED", response.code)
    return ExecutionResult("REJECTED", "UNEXPECTED_RESPONSE")

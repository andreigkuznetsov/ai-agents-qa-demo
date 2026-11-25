import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from time import monotonic, monotonic_ns
from typing import Literal
from uuid import UUID, uuid4

import httpx2

from app.logistics_agent_budget import (
    BudgetDenied,
    CallBudget,
    CallTimedOut,
    SyntheticLimits,
)
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
from app.logistics_agent_model import (
    Fact,
    IntentHint,
    ModelInput,
    ModelPort,
    ModelReply,
    ModelText,
    ModelUnavailable,
    Proposal,
    Step,
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


class ProposalRejected(Exception):
    pass


class ModelStopped(Exception):
    pass


@dataclass(frozen=True, slots=True)
class OperationExecutor:
    journal: OperationJournal
    timeout: float
    evidence: tuple[EventSink, EvidenceContext] | None = None
    model: ModelPort | None = None
    limits: SyntheticLimits | None = None
    clock: Callable[[], float] = monotonic

    async def execute(
        self,
        operation_id: UUID,
        context: TrustedContext,
        text: str,
        *,
        test_transport: httpx2.AsyncBaseTransport | None = None,
    ) -> ExecutionResult:
        budget = CallBudget(self.timeout, self.limits, self.clock)
        if self.evidence is None:
            return await self._execute(
                operation_id,
                context,
                text,
                test_transport,
                lambda _: True,
                True,
                budget,
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
                            budget.summary() if phase is Phase.FINISHED else None,
                        )
                    )
                except (OSError, ValueError):
                    incomplete = True
            return not incomplete

        admitted = record(Phase.STARTED)
        result = await self._execute(
            operation_id, context, text, test_transport, record, admitted, budget
        )
        diagnostic = next(
            (code for code in DiagnosticCode if code == result.code), None
        )
        if record(Phase.FINISHED, result.outcome, diagnostic):
            return result
        stderr = sys.stderr
        if stderr is None:
            return replace(
                result, evidence_incomplete=True, diagnostics_unavailable=True
            )
        try:
            write_line(
                stderr,
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
        budget: CallBudget,
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
                    try:
                        await self._propose(Step.CLARIFY, IntentHint.INCOMPLETE, budget)
                    except (
                        BudgetDenied,
                        ProposalRejected,
                        ModelUnavailable,
                        ModelStopped,
                        CallTimedOut,
                    ):
                        pass
                    return ExecutionResult("CLARIFY", "ORDER_OR_DATE_REQUIRED")
                async with ToolAdapter(
                    context, timeout=self.timeout, test_transport=test_transport
                ) as tools:
                    return await self._perform(tools, owned, intent, record, budget)
        except JournalUnavailable:
            return ExecutionResult("UNKNOWN", "OPERATION_JOURNAL_UNAVAILABLE")
        except BudgetDenied:
            return ExecutionResult("REJECTED", "EXECUTION_LIMIT_REACHED")
        except ProposalRejected:
            return ExecutionResult("REJECTED", "MODEL_PROPOSAL_REJECTED")
        except ModelStopped:
            return ExecutionResult("REJECTED", "MODEL_STOPPED")
        except (ModelUnavailable, CallTimedOut):
            return ExecutionResult("REJECTED", "CALL_UNAVAILABLE")

    async def _propose(
        self,
        step: Step,
        intent: IntentHint,
        budget: CallBudget,
        facts: tuple[Fact, ...] = (),
    ) -> None:
        model = self.model
        if model is None:
            return
        limits = budget.limits
        if limits is None:
            raise BudgetDenied
        request = ModelInput(
            step,
            intent,
            facts,
            max(1, int(budget.time_for("model") * 1000)),
            limits.max_input_tokens,
            limits.max_output_tokens,
        )
        response = await budget.call("model", lambda: model.propose(request))
        if type(response) is not ModelReply or type(response.proposals) is not tuple:
            raise ModelUnavailable
        valid_usage = budget.settle_usage(response.usage)
        proposals = response.proposals
        if not budget.count_proposals(len(proposals)):
            raise BudgetDenied
        if valid_usage and not proposals and type(response.text) is ModelText:
            raise ModelStopped
        if (
            valid_usage
            and response.text is None
            and len(proposals) == 1
            and request.allowed is not None
        ):
            proposal = proposals[0]
            if (
                type(proposal) is Proposal
                and type(proposal.tool) is str
                and proposal.tool == request.allowed.value
                and isinstance(proposal.arguments, Mapping)
                and set(proposal.arguments) == {"order_ref"}
                and type(proposal.arguments["order_ref"]) is str
                and proposal.arguments["order_ref"] == "current"
            ):
                return
        raise ProposalRejected

    async def _perform(
        self,
        tools: ToolAdapter,
        owned: OwnedOperation,
        intent: RequestIntent,
        record: Callable[[Phase], bool],
        budget: CallBudget,
    ) -> ExecutionResult:
        order_args = {"order_id": intent.order_id}
        prepaid = self.model is not None
        hint = (
            IntentHint.INFORMATION
            if intent.requested_date is None
            else IntentHint.RESCHEDULE
        )
        await self._propose(Step.ORDER, hint, budget)
        order = await budget.call(
            "read", lambda: tools.read(Operation.GET_ORDER, order_args), prepaid=prepaid
        )
        if not isinstance(order, OrderView):
            return _read_failure(order)
        if intent.requested_date is None:
            return ExecutionResult("INFORMATION", "ORDER_READ", order=order)

        facts: tuple[Fact, ...] = (
            Fact.ORDER_ACTIVE if order.status == "ACTIVE" else Fact.ORDER_BLOCKED,
        )
        await self._propose(Step.OPTIONS, hint, budget, facts)
        options = await budget.call(
            "read",
            lambda: tools.read(Operation.GET_DELIVERY_OPTIONS, order_args),
            prepaid=prepaid,
        )
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

        facts = (*facts, Fact.DATE_ALLOWED)
        await self._propose(Step.CASES, hint, budget, facts)
        cases = await budget.call(
            "read",
            lambda: tools.read(Operation.GET_ORDER_CASES, order_args),
            prepaid=prepaid,
        )
        if not isinstance(cases, CasesView):
            return _read_failure(cases)
        for case in cases.cases:
            if case.requested_date == intent.requested_date:
                return ExecutionResult("EXISTING", "OPEN_CASE_EXISTS", case.case_id)

        await self._propose(Step.WRITE, hint, budget, (*facts, Fact.NO_MATCHING_CASE))
        budget.require_post_reserve(prepaid=prepaid)
        permit = owned.consume(intent.order_id, intent.requested_date)
        if permit is None:
            return ExecutionResult("UNKNOWN", "POST_PERMIT_NOT_CONFIRMED")
        if not record(Phase.PERMIT_CONSUMED):
            return ExecutionResult("UNKNOWN", "EVENT_SINK_UNAVAILABLE")
        post_args = {**order_args, "requested_date": intent.requested_date.isoformat()}
        try:
            result = await budget.call(
                "post",
                lambda: tools.create_case(post_args, idempotency_key=permit.key),
                prepaid=prepaid,
            )
        except BudgetDenied:
            return ExecutionResult("UNKNOWN", "POST_PERMIT_CONSUMED_WITHOUT_HANDOFF")
        except CallTimedOut:
            result = ToolFailure("POST_WAIT_DEADLINE", outcome_unknown=True)
        if isinstance(result, ToolFailure):
            if result.code != "INVALID_ARGUMENTS":
                owned.observe_handoff()
                record(Phase.POST_OBSERVED)
            if result.outcome_unknown:

                async def reconcile() -> object:
                    check = owned.consume_check()
                    if check is not None and record(Phase.CHECK_CONSUMED):
                        budget.time_for("check")
                        return await tools.check_case_result(
                            order_args, idempotency_key=check.key
                        )
                    return None

                try:
                    reconciled = await budget.call("check", reconcile)
                except (BudgetDenied, CallTimedOut):
                    return ExecutionResult("UNKNOWN", result.code)
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

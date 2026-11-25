import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.logistics_agent_budget import Usage
from app.logistics_agent_tools import Operation


class Step(StrEnum):
    CLARIFY = "clarify"
    ORDER = "order"
    OPTIONS = "options"
    CASES = "cases"
    WRITE = "write"


class IntentHint(StrEnum):
    INCOMPLETE = "incomplete"
    INFORMATION = "information"
    RESCHEDULE = "reschedule"


class Fact(StrEnum):
    ORDER_ACTIVE = "order_active"
    ORDER_BLOCKED = "order_blocked"
    DATE_ALLOWED = "date_allowed"
    NO_MATCHING_CASE = "no_matching_case"


class ModelText(StrEnum):
    ASK_FOR_DETAILS = "ask_for_details"
    STOP = "stop"


_TOOLS = {
    Step.CLARIFY: None,
    Step.ORDER: Operation.GET_ORDER,
    Step.OPTIONS: Operation.GET_DELIVERY_OPTIONS,
    Step.CASES: Operation.GET_ORDER_CASES,
    Step.WRITE: Operation.CREATE_CASE,
}


@dataclass(frozen=True, slots=True)
class ModelInput:
    step: Step
    intent: IntentHint
    facts: tuple[Fact, ...]
    deadline_remaining_ms: int
    max_input_tokens: int
    max_output_tokens: int

    def __post_init__(self) -> None:
        if (
            type(self.step) is not Step
            or type(self.intent) is not IntentHint
            or type(self.facts) is not tuple
            or any(type(fact) is not Fact for fact in self.facts)
            or any(
                type(value) is not int or value <= 0
                for value in (
                    self.deadline_remaining_ms,
                    self.max_input_tokens,
                    self.max_output_tokens,
                )
            )
        ):
            raise ValueError("INVALID_MODEL_INPUT")

    @property
    def allowed(self) -> Operation | None:
        return _TOOLS[self.step]

    def serialize(self) -> bytes:
        tool = _TOOLS[self.step]
        return json.dumps(
            {
                "step": self.step.value,
                "dialogue": {"intent": self.intent.value},
                "facts": [fact.value for fact in self.facts],
                "deadline_remaining_ms": self.deadline_remaining_ms,
                "token_limits": {
                    "input": self.max_input_tokens,
                    "output": self.max_output_tokens,
                },
                "tools": []
                if tool is None
                else [
                    {
                        "name": tool.value,
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "order_ref": {"type": "string", "const": "current"}
                            },
                            "required": ["order_ref"],
                            "additionalProperties": False,
                        },
                    }
                ],
            },
            separators=(",", ":"),
        ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class Proposal:
    tool: str
    arguments: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ModelReply:
    proposals: tuple[Proposal, ...]
    usage: Usage | None
    text: ModelText | None = None


class ModelUnavailable(Exception):
    """The model call failed without a usable response."""


class ModelPort(Protocol):
    async def propose(self, request: ModelInput) -> ModelReply: ...


class ScenarioModel:
    """A scripted port with a local serialization boundary and no external I/O."""

    def __init__(self, replies: Iterable[ModelReply]) -> None:
        self._replies = iter(replies)
        self.inputs: list[ModelInput] = []
        self.payloads: list[bytes] = []
        self.outputs: list[ModelReply] = []

    async def propose(self, request: ModelInput) -> ModelReply:
        self.inputs.append(request)
        self.payloads.append(request.serialize())
        try:
            reply = next(self._replies)
        except StopIteration:
            raise ModelUnavailable from None
        self.outputs.append(reply)
        return reply

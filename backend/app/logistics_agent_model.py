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
    write_requested: bool

    def __post_init__(self) -> None:
        if type(self.step) is not Step or type(self.write_requested) is not bool:
            raise ValueError("INVALID_MODEL_INPUT")

    @property
    def allowed(self) -> Operation | None:
        return _TOOLS[self.step]

    def serialize(self) -> bytes:
        tool = _TOOLS[self.step]
        return json.dumps(
            {
                "step": self.step.value,
                "write_requested": self.write_requested,
                "tools": []
                if tool is None
                else [{"name": tool.value, "arguments": {"order_ref": "current"}}],
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

    async def propose(self, request: ModelInput) -> ModelReply:
        self.inputs.append(request)
        self.payloads.append(request.serialize())
        try:
            return next(self._replies)
        except StopIteration:
            raise ModelUnavailable from None

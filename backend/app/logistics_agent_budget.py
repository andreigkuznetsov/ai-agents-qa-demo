import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic
from typing import Literal

Call = Literal["model", "read", "post", "check"]


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class SyntheticLimits:
    total_seconds: float
    model_seconds: float
    check_seconds: float
    finish_seconds: float
    cost_limit: int
    max_input_tokens: int
    max_output_tokens: int
    input_rate: int
    output_rate: int

    def __post_init__(self) -> None:
        times = (
            self.total_seconds,
            self.model_seconds,
            self.check_seconds,
            self.finish_seconds,
        )
        costs = (
            self.cost_limit,
            self.max_input_tokens,
            self.max_output_tokens,
            self.input_rate,
            self.output_rate,
        )
        if any(not math.isfinite(value) or value <= 0 for value in times) or any(
            type(value) is not int or value <= 0 for value in costs
        ):
            raise ValueError("Invalid synthetic execution limits")

    @property
    def max_charge(self) -> int:
        return (
            self.max_input_tokens * self.input_rate
            + self.max_output_tokens * self.output_rate
        )


class BudgetDenied(Exception):
    """A call was blocked before reaching its external port."""


class CallTimedOut(Exception):
    """The wait for an admitted call exceeded its budget."""


@dataclass(slots=True)
class CallCounts:
    model: int = 0
    tools: int = 0
    posts: int = 0
    checks: int = 0


@dataclass(frozen=True, slots=True)
class BudgetSummary:
    model_calls: int
    tool_attempts: int
    post_attempts: int
    result_checks: int
    known_cost: int
    held_cost: int


class CallBudget:
    def __init__(
        self,
        http_timeout: float,
        limits: SyntheticLimits | None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.counts = CallCounts()
        self.limits = limits
        self.clock = clock
        self.deadline = clock() + limits.total_seconds if limits else math.inf
        self.http_timeout = http_timeout
        self.reserved = 0
        self.measured = 0

    def summary(self) -> BudgetSummary | None:
        if self.limits is None:
            return None
        return BudgetSummary(
            self.counts.model,
            self.counts.tools,
            self.counts.posts,
            self.counts.checks,
            self.measured,
            self.reserved,
        )

    def count_proposals(self, count: int) -> bool:
        available = 6 - self.counts.tools
        self.counts.tools += min(count, available)
        return count <= available

    def require_post_reserve(self, *, prepaid: bool = False) -> None:
        if (
            self.counts.posts >= 1
            or self.counts.checks >= 1
            or self.counts.tools > (5 if prepaid else 4)
        ):
            raise BudgetDenied
        self.time_for("post")

    def time_for(self, kind: Call) -> float:
        limits = self.limits
        if limits is None:
            return self.http_timeout
        remaining = self.deadline - self.clock() - limits.finish_seconds
        if kind == "post":
            remaining -= limits.check_seconds
        ceiling = {
            "model": limits.model_seconds,
            "check": min(self.http_timeout, limits.check_seconds),
            "post": self.http_timeout,
            "read": self.http_timeout,
        }[kind]
        timeout = min(remaining, ceiling)
        if timeout <= 0:
            raise BudgetDenied
        return timeout

    async def call[T](
        self, kind: Call, invoke: Callable[[], Awaitable[T]], *, prepaid: bool = False
    ) -> T:
        if kind == "model":
            limits = self.limits
            if (
                self.counts.model >= 6
                or limits is None
                or self.measured + self.reserved + limits.max_charge > limits.cost_limit
            ):
                raise BudgetDenied
            timeout = self.time_for(kind)
            self.counts.model += 1
            self.reserved += limits.max_charge
        else:
            if not prepaid and self.counts.tools >= 6:
                raise BudgetDenied
            if kind == "post":
                self.require_post_reserve(prepaid=prepaid)
            if kind == "check" and self.counts.checks >= 1:
                raise BudgetDenied
            timeout = self.time_for(kind)
            if not prepaid:
                self.counts.tools += 1
            if kind == "post":
                self.counts.posts += 1
            elif kind == "check":
                self.counts.checks += 1
        if self.limits is None:
            # The trusted 2A/2B harness uses the existing adapter timeout.
            return await invoke()
        try:
            async with asyncio.timeout(timeout):
                return await invoke()
        except TimeoutError:
            # TimeoutError is an OSError. Keep it distinct from journal I/O.
            raise CallTimedOut from None

    def settle_usage(self, usage: Usage | None) -> bool:
        limits = self.limits
        if usage is None or limits is None:
            return True
        if (
            type(usage) is not Usage
            or type(usage.input_tokens) is not int
            or type(usage.output_tokens) is not int
            or not 0 <= usage.input_tokens <= limits.max_input_tokens
            or not 0 <= usage.output_tokens <= limits.max_output_tokens
        ):
            return False
        self.reserved -= limits.max_charge
        self.measured += (
            usage.input_tokens * limits.input_rate
            + usage.output_tokens * limits.output_rate
        )
        return True

from collections.abc import Awaitable, Callable
from typing import Literal

from app.logistics_auth import LogisticsContext

FaultPhase = Literal["before_commit", "after_commit", "before_result_read"]
FaultHook = Callable[[LogisticsContext, FaultPhase], Awaitable[None]]


class ResultCheckUnavailable(Exception):
    """The trusted test harness could not complete a result check."""


async def no_fault(context: LogisticsContext, phase: FaultPhase) -> None:
    """The production dependency has no fault profiles."""

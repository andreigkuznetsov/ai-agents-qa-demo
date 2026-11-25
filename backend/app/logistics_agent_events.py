import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, TextIO
from uuid import UUID


class Phase(StrEnum):
    STARTED = "STARTED"
    PERMIT_CONSUMED = "PERMIT_CONSUMED"
    POST_OBSERVED = "POST_OBSERVED"
    CHECK_CONSUMED = "CHECK_CONSUMED"
    FINISHED = "FINISHED"


class Outcome(StrEnum):
    CLARIFY = "CLARIFY"
    INFORMATION = "INFORMATION"
    REJECTED = "REJECTED"
    CREATED = "CREATED"
    EXISTING = "EXISTING"
    UNKNOWN = "UNKNOWN"
    BUSY = "BUSY"


class DiagnosticCode(StrEnum):
    JOURNAL_UNAVAILABLE = "OPERATION_JOURNAL_UNAVAILABLE"
    PERMIT_NOT_CONFIRMED = "POST_PERMIT_NOT_CONFIRMED"


@dataclass(frozen=True, slots=True)
class EvidenceContext:
    series_id: UUID
    commit_sha: str

    def __post_init__(self) -> None:
        if type(self.series_id) is not UUID or not re.fullmatch(
            r"[a-f0-9]{40}", self.commit_sha
        ):
            raise ValueError("INVALID_EVIDENCE_CONTEXT")


@dataclass(frozen=True, slots=True)
class AttemptEvent:
    context: EvidenceContext
    attempt_id: UUID
    operation_id: UUID
    run_id: UUID
    phase: Phase
    elapsed_ms: int
    outcome: Outcome | None = None
    diagnostic: DiagnosticCode | None = None


class EventSink(Protocol):
    def emit(self, event: AttemptEvent) -> None: ...


@dataclass(slots=True)
class JsonlEventSink:
    """The trusted caller opens an independent append stream for its series."""

    stream: TextIO
    _available: bool = field(default=True, init=False)

    def emit(self, event: AttemptEvent) -> None:
        if not self._available:
            raise OSError("EVENT_SINK_UNAVAILABLE")
        if (
            any(
                type(value) is not UUID
                for value in (event.attempt_id, event.operation_id, event.run_id)
            )
            or not isinstance(event.phase, Phase)
            or (event.outcome is not None and not isinstance(event.outcome, Outcome))
            or (
                event.diagnostic is not None
                and not isinstance(event.diagnostic, DiagnosticCode)
            )
            or type(event.elapsed_ms) is not int
            or event.elapsed_ms < 0
        ):
            raise ValueError("INVALID_ATTEMPT_EVENT")
        row: dict[str, str | bool | int | None] = {
            "version": 1,
            "commit_sha": event.context.commit_sha,
            "series_id": str(event.context.series_id),
            "attempt_id": str(event.attempt_id),
            "operation_id": str(event.operation_id),
            "run_id": str(event.run_id),
            "phase": event.phase.value,
            "outcome": event.outcome.value if event.outcome is not None else None,
            "diagnostic": event.diagnostic.value
            if event.diagnostic is not None
            else None,
            "utc": datetime.now(UTC).isoformat(),
            "elapsed_ms": event.elapsed_ms,
            "usage_source": "UNKNOWN",
        }
        try:
            write_line(self.stream, row)
        except (OSError, ValueError):
            self._available = False
            raise


def write_line(stream: TextIO, row: dict[str, str | bool | int | None]) -> None:
    line = json.dumps(row, separators=(",", ":")) + "\n"
    if stream.write(line) != len(line):
        raise OSError("INCOMPLETE_EVIDENCE_WRITE")
    stream.flush()


def unfinished_attempts(events: Iterable[AttemptEvent]) -> frozenset[UUID]:
    pending: set[UUID] = set()
    for event in events:
        if event.phase is Phase.STARTED:
            pending.add(event.attempt_id)
        elif event.phase is Phase.FINISHED:
            pending.discard(event.attempt_id)
    return frozenset(pending)

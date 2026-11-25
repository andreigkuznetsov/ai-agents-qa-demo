import secrets
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from uuid import UUID

from app.logistics_agent_tools import TrustedContext

if sys.platform == "win32":
    from app import logistics_agent_storage_win as storage
else:
    from app import logistics_agent_storage_posix as storage

_DB = "operations.sqlite3"
_SCHEMA_VERSION = 2
FOUND_EVIDENCE = 1
_APP_ROOT = Path(__file__).resolve().parents[1]
_PROJECT_ROOT = (
    _APP_ROOT.parent if (_APP_ROOT.parent / "openspec").is_dir() else _APP_ROOT
)
_SCHEMA = """
CREATE TABLE operations (
    operation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    started INTEGER NOT NULL DEFAULT 0 CHECK (started IN (0, 1)),
    order_id TEXT,
    requested_date TEXT,
    operation_type TEXT,
    idempotency_key TEXT UNIQUE,
    transport_observed TEXT NOT NULL DEFAULT 'UNOBSERVED'
        CHECK (transport_observed IN ('UNOBSERVED', 'HANDED_OFF')),
    result_check_consumed INTEGER NOT NULL DEFAULT 0 CHECK (result_check_consumed IN (0, 1)),
    case_id TEXT,
    evidence_kind INTEGER CHECK (evidence_kind IN (1, 200, 201)),
    CHECK (result_check_consumed = 0 OR transport_observed = 'HANDED_OFF'),
    CHECK (evidence_kind IS NOT 1 OR result_check_consumed = 1),
    CHECK ((case_id IS NULL) = (evidence_kind IS NULL)),
    CHECK (
        (idempotency_key IS NULL AND order_id IS NULL AND requested_date IS NULL
            AND operation_type IS NULL AND transport_observed = 'UNOBSERVED' AND case_id IS NULL)
        OR (idempotency_key IS NOT NULL AND order_id IS NOT NULL AND requested_date IS NOT NULL
            AND operation_type = 'DELIVERY_RESCHEDULE')
    )
) STRICT
"""


class JournalUnavailable(Exception):
    def __init__(self) -> None:
        super().__init__("OPERATION_JOURNAL_UNAVAILABLE")


def _case_id(value: str | None) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(value)
    except ValueError:
        raise JournalUnavailable from None


@dataclass(frozen=True, slots=True)
class Permit:
    key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class OperationState:
    started: bool
    consumed: bool
    handed_off: bool
    case_id: UUID | None
    response_status: int | None
    checked: bool


def lock_filename(operation_id: UUID) -> str:
    if type(operation_id) is not UUID:
        raise JournalUnavailable
    return f"{operation_id.hex}.lock"


@dataclass(frozen=True, slots=True)
class OperationJournal:
    path: Path

    def __post_init__(self) -> None:
        path = self.path
        if (
            not path.is_absolute()
            or path == Path(path.anchor)
            or ".." in path.parts
            or path.is_relative_to(_PROJECT_ROOT)
            or any(part.casefold().startswith("onedrive") for part in path.parts[1:])
            or any(":" in part for part in path.parts[1:])
        ):
            raise JournalUnavailable

    @contextmanager
    def _directory(self) -> Iterator[int]:
        try:
            with storage.directory(self.path) as directory:
                yield directory
        except OSError:
            raise JournalUnavailable from None

    @contextmanager
    def _connect(self, directory: int) -> Iterator[sqlite3.Connection]:
        try:
            with storage.file_handle(directory, self.path / _DB):
                pass
            for suffix in ("-journal", "-wal", "-shm"):
                try:
                    with storage.file_handle(directory, self.path / (_DB + suffix)):
                        if suffix != "-journal":
                            raise JournalUnavailable
                except FileNotFoundError:
                    continue
            uri = (self.path / _DB).as_uri() + "?mode=rw"
            with closing(
                sqlite3.connect(uri, uri=True, timeout=0, autocommit=True)
            ) as db:
                if db.execute("PRAGMA journal_mode").fetchone() != ("delete",):
                    raise JournalUnavailable
                db.execute("PRAGMA synchronous=EXTRA")
                if db.execute("PRAGMA synchronous").fetchone() != (3,):
                    raise JournalUnavailable
                yield db
        except (OSError, sqlite3.Error, ValueError):
            raise JournalUnavailable from None

    @contextmanager
    def _transaction(self, directory: int) -> Iterator[sqlite3.Connection]:
        with self._connect(directory) as db:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
            # If the body or commit fails, closing the connection rolls back any
            # active transaction. Callers receive no permit from an uncertain commit.

    def prepare(self, operation_id: UUID, context: TrustedContext) -> None:
        """Called only during trusted preparation of a new operation, never on delivery."""
        lock_filename(operation_id)
        with self._directory() as directory:
            try:
                with storage.file_handle(directory, self.path / _DB, create=True):
                    pass
            except OSError:
                raise JournalUnavailable from None
            with self._transaction(directory) as db:
                version = db.execute("PRAGMA user_version").fetchone()
                if version == (0,):
                    if db.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone():
                        raise JournalUnavailable
                    db.execute(_SCHEMA)
                    db.execute("PRAGMA user_version=2")
                self._require_version(db)
                db.execute(
                    "INSERT INTO operations (operation_id, run_id, client_id) VALUES (?, ?, ?)",
                    (str(operation_id), str(context.run_id), context.client_id),
                )

    @staticmethod
    def _require_version(db: sqlite3.Connection) -> None:
        if db.execute("PRAGMA user_version").fetchone() != (_SCHEMA_VERSION,):
            raise JournalUnavailable

    @contextmanager
    def ownership(
        self, operation_id: UUID, context: TrustedContext
    ) -> Iterator["OwnedOperation | None"]:
        lock_path = self.path / lock_filename(operation_id)
        with self._directory() as directory:
            # Opening a lock file before a read is safe. The SQLite database and its
            # operation row are never implicitly recreated on delivery.
            with storage.file_handle(directory, lock_path, create=True) as handle:
                if storage.lock(handle):
                    owned = OwnedOperation(self, directory, operation_id, context)
                    try:
                        yield owned
                    finally:
                        owned._active = False
                else:
                    yield None


@dataclass(slots=True)
class OwnedOperation:
    journal: OperationJournal
    directory: int
    operation_id: UUID
    context: TrustedContext

    _active: bool = field(default=True, init=False, repr=False)

    def _identity(self) -> tuple[str, str, str]:
        if not self._active:
            raise JournalUnavailable
        return str(self.operation_id), str(self.context.run_id), self.context.client_id

    def read(self) -> OperationState:
        with self.journal._connect(self.directory) as db:
            self.journal._require_version(db)
            row = db.execute(
                "SELECT started, idempotency_key IS NOT NULL, transport_observed, case_id, evidence_kind, result_check_consumed "
                "FROM operations WHERE operation_id=? AND run_id=? AND client_id=?",
                self._identity(),
            ).fetchone()
            if row is None:
                raise JournalUnavailable
            return OperationState(
                bool(row[0]),
                bool(row[1]),
                row[2] == "HANDED_OFF",
                _case_id(row[3]),
                row[4],
                bool(row[5]),
            )

    def consume(self, order_id: str, requested_date: date) -> Permit | None:
        key = secrets.token_urlsafe(32)
        with self.journal._transaction(self.directory) as db:
            self.journal._require_version(db)
            changed = db.execute(
                "UPDATE operations SET order_id=?, requested_date=?, "
                "operation_type='DELIVERY_RESCHEDULE', idempotency_key=? "
                "WHERE operation_id=? AND run_id=? AND client_id=? AND started=1 AND idempotency_key IS NULL",
                (order_id, requested_date.isoformat(), key, *self._identity()),
            ).rowcount
        return Permit(key) if changed == 1 else None

    def observe_handoff(self) -> None:
        with self.journal._transaction(self.directory) as db:
            self.journal._require_version(db)
            changed = db.execute(
                "UPDATE operations SET transport_observed='HANDED_OFF' "
                "WHERE operation_id=? AND run_id=? AND client_id=? "
                "AND idempotency_key IS NOT NULL AND transport_observed='UNOBSERVED'",
                self._identity(),
            ).rowcount
            if changed != 1:
                raise JournalUnavailable

    def consume_check(self) -> Permit | None:
        with self.journal._transaction(self.directory) as db:
            self.journal._require_version(db)
            row = db.execute(
                "UPDATE operations SET result_check_consumed=1 "
                "WHERE operation_id=? AND run_id=? AND client_id=? "
                "AND transport_observed='HANDED_OFF' AND result_check_consumed=0 "
                "AND case_id IS NULL RETURNING idempotency_key",
                self._identity(),
            ).fetchone()
        return Permit(row[0]) if row is not None else None

    def confirm(self, case_id: UUID, status: int) -> None:
        with self.journal._transaction(self.directory) as db:
            self.journal._require_version(db)
            changed = db.execute(
                "UPDATE operations SET case_id=?, evidence_kind=? "
                "WHERE operation_id=? AND run_id=? AND client_id=? "
                "AND transport_observed='HANDED_OFF' AND case_id IS NULL",
                (str(case_id), status, *self._identity()),
            ).rowcount
            if changed != 1:
                raise JournalUnavailable

    def start(self) -> None:
        with self.journal._transaction(self.directory) as db:
            self.journal._require_version(db)
            changed = db.execute(
                "UPDATE operations SET started=1 "
                "WHERE operation_id=? AND run_id=? AND client_id=? AND started=0",
                self._identity(),
            ).rowcount
            if changed != 1:
                raise JournalUnavailable

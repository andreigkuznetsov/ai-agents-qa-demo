from __future__ import annotations

import asyncio
import multiprocessing
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from multiprocessing.connection import Connection
from multiprocessing.context import SpawnProcess
from pathlib import Path
from uuid import UUID, uuid4

import httpx2
import pytest
from kt4_executor_support import private_journal_dir as private_journal_dir
from test_kt4_executor_core import CASE_ID, DAY, ORDER, TEXT, context, simulator_handler

from app.logistics_agent_executor import ExecutionResult, OperationExecutor
from app.logistics_agent_journal import (
    JournalUnavailable,
    OperationJournal,
    lock_filename,
)
from app.logistics_agent_tools import TrustedContext

if sys.platform == "win32":
    from multiprocessing.connection import PipeConnection as ParentConnection
else:
    from multiprocessing.connection import Connection as ParentConnection


def test_kt4_journal_transaction_and_reopen(private_journal_dir: Path) -> None:
    journal = OperationJournal(private_journal_dir)
    trusted, other = context(), context()
    operation, other_operation = uuid4(), uuid4()
    journal.prepare(operation, trusted)
    journal.prepare(other_operation, other)
    with journal.ownership(operation, trusted) as owned:
        assert owned is not None
        with journal._connect(owned.directory) as db:
            assert db.execute("PRAGMA journal_mode").fetchone() == ("delete",)
            assert db.execute("PRAGMA synchronous").fetchone() == (3,)
        pre = owned.read()
        assert not pre.started and not pre.consumed and not pre.handed_off
        owned.start()
        permit = owned.consume(ORDER, DAY)
        assert permit is not None
        if not permit.key.isascii() or len(permit.key) > 512:
            pytest.fail("Unexpected private key format", pytrace=False)
        if permit.key in repr(permit):
            pytest.fail(
                "Private key appeared in the permit representation", pytrace=False
            )
        assert owned.consume(ORDER, DAY) is None
        assert not owned.read().handed_off
    with OperationJournal(private_journal_dir).ownership(
        operation, trusted
    ) as reopened:
        assert reopened is not None
        current = reopened.read()
        assert current.consumed and not current.handed_off and current.case_id is None
        reopened.observe_handoff()
        assert reopened.read().handed_off and reopened.read().case_id is None
        reopened.confirm(CASE_ID, 201)
    with journal.ownership(operation, other) as different:
        assert different is not None
        with pytest.raises(JournalUnavailable, match="OPERATION_JOURNAL_UNAVAILABLE"):
            different.read()
    with journal.ownership(other_operation, other) as unrelated:
        assert unrelated is not None and not unrelated.read().consumed
    with journal.ownership(operation, trusted) as final:
        assert final is not None and final.read().case_id == CASE_ID
    with closing(sqlite3.connect(private_journal_dir / "operations.sqlite3")) as db:
        query = "SELECT idempotency_key, order_id FROM operations WHERE operation_id=?"
        if db.execute(query, (str(operation),)).fetchone() != (permit.key, ORDER):
            pytest.fail("Private permit was not persisted consistently", pytrace=False)
        assert db.execute(query, (str(other_operation),)).fetchone() == (None, None)


def _lock_process(
    path: Path,
    operation: UUID,
    trusted: TrustedContext,
    channel: Connection,
    hold: bool,
) -> None:
    try:
        with OperationJournal(path).ownership(operation, trusted) as owned:
            if owned is None:
                channel.send("busy")
                return
            state = owned.read()
            if hold:
                owned.start()
                assert owned.consume(ORDER, DAY) is not None
                channel.send("held")
                channel.recv()
            else:
                channel.send((state.consumed, state.handed_off, state.case_id))
    finally:
        channel.close()


@pytest.mark.parametrize("terminate", [False, True])
def test_kt4_lock_two_processes(private_journal_dir: Path, terminate: bool) -> None:
    launcher = multiprocessing.get_context("spawn")
    trusted, operation = context(), uuid4()
    journal = OperationJournal(private_journal_dir)
    journal.prepare(operation, trusted)

    def start(hold: bool) -> tuple[SpawnProcess, ParentConnection]:
        parent, child = launcher.Pipe()
        process = launcher.Process(
            target=_lock_process,
            args=(private_journal_dir, operation, trusted, child, hold),
        )
        process.start()
        child.close()
        return process, parent

    first, release = start(True)
    try:
        assert release.poll(12) and release.recv() == "held"
        second, report = start(False)
        try:
            assert report.poll(8) and report.recv() == "busy"
            second.join(4)
            assert second.exitcode == 0
        finally:
            if second.is_alive():
                second.kill()
                second.join(4)
            report.close()
        if terminate:
            first.kill()
        else:
            release.send("release")
        first.join(8)
        assert first.exitcode is not None and (first.exitcode != 0) == terminate
        third, report = start(False)
        try:
            assert report.poll(8) and report.recv() == (True, False, None)
            third.join(4)
            assert third.exitcode == 0
        finally:
            if third.is_alive():
                third.kill()
                third.join(4)
            report.close()
        calls: list[tuple[str, str]] = []
        result = asyncio.run(
            OperationExecutor(journal, 2).execute(
                operation,
                trusted,
                TEXT,
                test_transport=httpx2.MockTransport(simulator_handler(calls)),
            )
        )
        assert result == ExecutionResult("UNKNOWN", "OPERATION_ALREADY_STARTED")
        assert not calls
        assert (private_journal_dir / lock_filename(operation)).is_file()
    finally:
        if first.is_alive():
            first.kill()
            first.join(4)
        release.close()


def test_kt4_rejects_invalid_paths_and_unsafe_artifacts(
    private_journal_dir: Path,
) -> None:
    trusted, operation = context(), uuid4()
    with pytest.raises(JournalUnavailable):
        OperationJournal(Path("relative"))
    with pytest.raises(JournalUnavailable):
        OperationJournal(private_journal_dir / ".." / "private")
    with pytest.raises(JournalUnavailable):
        lock_filename("../bad")  # type: ignore[arg-type]
    with pytest.raises(JournalUnavailable):
        OperationJournal(private_journal_dir / "OneDrive-false" / "private")
    journal = OperationJournal(private_journal_dir)
    journal.prepare(operation, trusted)
    database = private_journal_dir / "operations.sqlite3"
    extra = private_journal_dir / "hard-link"
    os.link(database, extra)
    with journal.ownership(operation, trusted) as owned:
        assert owned is not None
        with pytest.raises(JournalUnavailable):
            owned.read()
    extra.unlink()
    outsider = private_journal_dir / "operations.sqlite3-wal"
    outsider.touch(mode=0o600)
    with journal.ownership(operation, trusted) as owned:
        assert owned is not None
        with pytest.raises(JournalUnavailable):
            owned.read()
    outsider.unlink()

    if sys.platform == "win32":
        junction = private_journal_dir.parent / "junction"
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(private_journal_dir)],
            check=True,
            capture_output=True,
            timeout=10,
        )
        try:
            with pytest.raises(JournalUnavailable):
                OperationJournal(junction).prepare(uuid4(), trusted)
        finally:
            junction.rmdir()
        subprocess.run(
            ["icacls", str(private_journal_dir), "/grant", "*S-1-1-0:(RX)"],
            check=True,
            capture_output=True,
            timeout=10,
        )
        try:
            with pytest.raises(JournalUnavailable):
                journal.prepare(uuid4(), trusted)
        finally:
            subprocess.run(
                ["icacls", str(private_journal_dir), "/remove:g", "*S-1-1-0"],
                check=True,
                capture_output=True,
                timeout=10,
            )
    else:
        link = private_journal_dir.parent / "symlink"
        link.symlink_to(private_journal_dir, target_is_directory=True)
        with pytest.raises(JournalUnavailable):
            OperationJournal(link).prepare(uuid4(), trusted)
        private_journal_dir.chmod(0o755)
        try:
            with pytest.raises(JournalUnavailable):
                journal.prepare(uuid4(), trusted)
        finally:
            private_journal_dir.chmod(0o700)


def test_kt4_rejects_lost_or_incompatible_journal(private_journal_dir: Path) -> None:
    trusted, operation = context(), uuid4()
    journal = OperationJournal(private_journal_dir)
    calls: list[tuple[str, str]] = []

    def run() -> ExecutionResult:
        return asyncio.run(
            OperationExecutor(journal, 2).execute(
                operation,
                trusted,
                TEXT,
                test_transport=httpx2.MockTransport(simulator_handler(calls)),
            )
        )

    assert run().code == "OPERATION_JOURNAL_UNAVAILABLE"
    database = private_journal_dir / "operations.sqlite3"
    assert not database.exists()
    journal.prepare(operation, trusted)
    with closing(sqlite3.connect(database)) as db:
        db.execute("PRAGMA user_version=99")
    assert run().code == "OPERATION_JOURNAL_UNAVAILABLE"
    assert not calls

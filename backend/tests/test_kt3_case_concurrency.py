import asyncio
import hashlib
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import httpx2 as httpx
import pytest
from conftest import Project
from sqlalchemy.ext.asyncio import AsyncConnection
from test_kt3_case_contract import PAYLOAD
from test_kt3_fixtures import CLIENT_B, SAME_ORDER, create_run, initialize

from app.case_store import INSERT_KEY, CaseStore
from app.logistics_auth import LogisticsContext
from app.logistics_locks import order_lock
from app.main import create_app

TEST_TOKEN = secrets.token_urlsafe(32)
AUTHORIZATION = {"Authorization": f"Bearer {TEST_TOKEN}"}


@asynccontextmanager
async def running_app(
    project: Project,
    monkeypatch: pytest.MonkeyPatch,
    run: UUID,
    *,
    statement_timeout: int = 2000,
) -> AsyncIterator[tuple[httpx.AsyncClient, CaseStore]]:
    project.api_env(monkeypatch)
    monkeypatch.setenv("QA_SQL_TIMEOUT_MS", str(statement_timeout))
    context = LogisticsContext(run, CLIENT_B)
    digest = hashlib.sha256(TEST_TOKEN.encode()).hexdigest()
    application = create_app({digest: context}, case_write_timeout=4)
    async with (
        application.router.lifespan_context(application),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application, raise_app_exceptions=False),
            base_url="http://synthetic-test",
        ) as client,
    ):
        yield client, application.state.case_store


async def post_case(
    client: httpx.AsyncClient, key: str, *, requested_date: str = "2026-11-26"
) -> httpx.Response:
    return await client.post(
        "/cases",
        headers={**AUTHORIZATION, "Idempotency-Key": key},
        json={**PAYLOAD, "requested_date": requested_date},
    )


def case_rows(
    project: Project, run: UUID
) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    with project.db() as observer:
        observer.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        cases = observer.execute(
            "SELECT case_id,requested_date FROM qa.las_cases WHERE run_id=%s AND client_id=%s ORDER BY case_id",
            (run, CLIENT_B),
        ).fetchall()
        keys = observer.execute(
            "SELECT request_key,case_id FROM qa.las_idempotency_keys WHERE run_id=%s AND client_id=%s ORDER BY request_key",
            (run, CLIENT_B),
        ).fetchall()
        return cases, keys


async def wait_for_advisory_block(project: Project) -> tuple[int, int]:
    loop = asyncio.get_running_loop()
    end = loop.time() + 1.5
    with project.db() as observer:
        observer.autocommit = True
        while loop.time() < end:
            row = observer.execute("""
                SELECT a.pid, pg_blocking_pids(a.pid)[1]
                FROM pg_stat_activity AS a
                JOIN pg_locks AS locks ON locks.pid=a.pid
                WHERE a.application_name='qa-api' AND locks.locktype='advisory'
                  AND NOT locks.granted AND cardinality(pg_blocking_pids(a.pid)) > 0
            """).fetchone()
            if row:
                waiter, holder = row
                assert (
                    isinstance(waiter, int)
                    and isinstance(holder, int)
                    and waiter != holder
                )
                return waiter, holder
            await asyncio.sleep(0.02)
    raise AssertionError("PostgreSQL did not report advisory lock contention")


def update_conditions(writer: Any, run: UUID, field: str) -> None:
    args = (run, CLIENT_B, SAME_ORDER)
    if field == "status":
        writer.execute(
            "UPDATE qa.las_orders SET status='TRANSFER_BLOCKED' WHERE (run_id,client_id,order_id)=(%s,%s,%s)",
            args,
        )
    else:
        writer.execute(
            "DELETE FROM qa.las_delivery_dates WHERE (run_id,client_id,order_id)=(%s,%s,%s)",
            args,
        )


def test_case_competing_posts(
    migrated_db: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        for second_key, second_date, expected in (
            ("first", "2026-11-26", 200),
            ("second", "2026-11-26", 200),
            ("first", "2026-11-27", 409),
        ):
            run = create_run(migrated_db)
            assert initialize(migrated_db, run)
            paused, release = asyncio.Event(), asyncio.Event()
            execute = AsyncConnection.execute

            async def hold_first(
                conn: AsyncConnection, statement: Any, params: Any = None, **kw: Any
            ) -> Any:
                result = await execute(conn, statement, params, **kw)
                if statement is INSERT_KEY and params["request_key"] == "first":
                    paused.set()
                    async with asyncio.timeout(3):
                        await release.wait()
                return result

            with monkeypatch.context() as patch:
                patch.setattr(AsyncConnection, "execute", hold_first)
                async with running_app(migrated_db, patch, run) as (client, _):
                    first = asyncio.create_task(post_case(client, "first"))
                    second = None
                    try:
                        async with asyncio.timeout(3):
                            await paused.wait()
                        second = asyncio.create_task(
                            post_case(client, second_key, requested_date=second_date)
                        )
                        await wait_for_advisory_block(migrated_db)
                    finally:
                        release.set()
                        responses = await asyncio.gather(
                            *([first, second] if second is not None else [first])
                        )
            initial, replay = responses
            assert initial.status_code == 201
            assert replay.status_code == expected
            if expected == 200:
                assert replay.json() == initial.json()
            else:
                assert replay.json() == {"error": {"code": "IDEMPOTENCY_KEY_CONFLICT"}}
            cases, keys = case_rows(migrated_db, run)
            assert len(cases) == 1
            assert keys == [(key, cases[0][0]) for key in sorted({"first", second_key})]

    asyncio.run(exercise())


def test_case_and_internal_change_both_orders(
    migrated_db: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        for field, failure in (("status", 409), ("date", 422)):
            for writer_first in (True, False):
                run = create_run(migrated_db)
                assert initialize(migrated_db, run)
                key = order_lock(run, CLIENT_B, SAME_ORDER)
                ready, release = asyncio.Event(), asyncio.Event()
                execute = AsyncConnection.execute

                async def hold_post(
                    conn: AsyncConnection, statement: Any, params: Any = None, **kw: Any
                ) -> Any:
                    result = await execute(conn, statement, params, **kw)
                    if statement is INSERT_KEY:
                        ready.set()
                        async with asyncio.timeout(3):
                            await release.wait()
                    return result

                def write() -> None:
                    with migrated_db.db("migrate") as writer:
                        writer.execute("SET LOCAL lock_timeout='3s'")
                        writer.execute(
                            "SELECT pg_advisory_xact_lock(%s::bigint)", (key,)
                        )
                        update_conditions(writer, run, field)

                with monkeypatch.context() as patch:
                    if not writer_first:
                        patch.setattr(AsyncConnection, "execute", hold_post)
                    async with running_app(migrated_db, patch, run) as (client, _):
                        if writer_first:
                            pending = None
                            try:
                                with migrated_db.db("migrate") as writer:
                                    writer.execute(
                                        "SELECT pg_advisory_xact_lock(%s::bigint)",
                                        (key,),
                                    )
                                    pending = asyncio.create_task(
                                        post_case(client, "candidate")
                                    )
                                    await wait_for_advisory_block(migrated_db)
                                    update_conditions(writer, run, field)
                            finally:
                                if pending is not None:
                                    response = await pending
                            assert response.status_code == failure
                            assert case_rows(migrated_db, run) == ([], [])
                        else:
                            post = asyncio.create_task(post_case(client, "candidate"))
                            writer_task = None
                            try:
                                async with asyncio.timeout(3):
                                    await ready.wait()
                                writer_task = asyncio.create_task(
                                    asyncio.to_thread(write)
                                )
                                loop = asyncio.get_running_loop()
                                until = loop.time() + 1.5
                                with migrated_db.db() as observer:
                                    observer.autocommit = True
                                    while loop.time() < until:
                                        if observer.execute("""
                                            SELECT EXISTS (SELECT 1 FROM pg_stat_activity a
                                            JOIN pg_locks l ON l.pid=a.pid
                                            WHERE a.usename='qa_migrate' AND l.locktype='advisory'
                                              AND NOT l.granted AND cardinality(pg_blocking_pids(a.pid))>0)
                                        """).fetchone() == (True,):
                                            break
                                        await asyncio.sleep(0.02)
                                    else:
                                        pytest.fail(
                                            "internal writer did not wait on the case order lock"
                                        )
                            finally:
                                release.set()
                                response = await post
                                if writer_task is not None:
                                    await writer_task
                            assert response.status_code == 201
                            cases, keys = case_rows(migrated_db, run)
                            assert len(cases) == 1 and keys == [
                                ("candidate", cases[0][0])
                            ]
                            options = await client.get(
                                f"/orders/{SAME_ORDER}/delivery-options",
                                headers=AUTHORIZATION,
                            )
                            assert (
                                options.status_code == 200
                                and options.json()["eligible"] is False
                            )

    asyncio.run(exercise())

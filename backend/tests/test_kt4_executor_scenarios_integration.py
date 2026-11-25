import asyncio
import hashlib
from pathlib import Path
from time import perf_counter
from uuid import UUID, uuid4

import httpx2
import pytest
from conftest import Project
from kt4_executor_support import make_private_journal_dir
from test_kt3_fixtures import CLIENT_A, CLIENT_B, SAME_ORDER
from test_kt4_executor_core import DAY, TEXT
from test_kt4_executor_policy import LIMITS, STEPS, reply
from test_kt4_executor_reconciliation_integration import DropPostResponse
from test_kt4_executor_recovery_integration import ObserveHTTP, new_fixture
from test_kt4_tool_adapter_integration import case_ids, scheduled_date, serve_loopback

from app.logistics_agent_executor import OperationExecutor
from app.logistics_agent_journal import OperationJournal
from app.logistics_agent_model import ModelReply, ScenarioModel
from app.logistics_agent_tools import Operation, TrustedContext
from app.logistics_auth import LogisticsContext
from app.logistics_faults import FaultPhase
from app.main import create_app

ORDER_READS = [
    ("GET", "/orders/order-001"),
    ("GET", "/orders/order-001/delivery-options"),
    ("GET", "/orders/order-001/cases"),
]
POST = ("POST", "/cases")
CHECK = ("GET", "/orders/order-001/case-result")


def test_s01_to_s06_with_scripted_model_and_postgres(
    migrated_db: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = perf_counter()
    migrated_db.api_env(monkeypatch)
    matrix = {
        "S01": (CLIENT_B, TEXT, STEPS, [*ORDER_READS, POST], "CREATED", "CASE_CREATED"),
        "S02-missing": (
            CLIENT_B,
            TEXT.replace("order-001", ""),
            (Operation.GET_ORDER,),
            [],
            "CLARIFY",
            "ORDER_OR_DATE_REQUIRED",
        ),
        "S02-partial": (
            CLIENT_B,
            TEXT.replace("order-001", "order-0"),
            (Operation.GET_ORDER,),
            [],
            "CLARIFY",
            "ORDER_OR_DATE_REQUIRED",
        ),
        "S02-information": (
            CLIENT_B,
            "Покажите заказ order-001",
            (Operation.GET_ORDER,),
            ORDER_READS[:1],
            "INFORMATION",
            "ORDER_READ",
        ),
        "S03-blocked": (
            CLIENT_A,
            TEXT.replace("order-001", "order-blocked"),
            STEPS[:2],
            [
                ("GET", "/orders/order-blocked"),
                ("GET", "/orders/order-blocked/delivery-options"),
            ],
            "REJECTED",
            "ORDER_TRANSFER_BLOCKED",
        ),
        "S03-date": (
            CLIENT_B,
            TEXT.replace("26 ноября", "2026-11-27"),
            STEPS[:2],
            ORDER_READS[:2],
            "REJECTED",
            "DELIVERY_DATE_NOT_ALLOWED",
        ),
        "S04": (
            CLIENT_A,
            TEXT,
            STEPS[:3],
            ORDER_READS,
            "EXISTING",
            "OPEN_CASE_EXISTS",
        ),
        "S05-after": (
            CLIENT_B,
            TEXT,
            STEPS,
            [*ORDER_READS, POST, CHECK],
            "EXISTING",
            "OPEN_CASE_EXISTS",
        ),
        "S05-before": (
            CLIENT_B,
            TEXT,
            STEPS,
            [*ORDER_READS, POST, CHECK],
            "UNKNOWN",
            "TRANSPORT_ERROR",
        ),
        "S06-other": (
            CLIENT_B,
            TEXT.replace("order-001", "order-blocked"),
            (Operation.GET_ORDER,),
            [("GET", "/orders/order-blocked")],
            "REJECTED",
            "RESOURCE_NOT_FOUND",
        ),
        "S06-batch": (
            CLIENT_B,
            TEXT,
            (Operation.GET_ORDER, Operation.CREATE_CASE),
            [],
            "REJECTED",
            "MODEL_PROPOSAL_REJECTED",
        ),
    }
    runs = {name: new_fixture(migrated_db) for name in matrix}
    tokens = {name: uuid4().hex for name in matrix}
    auth = {
        hashlib.sha256(tokens[name].encode()).hexdigest(): LogisticsContext(
            run, matrix[name][0]
        )
        for name, run in runs.items()
    }
    journal = OperationJournal(make_private_journal_dir(tmp_path))
    assert len(set(runs.values())) == len(matrix)

    def key_count(run: UUID) -> int:
        with migrated_db.db() as db:
            row = db.execute(
                "SELECT count(*) FROM qa.las_idempotency_keys WHERE run_id=%s", (run,)
            ).fetchone()
        assert row is not None and isinstance(row[0], int)
        return row[0]

    async def exercise() -> None:
        held, release = asyncio.Event(), asyncio.Event()

        async def faults(context: LogisticsContext, phase: FaultPhase) -> None:
            if context.run_id == runs["S05-before"] and phase == "before_commit":
                held.set()
                await release.wait()

        simulator = create_app(
            auth, case_write_timeout=8, case_result_timeout=4, case_fault=faults
        )
        wire = ObserveHTTP(simulator)
        async with serve_loopback(wire) as origin:
            for name, (
                client,
                message,
                tools,
                expected_http,
                outcome,
                code,
            ) in matrix.items():
                wire.calls.clear()
                run = runs[name]
                context = TrustedContext(run, client, tokens[name], origin, 2026)
                before = case_ids(migrated_db, run, client)
                other_client = CLIENT_A if client == CLIENT_B else CLIENT_B
                other_before = case_ids(migrated_db, run, other_client)
                date_before = scheduled_date(migrated_db, run, client)
                keys_before = key_count(run)

                replies = [reply(tool.value) for tool in tools]
                if name == "S06-batch":
                    replies = [
                        ModelReply(tuple(item.proposals[0] for item in replies), None)
                    ]
                model = ScenarioModel(replies)
                executor = OperationExecutor(journal, 5, model=model, limits=LIMITS)
                operation = uuid4()
                journal.prepare(operation, context)
                lost: DropPostResponse | None = None
                transport: httpx2.AsyncBaseTransport | None = None
                if name.startswith("S05"):
                    lost = DropPostResponse(
                        wire, held if name == "S05-before" else None
                    )
                    transport = lost
                elif name != "S01":
                    transport = httpx2.ASGITransport(app=wire)
                try:
                    result = await executor.execute(
                        operation, context, message, test_transport=transport
                    )
                    assert (result.outcome, result.code) == (outcome, code), name
                    if name == "S05-before":
                        assert (
                            lost is not None
                            and lost.pending is not None
                            and not lost.pending.done()
                        )
                        assert case_ids(migrated_db, run, client) == before
                        assert key_count(run) == keys_before
                    if name.startswith("S05"):
                        wire.check_context()
                        observations = (len(wire.calls), len(model.inputs))
                        replay = await executor.execute(
                            operation, context, message, test_transport=transport
                        )
                        assert (len(wire.calls), len(model.inputs)) == observations
                        assert replay.outcome == result.outcome
                        assert replay.case_id == result.case_id
                finally:
                    if lost is not None and lost.pending is not None:
                        release.set()
                        assert (
                            await asyncio.wait_for(lost.pending, 8)
                        ).status_code == 201
                actual_http = [(method, path) for method, path, *_ in wire.calls]
                assert actual_http == expected_http, name
                after = case_ids(migrated_db, run, client)
                writes = int(POST in expected_http)
                assert len(after - before) == writes and before <= after
                assert key_count(run) == keys_before + writes
                assert case_ids(migrated_db, run, other_client) == other_before
                assert scheduled_date(migrated_db, run, client) == date_before
                if result.case_id is not None:
                    assert result.case_id in after
                    with migrated_db.db() as db:
                        row = db.execute(
                            "SELECT order_id, requested_date, status FROM qa.las_cases "
                            "WHERE run_id=%s AND client_id=%s AND case_id=%s",
                            (run, client, result.case_id),
                        ).fetchone()
                    assert row == (SAME_ORDER, DAY, "OPEN")
                assert len(model.inputs) == len(replies)
                print(
                    f"{name} transport={'TCP' if transport is None else 'ASGI'} "
                    f"proposals={','.join(tool.value for tool in tools)} "
                    f"http={actual_http} result={outcome}/{code} "
                    f"case_delta={writes} key_delta={writes} other_client_unchanged=true"
                )

    asyncio.run(exercise())
    print(f"scenario_group_seconds={perf_counter() - started:.3f}")

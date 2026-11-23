import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from uuid import UUID

from psycopg import Connection
from psycopg.pq import TransactionStatus

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "logistics" / "v1.json"
FIXTURE_VERSION = "v1"


class FixtureError(ValueError):
    """A sanitized failure during trusted test initialization."""


@dataclass(frozen=True)
class Order:
    client: str
    order_id: str
    status: str
    scheduled: date
    options: tuple[date, ...]


@dataclass(frozen=True)
class Bundle:
    digest: str
    clients: tuple[str, ...]
    orders: tuple[Order, ...]


def _object(value: object, fields: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or value.keys() != fields:
        raise FixtureError("invalid fixture object")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise FixtureError("duplicate fixture field")
    return result


def _values(value: object) -> list[object]:
    if not isinstance(value, list):
        raise FixtureError("invalid fixture list")
    return value


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise FixtureError("invalid fixture identifier")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise FixtureError("invalid fixture identifier") from None
    return value


def _date(value: object) -> date:
    value = _identifier(value)
    try:
        result = date.fromisoformat(value)
    except ValueError:
        raise FixtureError("invalid fixture date") from None
    if result.isoformat() != value:
        raise FixtureError("invalid fixture date")
    return result


def read_bundle(path: Path) -> Bundle:
    try:
        raw = path.read_bytes()
        if b"\r" in raw or raw.startswith(b"\xef\xbb\xbf"):
            raise FixtureError("fixture requires UTF-8 and LF")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise FixtureError("cannot read fixture bundle") from None
    data = _object(value, {"clients", "orders", "initial_case_s04"})
    clients = tuple(_identifier(client) for client in _values(data["clients"]))
    if not clients or len(clients) != len(set(clients)):
        raise FixtureError("missing or duplicate fixture client")
    orders: dict[tuple[str, str], Order] = {}
    for value in _values(data["orders"]):
        item = _object(
            value,
            {
                "client_id",
                "order_id",
                "status",
                "scheduled_delivery_date",
                "delivery_options",
            },
        )
        client = _identifier(item["client_id"])
        order_id = _identifier(item["order_id"])
        status = _identifier(item["status"])
        options = tuple(_date(option) for option in _values(item["delivery_options"]))
        if client not in clients or (client, order_id) in orders:
            raise FixtureError("invalid or duplicate fixture order")
        if status not in {"ACTIVE", "TRANSFER_BLOCKED"} or len(set(options)) != len(
            options
        ):
            raise FixtureError("invalid fixture order state or delivery options")
        orders[client, order_id] = Order(
            client, order_id, status, _date(item["scheduled_delivery_date"]), options
        )
    case = _object(
        data["initial_case_s04"],
        {"client_id", "order_id", "requested_date", "operation_type", "status"},
    )
    order = orders.get((_identifier(case["client_id"]), _identifier(case["order_id"])))
    if (
        order is None
        or order.status != "ACTIVE"
        or _date(case["requested_date"]) not in order.options
        or case["operation_type"] != "DELIVERY_RESCHEDULE"
        or case["status"] != "OPEN"
    ):
        raise FixtureError("invalid S04 fixture description")
    return Bundle(hashlib.sha256(raw).hexdigest(), clients, tuple(orders.values()))


def _lock_key(namespace: int, payload: bytes) -> int:
    digest = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")
    return (namespace << 61) | (digest >> 3)


def initialization_lock(run_id: UUID) -> int:
    return _lock_key(3, run_id.bytes)


def order_lock(run_id: UUID, client: str, order_id: str) -> int:
    encoded = tuple(field.encode("utf-8") for field in (client, order_id))
    return _lock_key(
        2,
        run_id.bytes
        + b"".join(len(field).to_bytes(4, "big") + field for field in encoded),
    )


def load_fixture(
    conn: Connection[tuple[object, ...]], run_id: UUID, version: str, path: Path
) -> bool:
    """Initialize a disposable run or preserve a previous initialization."""
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise FixtureError("fixture loader requires an idle connection")
    version = _identifier(version)
    bundle = read_bundle(path)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
        cur.execute(
            "SELECT session_user = 'qa_migrate' AND current_user = 'qa_migrate'"
        )
        if cur.fetchone() != (True,):
            raise FixtureError("fixture loader requires the trusted migration role")
        cur.execute("SHOW session_replication_role")
        if cur.fetchone() != ("origin",):
            raise FixtureError("unexpected PostgreSQL session replication role")
        cur.execute(
            "SELECT pg_advisory_xact_lock(%s::bigint)", (initialization_lock(run_id),)
        )
        cur.execute(
            "SELECT fixture_version,content_sha256 FROM qa.las_fixture_runs WHERE run_id=%s",
            (run_id,),
        )
        existing = cur.fetchone()
        if existing is not None:
            if existing != (version, bundle.digest):
                raise FixtureError("fixture version or SHA-256 mismatch")
            return False
        cur.execute(
            "INSERT INTO qa.las_fixture_runs (run_id,fixture_version,content_sha256) VALUES (%s,%s,%s)",
            (run_id, version, bundle.digest),
        )
        cur.executemany(
            "INSERT INTO qa.las_run_clients (run_id,client_id) VALUES (%s,%s)",
            [(run_id, client) for client in bundle.clients],
        )
        cur.executemany(
            "INSERT INTO qa.las_orders (run_id,client_id,order_id,status,scheduled_delivery_date) "
            "VALUES (%s,%s,%s,%s,%s)",
            [
                (run_id, order.client, order.order_id, order.status, order.scheduled)
                for order in bundle.orders
            ],
        )
        cur.executemany(
            "INSERT INTO qa.las_delivery_dates (run_id,client_id,order_id,delivery_date) VALUES (%s,%s,%s,%s)",
            [
                (run_id, order.client, order.order_id, option)
                for order in bundle.orders
                for option in order.options
            ],
        )
    return True

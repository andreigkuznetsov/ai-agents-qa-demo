import hashlib
from uuid import UUID


def _lock_key(namespace: int, run_id: UUID, *fields: str) -> int:
    payload = bytearray(run_id.bytes)
    for field in fields:
        encoded = field.encode("utf-8")
        payload.extend(len(encoded).to_bytes(4, "big"))
        payload.extend(encoded)
    digest = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")
    return (namespace << 61) | (digest >> 3)


def request_lock(run_id: UUID, client: str, operation_type: str, key: str) -> int:
    return _lock_key(1, run_id, client, operation_type, key)


def order_lock(run_id: UUID, client: str, order_id: str) -> int:
    return _lock_key(2, run_id, client, order_id)


def initialization_lock(run_id: UUID) -> int:
    return _lock_key(3, run_id)

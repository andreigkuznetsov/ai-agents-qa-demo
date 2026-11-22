import pytest
from fastapi.testclient import TestClient

from app.main import app


def test_liveness_reports_process_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QA_DB_PASSWORD", raising=False)
    with TestClient(app) as client:
        response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("path", ["/docs", "/openapi.json", "/runs"])
def test_unimplemented_routes_are_not_exposed(path: str) -> None:
    with TestClient(app) as client:
        assert client.get(path).status_code == 404


@pytest.mark.parametrize("path", ["/health/ready", "/healthz"])
def test_readiness_without_configuration(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("QA_DB_PASSWORD", raising=False)
    with TestClient(app) as client:
        response = client.get(path)
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}

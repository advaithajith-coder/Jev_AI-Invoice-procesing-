from fastapi import FastAPI
from fastapi.testclient import TestClient

from jev_invoice import __version__
from jev_invoice.api.dependencies import ReadinessCheck, get_readiness_checks


async def _ok() -> None:
    return None


async def _down() -> None:
    raise ConnectionRefusedError("connect to db.internal:5432 as user jev failed")


def _override_checks(app: FastAPI, checks: dict[str, ReadinessCheck]) -> None:
    app.dependency_overrides[get_readiness_checks] = lambda: checks


def test_live_reports_version(client: TestClient) -> None:
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_ready_when_all_dependencies_ok(app: FastAPI, client: TestClient) -> None:
    _override_checks(app, {"database": _ok, "queue": _ok})

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok", "queue": "ok"}}


def test_not_ready_returns_503_without_leaking_error_details(
    app: FastAPI, client: TestClient
) -> None:
    _override_checks(app, {"database": _down, "queue": _ok})

    response = client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"] == "error: ConnectionRefusedError"
    assert "db.internal" not in response.text

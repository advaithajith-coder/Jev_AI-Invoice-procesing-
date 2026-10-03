from fastapi import FastAPI
from fastapi.testclient import TestClient

from jev_invoice.core.errors import NotFoundError


def test_generates_request_id_when_absent(client: TestClient) -> None:
    response = client.get("/health/live")

    assert len(response.headers["x-request-id"]) == 32


def test_echoes_valid_incoming_request_id(client: TestClient) -> None:
    response = client.get("/health/live", headers={"x-request-id": "abc-123_DEF"})

    assert response.headers["x-request-id"] == "abc-123_DEF"


def test_replaces_malformed_incoming_request_id(client: TestClient) -> None:
    response = client.get("/health/live", headers={"x-request-id": "bad id\r\ninjected"})

    assert response.headers["x-request-id"] != "bad id\r\ninjected"
    assert len(response.headers["x-request-id"]) == 32


def test_app_error_renders_error_envelope(app: FastAPI, client: TestClient) -> None:
    @app.get("/_test/missing")
    async def missing() -> None:
        raise NotFoundError("Invoice not found", details={"invoice_id": "inv_1"})

    response = client.get("/_test/missing", headers={"x-request-id": "req-1"})

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "not_found",
            "message": "Invoice not found",
            "details": {"invoice_id": "inv_1"},
            "request_id": "req-1",
        }
    }


def test_unhandled_exception_returns_generic_500(app: FastAPI, client: TestClient) -> None:
    @app.get("/_test/boom")
    async def boom() -> None:
        raise RuntimeError("secret internal detail")

    response = client.get("/_test/boom", headers={"x-request-id": "req-2"})

    assert response.status_code == 500
    assert response.headers["x-request-id"] == "req-2"
    assert response.json()["error"]["code"] == "internal_error"
    assert "secret internal detail" not in response.text

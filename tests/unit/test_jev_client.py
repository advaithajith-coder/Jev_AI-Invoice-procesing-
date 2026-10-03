"""JevClient and the question set, using the real TypeSafe SDK on a fake transport."""

from pathlib import Path

import pytest
from pydantic import SecretStr

from jev_invoice.config import Settings
from jev_invoice.core.errors import DecisionError
from jev_invoice.core.resilience import CircuitOpenError
from jev_invoice.decision.jev_client import JevClient
from jev_invoice.decision.questions import (
    DOCUMENT_TYPES,
    build_questions,
    build_state,
    parse_answers,
)
from tests.conftest import make_settings
from tests.fakes import (
    JEV_MODEL,
    FakeJevServer,
    error_response,
    make_jev_client,
    system_one_response,
    wire_answers,
)


@pytest.fixture
def jev_settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path, circuit_breaker_threshold=2)


async def test_ask_sends_state_model_and_typed_questions(jev_settings: Settings) -> None:
    server = FakeJevServer(wire_answers())
    client = make_jev_client(server, jev_settings)

    result = await client.ask("INVOICE text", build_questions())

    body = server.requests[0]
    assert body["state"] == "INVOICE text"
    assert body["model"] == JEV_MODEL
    assert {name: q["type"] for name, q in body["questions"].items()} == {
        "document_type": "choice",
        "is_payable": "noul",
        "prompt_injection": "noul",
        "bank_details_change": "noul",
        "urgent_pressure": "noul",
        "unusual_payment_method": "noul",
        "payment_risk": "score",
    }
    assert set(body["questions"]["document_type"]["criteria"]) == set(DOCUMENT_TYPES)
    assert (result.model, result.input_tokens) == (JEV_MODEL, 850)


@pytest.mark.parametrize("status", [400, 401, 403, 422, 429, 500])
async def test_api_errors_become_decision_errors(jev_settings: Settings, status: int) -> None:
    client = make_jev_client(FakeJevServer(error_response(status)), jev_settings)

    with pytest.raises(DecisionError) as excinfo:
        await client.ask("x", build_questions())

    assert excinfo.value.details["http_status"] == status
    assert client.breaker.consecutive_failures == 1


async def test_transient_error_retried_by_sdk(jev_settings: Settings) -> None:
    server = FakeJevServer(error_response(503), wire_answers())
    client = make_jev_client(server, jev_settings, max_retries=1)

    await client.ask("x", build_questions())

    assert len(server.requests) == 2
    assert client.breaker.consecutive_failures == 0


async def test_breaker_stops_calls_after_repeated_failures(jev_settings: Settings) -> None:
    server = FakeJevServer(error_response(500), error_response(500))
    client = make_jev_client(server, jev_settings)
    for _ in range(2):
        with pytest.raises(DecisionError):
            await client.ask("x", build_questions())

    with pytest.raises(CircuitOpenError):
        await client.ask("x", build_questions())
    assert len(server.requests) == 2


@pytest.mark.parametrize("key", [None, "", "   ", "test-key"])
def test_no_client_without_a_real_key(tmp_path: Path, key: str | None) -> None:
    settings = make_settings(tmp_path, typesafe_api_key=SecretStr(key) if key is not None else None)
    assert JevClient.from_settings(settings) is None


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        (None, "https://api.typesafe.ai"),
        ("https://api.beatapi.io", "https://api.beatapi.io"),
        ("https://api.beatapi.io/", "https://api.beatapi.io"),
    ],
)
def test_base_url_is_configurable(tmp_path: Path, base_url: str | None, expected: str) -> None:
    settings = make_settings(
        tmp_path, typesafe_api_key=SecretStr("sk-real-looking"), typesafe_base_url=base_url
    )

    client = JevClient.from_settings(settings)

    assert client is not None
    assert client._sdk._config.base_url == expected  # SDK keeps the resolved root here


# ── Question set ──────────────────────────────────────────────────────────────


def test_build_state_trims_long_text_keeping_head_and_tail() -> None:
    text = "HEAD-SUPPLIER " + "x" * 5000 + " TAIL-BANK-DETAILS"

    state = build_state(text, max_chars=600)

    assert state.startswith("HEAD-SUPPLIER") and state.endswith("TAIL-BANK-DETAILS")
    assert "characters omitted" in state and len(state) < 700
    assert build_state("  short  ", max_chars=600) == "short"


def test_parse_answers_returns_typed_answers() -> None:
    answers = parse_answers(system_one_response(wire_answers(document_type="credit_note")))

    assert answers.document_type == "credit_note"
    assert answers.payment_risk == pytest.approx(0.1)


@pytest.mark.parametrize("missing", ["urgent_pressure", "payment_risk", "document_type"])
def test_parse_answers_rejects_missing_answer(missing: str) -> None:
    wire = wire_answers()
    del wire[missing]

    with pytest.raises(DecisionError, match=missing):
        parse_answers(system_one_response(wire))


def test_parse_answers_rejects_undeclared_choice() -> None:
    with pytest.raises(DecisionError, match="document_type"):
        parse_answers(system_one_response(wire_answers(document_type="purchase_order")))


@pytest.mark.parametrize(
    ("legend_start", "score", "expected"),
    [(0, 0.0, 0.0), (0, 1.0, 1.0), (0, 2.0, 2.0), (1, 1.0, 0.0), (1, 2.0, 1.0), (1, 3.0, 2.0)],
)
def test_payment_risk_normalised_from_legend(
    legend_start: int, score: float, expected: float
) -> None:
    wire = wire_answers()
    wire["payment_risk"]["score"] = score
    wire["payment_risk"]["legend"] = {str(legend_start + i): f"level {i}" for i in range(3)}
    wire["payment_risk"]["probabilities"] = {str(legend_start + i): 1 / 3 for i in range(3)}

    assert parse_answers(system_one_response(wire)).payment_risk == pytest.approx(expected)


async def test_min_interval_paces_calls_instead_of_failing(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, jev_min_interval_seconds=61)
    now = [1000.0]
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    server = FakeJevServer(wire_answers(), wire_answers())
    client = make_jev_client(server, settings)
    client._clock = lambda: now[0]  # injected for the test
    client._sleep = fake_sleep

    first = await client.ask("x", build_questions())
    now[0] += 10  # 10 s later another invoice needs Jev
    assert client.expected_wait_seconds() == 51
    second = await client.ask("y", build_questions())

    assert first.waited_seconds == 0
    assert slept == [51]
    assert second.waited_seconds == 51
    assert len(server.requests) == 2

"""Evaluation uses frozen local samples and saved outputs, never live models."""

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jev_invoice.core.errors import ConflictError
from jev_invoice.evaluation.scoring import MISSING, passes
from jev_invoice.evaluation.service import STAGES, EvaluationStore, GeneratedOutput, ModelCall

# The evaluation dataset lives in the local test-data folder, which is not in the
# repository: these tests run where it exists and are skipped elsewhere (such as CI).
DATASET = Path("invoice generator/testing segments")
needs_dataset = pytest.mark.skipif(
    not DATASET.exists(), reason="needs the local evaluation dataset (invoice generator/)"
)


@pytest.fixture
def evaluation(tmp_path: Path) -> tuple[EvaluationStore, dict[str, Any]]:
    if not DATASET.exists():
        pytest.skip("needs the local evaluation dataset (invoice generator/)")
    db = EvaluationStore(tmp_path / "evaluation.db")
    dataset = db.import_dataset(DATASET)
    assert dataset["cases"] == 100
    assert set(dataset["segments"].values()) == {25}
    return db, db.prepare(dataset["dataset_id"], "Test fixture", list(STAGES))


def test_preparation_is_not_a_test_run(evaluation: tuple[EvaluationStore, dict[str, Any]]) -> None:
    db, run = evaluation
    summary = db.summary(run["run_id"])
    assert summary["not_run"] == 100
    assert summary["known_cost_usd"] is None
    assert summary["field_accuracy"] is None
    assert summary["status"] == "NOT RUN"


def test_correct_gl_does_not_hide_incomplete_classification(
    evaluation: tuple[EvaluationStore, dict[str, Any]],
) -> None:
    db, run = evaluation
    db.submit(
        run["run_id"],
        "receiving-demo-038",
        GeneratedOutput(
            actual={
                "lines": [
                    {
                        "line_no": 1,
                        "gl_account_code": "5000",
                        "gl_source": "po",
                        "purchase_category": None,
                        "department": None,
                        "project": None,
                        "evidence": None,
                    }
                ]
            }
        ),
    )
    summary = db.summary(run["run_id"])
    assert summary["critical_pass_cases"] == 1
    quality = summary["classification_dimensions"]
    assert quality["gl_coding"]["accuracy"] == 1
    assert quality["purchase_category"]["accuracy"] == 0
    assert quality["supporting_evidence"]["accuracy"] == 0
    assert quality["supporting_evidence"]["manual_review_required"] is True


def test_outputs_are_immutable_and_missing_fields_fail(
    evaluation: tuple[EvaluationStore, dict[str, Any]],
) -> None:
    db, run = evaluation
    case_id = run["case_ids"][0]
    result = GeneratedOutput(
        actual={},
        wall_time_ms=1200,
        calls=[
            ModelCall(
                stage="extraction",
                provider="fake",
                model="fixture",
                latency_ms=900,
                cost_usd=Decimal("0.0012"),
                input_tokens=123,
                output_tokens=50,
            )
        ],
    )
    saved = db.submit(run["run_id"], case_id, result)
    assert saved["score"]["fields_passed"] == 0
    assert saved["score"]["critical_pass"] is False
    assert all(row["missing"] for row in saved["score"]["details"])
    with pytest.raises(ConflictError):
        db.submit(run["run_id"], case_id, result)
    summary = db.summary(run["run_id"])
    assert summary["known_cost_usd"] == "0.0012"
    assert summary["sum_case_wall_time_ms"] == 1200
    assert summary["not_run"] == 99
    assert db.comparison(run["run_id"], case_id)["expected"]["assertions"]


def test_failed_calls_preserve_unknown_cost(
    evaluation: tuple[EvaluationStore, dict[str, Any]],
) -> None:
    db, run = evaluation
    db.submit(
        run["run_id"],
        run["case_ids"][0],
        GeneratedOutput(
            status="failed",
            error="timeout",
            calls=[
                ModelCall(
                    stage="extraction",
                    provider="fake",
                    model="fixture",
                    status="failed",
                    latency_ms=2000,
                )
            ],
        ),
    )
    summary = db.summary(run["run_id"])
    assert summary["failed"] == 1 and summary["completed"] == 0
    assert summary["calls_missing_cost"] == 1 and summary["known_cost_usd"] is None
    assert summary["field_accuracy"] is None


def test_exact_missing_null_bool_and_decimal_are_distinct() -> None:
    assert not passes(MISSING, {"mode": "equal", "expected": None})
    assert passes(None, {"mode": "equal", "expected": None})
    assert not passes(1, {"mode": "equal", "expected": True})
    assert passes("6.250", {"mode": "decimal", "expected": "6.25"})
    assert not passes("Infinity", {"mode": "decimal", "expected": "6.25"})


@needs_dataset
def test_evaluation_api_keeps_expected_answers_out_of_inputs(client: TestClient) -> None:
    page = client.get("/evaluations")
    assert page.status_code == 200
    assert "data-evaluations" in page.text
    assert 'href="/evaluations"' not in page.text
    dataset = client.post("/api/v1/evaluations/datasets/import")
    assert dataset.status_code == 201, dataset.text
    run = client.post(
        "/api/v1/evaluations/runs", json={"dataset_id": dataset.json()["dataset_id"]}
    ).json()
    prefix = f"/api/v1/evaluations/runs/{run['run_id']}"
    inputs = client.get(prefix + "/inputs").json()
    cases = client.get(prefix + "/cases").json()
    assert len(cases) == 100
    assert all(row["status"] == "not_run" for row in cases)
    assert all("expected" not in row for row in cases)
    assert len(inputs) == 100
    assert all("expected" not in row for row in inputs)
    key = inputs[0]["case_id"]
    output = client.post(prefix + f"/cases/{key}/output", json={"actual": {}})
    assert output.status_code == 201, output.text
    case_row = next(row for row in client.get(prefix + "/cases").json() if row["case_id"] == key)
    assert case_row["status"] == "differences"
    assert case_row["fields_passed"] == 0
    assert client.get(prefix + f"/cases/{key}").json()["generated"]["actual"] == {}
    assert client.get(prefix + "/report.csv").status_code == 200
    assert client.get("/api/v1/invoices").json() == []

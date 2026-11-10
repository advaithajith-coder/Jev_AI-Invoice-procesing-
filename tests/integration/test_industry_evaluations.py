"""Benchmark preparation, split isolation and industry reporting need no live models."""

import importlib.util
import json
from collections import Counter
from collections.abc import Callable
from decimal import Decimal
from importlib.abc import Loader
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from jev_invoice.evaluation import industries
from jev_invoice.evaluation.pilot import invoice_from_input, select_pending
from jev_invoice.evaluation.service import STAGES, EvaluationStore, GeneratedOutput


class InvoiceRenderer(ModuleType):
    render: Callable[[Path, dict[str, Any], dict[str, Any]], None]


@pytest.fixture
def suite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Test the full preparer independently of the optional PDF-rendering dependency.
    renderer = InvoiceRenderer("test_invoice_renderer")

    def render(path: Path, record: dict[str, Any], config: dict[str, Any]) -> None:
        path.write_bytes(b"%PDF-1.4\n" + record["invoice_number"].encode())

    renderer.render = render
    spec = importlib.util.spec_from_loader("test_invoice_renderer", loader=None)
    assert spec is not None

    class StubLoader(Loader):
        def exec_module(self, module: ModuleType) -> None:
            pass

    spec.loader = StubLoader()
    monkeypatch.setattr(importlib.util, "spec_from_file_location", lambda *args: spec)
    monkeypatch.setattr(importlib.util, "module_from_spec", lambda *args: renderer)
    root = tmp_path / "industry tests"
    industries.prepare(root, Path.cwd())
    return root


def test_suite_is_balanced_and_frozen(suite: Path, tmp_path: Path) -> None:
    db = EvaluationStore(tmp_path / "evaluations.db")
    info = db.import_dataset(suite)
    cases = db.dataset(info["dataset_id"])["cases"]
    assert len(cases) == len({c["invoice_number"] for c in cases}) == 200
    assert len({c["sha256"] for c in cases}) == 200
    assert Counter(c["industry"] for c in cases) == dict.fromkeys(industries.INDUSTRIES, 20)
    assert Counter(c["stage"] for c in cases) == dict.fromkeys(STAGES, 50)
    assert Counter(c["split"] for c in cases) == {"development": 160, "holdout": 40}
    assert set(Counter(c["style_id"] for c in cases).values()) == {20}
    for industry in industries.INDUSTRIES:
        for stage in STAGES:
            group = [c for c in cases if c["industry"] == industry and c["stage"] == stage]
            assert Counter(c["split"] for c in group) == {"development": 4, "holdout": 1}
    assert db.import_dataset(suite)["dataset_id"] == info["dataset_id"]
    context = json.loads((suite / "suite.json").read_text())
    context["policy"] = "Changed approved company policy"
    (suite / "suite.json").write_text(json.dumps(context))
    assert db.import_dataset(suite)["dataset_id"] != info["dataset_id"]
    with pytest.raises(ValueError, match="not empty"):
        industries.prepare(suite, Path.cwd())


def test_inputs_are_valid_and_money_reconciles(suite: Path, tmp_path: Path) -> None:
    db = EvaluationStore(tmp_path / "evaluations.db")
    info = db.import_dataset(suite)
    for case in db.dataset(info["dataset_id"])["cases"]:
        record = json.loads((suite / "records" / (case["case_id"] + ".json")).read_text())
        assert record["bill_to_name"] == "Northwind Analytics Inc"
        subtotal = sum((Decimal(line["amount"]) for line in record["line_items"]), Decimal(0))
        assert subtotal == Decimal(record["subtotal"])
        assert subtotal + Decimal(record["tax"]) == Decimal(record["total"])
        for line in record["line_items"]:
            if line["quantity"] is None:
                assert line["unit_price"] is None
            else:
                assert Decimal(line["quantity"]) * Decimal(line["unit_price"]) == Decimal(
                    line["amount"]
                )
        if case["stage"] in {"classification", "tax"}:
            invoice = invoice_from_input(case["input"])
            assert len(invoice.lines) == len(record["line_items"])
            assert invoice.total == Decimal(record["total"])
        if case["stage"] == "tax" and "certificate not supplied" in record["description"]:
            expected = {row["path"]: row["expected"] for row in case["expected"]["assertions"]}
            assert expected["tax_review.status"] == "Finance review"
            assert expected["expected_tax"] is None


def test_reservation_and_industry_scores(suite: Path, tmp_path: Path) -> None:
    db = EvaluationStore(tmp_path / "evaluations.db")
    info = db.import_dataset(suite)
    run = db.prepare(info["dataset_id"], "Industry diagnostics", list(STAGES))
    selected = select_pending(db, run["run_id"])
    assert len(selected) == 40
    assert all(db.case(run["run_id"], key)["split"] == "development" for key in selected)
    assert Counter(db.case(run["run_id"], key)["industry"] for key in selected) == dict.fromkeys(
        industries.INDUSTRIES, 4
    )
    classification = next(
        key for key in selected if db.case(run["run_id"], key)["stage"] == "classification"
    )
    db.submit(run["run_id"], classification, GeneratedOutput(actual={"lines": []}))
    assert classification not in select_pending(db, run["run_id"])
    summary = db.summary(run["run_id"])
    assert summary["splits"]["holdout"]["not_run"] == 40
    assert summary["splits"]["holdout"]["field_accuracy"] is None
    assert summary["splits"]["development"]["completed"] == 1
    industry = db.case(run["run_id"], classification)["industry"]
    assert summary["industries"][industry]["completed"] == 1
    assert summary["industries"][industry]["segments"]["extraction"]["field_accuracy"] is None
    assert summary["unresolved_field_checks"] > 0
    assert all(
        db.case(run["run_id"], key)["split"] == "holdout"
        for key in select_pending(db, run["run_id"], split="holdout")
    )

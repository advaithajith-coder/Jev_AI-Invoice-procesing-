"""Protect live fixture preparation and route comparisons without invoking models."""

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

from jev_invoice.evaluation.live_sector import control_routes, prepare
from jev_invoice.evaluation.service import EvaluationStore, GeneratedOutput


def test_live_fixtures_are_coherent_and_reserve_four_cases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Renderer(ModuleType):
        render: Callable[[Path, dict[str, Any], dict[str, Any]], None]

    class StubLoader(Loader):
        def exec_module(self, module: ModuleType) -> None:
            pass

    renderer = Renderer("layouts")

    def render(path: Path, record: dict[str, Any], config: dict[str, Any]) -> None:
        path.write_bytes(b"%PDF\n" + record["invoice_number"].encode())

    renderer.render = render
    spec = importlib.util.spec_from_loader("layouts", StubLoader())
    assert spec is not None
    monkeypatch.setattr(importlib.util, "spec_from_file_location", lambda *args: spec)
    monkeypatch.setattr(importlib.util, "module_from_spec", lambda *args: renderer)
    root = tmp_path / "live"
    prepare(Path.cwd(), root)
    plan = json.loads((root / "plan.json").read_text())
    assert len(plan["vendors"]) == 7
    assert len({v["tax_id"] for v in plan["vendors"]}) == 7
    cases = plan["cases"]
    assert len(cases) == len({c["case_id"] for c in cases}) == 20
    assert Counter(c["split"] for c in cases) == {"development": 16, "holdout": 4}
    development = [c for c in cases if c["split"] == "development"]
    assert sum(c["po"] is not None for c in development) == 11
    assert Counter(
        c["receipt_scenario"] for c in development if c["po"] and c["po"]["needs_receiving"]
    ) == {"partial": 2, "none": 1, "full": 1}
    vendors = {v["kind"]: v for v in plan["vendors"]}
    for case in cases:
        record = case["record"]
        assert record["vendor_tax_id"] == vendors[case["vendor_kind"]]["tax_id"]
        assert record["bill_to_name"] == "Northwind Analytics Inc"
        assert Decimal(record["total"]) == Decimal(record["subtotal"]) + Decimal(record["tax"])
        if po := case["po"]:
            assert po["po_number"] == record["po_reference"]
            assert po["needs_receiving"] == record["needs_receiving"]
            assert [line["description"] for line in po["lines"]] == [
                line["description"] for line in record["line_items"]
            ]
            if not po["needs_receiving"]:
                assert case["receipt_scenario"] == "not_required"
    store = EvaluationStore(tmp_path / "eval.sqlite3")
    imported = store.import_dataset(root)
    assert imported["cases"] == 20
    with pytest.raises(ValueError, match="empty folder"):
        prepare(Path.cwd(), root)


def test_display_route_normalization_preserves_raw_and_wrong_answers() -> None:
    raw = {
        "decision": {
            "exception_routing": [
                {
                    "code": "QUANTITY_VARIANCE",
                    "route": "Delivery team",
                    "evidence": ["Short delivery"],
                },
                {"code": "PRICE_VARIANCE", "route": "Finance", "evidence": ["Higher price"]},
            ]
        }
    }
    result = control_routes(raw)
    assert result["QUANTITY_VARIANCE"]["route"] == "delivery"
    assert result["QUANTITY_VARIANCE"]["route_label"] == "Delivery team"
    assert result["PRICE_VARIANCE"]["route"] == "finance"  # Wrong owner remains wrong.
    assert raw["decision"]["exception_routing"][0]["route"] == "Delivery team"
    assert control_routes({}) == {}


def test_live_classification_metrics_include_provenance(tmp_path: Path) -> None:
    store = EvaluationStore(tmp_path / "unused.sqlite3")
    result = GeneratedOutput(
        actual={
            "coding": [
                {"account": "6010", "source": "vendor"},
                {"account": "6020", "source": "ai"},
            ]
        }
    ).model_dump(mode="json")
    result.update(
        stage="classification",
        score={
            "fields_checked": 2,
            "fields_passed": 2,
            "critical_pass": True,
            "manual_review_required": False,
            "details": [
                {"path": "coding.0.account", "passed": True, "missing": False, "actual": "6010"},
                {
                    "path": "lines.0.finance_classification.fields.purchase_category.value",
                    "passed": True,
                    "missing": False,
                    "actual": "subscription",
                },
            ],
        },
    )
    metrics = store.metrics([{"case_id": "test"}], {"test": result})
    assert metrics["classification_dimensions"]["gl_coding"]["checked"] == 1
    assert metrics["classification_dimensions"]["purchase_category"]["checked"] == 1
    assert metrics["gl_sources"]["vendor"] == metrics["gl_sources"]["ai"] == 1

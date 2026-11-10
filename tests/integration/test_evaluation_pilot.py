"""The pilot records unavailable models as failures without writing to the ledger."""

import sqlite3
from pathlib import Path

import pytest

from jev_invoice.config import Settings
from jev_invoice.decision.jev_client import JevClient
from jev_invoice.evaluation import pilot
from jev_invoice.evaluation.service import STAGES, EvaluationStore


@pytest.mark.asyncio
async def test_isolated_pilot_preserves_ledger_and_does_not_score_fallbacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(ledger) as connection:
        connection.executescript("""
            CREATE TABLE gl_accounts (code, name, description, account_type, is_active);
            CREATE TABLE app_settings (key, value);
            CREATE TABLE vendors (name, default_gl_account_code);
            CREATE TABLE invoices (id);
            INSERT INTO invoices VALUES ('existing-invoice');
        """)
    before = ledger.read_bytes()
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{ledger.as_posix()}",
        storage_dir=tmp_path / "outputs",
    )
    monkeypatch.setattr(pilot, "get_settings", lambda: settings)
    monkeypatch.setattr(pilot, "build_extraction", lambda _: (None, None))
    monkeypatch.setattr(JevClient, "from_settings", lambda _: None)
    db = EvaluationStore(settings.storage_dir / "evaluations.sqlite3")
    dataset = db.import_dataset(Path("invoice generator/testing segments"))
    run = db.prepare(dataset["dataset_id"], "offline pilot verification", list(STAGES))
    summary = await pilot.run_pilot(run["run_id"])
    assert summary["failed"] == 6
    assert summary["completed"] == 2  # Exact tax rules do not require an available model.
    assert summary["not_run"] == 92
    assert summary["live_invoice_count_before"] == summary["live_invoice_count_after"] == 1
    assert ledger.read_bytes() == before
    results = db.results(run["run_id"])
    assert set(results) == set(pilot.PILOT_IDS)
    assert all(r["score"] is None for r in results.values() if r["stage"] != "tax")
    assert all(
        r["actual"]["tax_review"]["model_status"] == "not_needed"
        for r in results.values()
        if r["stage"] == "tax"
    )
    selected = pilot.select_batch(db, run["run_id"])
    assert len(selected) == len(set(selected)) == 40
    assert not set(selected) & set(results)
    assert all(
        sum(db.case(run["run_id"], key)["stage"] == stage for key in selected) == 10
        for stage in STAGES
    )
    assert results["receiving-demo-019"]["actual"]["routes"][0]["source"] == "rule_fallback"
    with pytest.raises(ValueError, match="already exist"):
        await pilot.run_pilot(run["run_id"])


def test_stage_inputs_preserve_null_quantities_and_po_provenance() -> None:
    data = {
        "invoice_fields": {"line_items": [{"description": "Legal work", "amount": "200"}]},
        "purchase_order": {
            "po_number": "PO-1",
            "lines": [
                {
                    "description": "Legal work",
                    "amount": "200",
                    "quantity": None,
                    "unit_price": None,
                    "expected_gl_account_code": "6100",
                },
            ],
        },
    }
    invoice = pilot.invoice_from_input(data)
    po = pilot.po_from_input(data, invoice)
    assert invoice.lines[0].quantity is None
    assert po is not None
    assert invoice.lines[0].purchase_order_line_id == po.lines[0].id
    assert po.lines[0].gl_account_code == "6100"


def test_uncertain_tax_is_not_a_successful_match() -> None:
    invoice = pilot.invoice_from_input({"invoice_fields": {"tax": "0"}})
    invoice.additional_info = {
        "tax_review": {
            "expected_tax": None,
            "lines": [
                {
                    "line_no": 1,
                    "expected_rate": None,
                    "description": "Unclear bundle",
                    "source": {"url": "https://example.test/source"},
                },
            ],
        }
    }
    actual = pilot.tax_output(invoice)
    assert actual["expected_tax"] is None
    assert actual["rate_matches"] is None

"""Run eight explicit stage cases with live models and an isolated in-memory ledger.

Usage: python -m jev_invoice.evaluation.pilot --run RUN_ID
The source ledger is opened read-only. Only evaluation records are persisted.
"""

import argparse
import asyncio
import hashlib
import sqlite3
import time
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jev_invoice.coding.service import CodingService
from jev_invoice.config import Settings, get_settings
from jev_invoice.db.base import Base
from jev_invoice.db.models import (
    AppSetting,
    GLAccount,
    Invoice,
    InvoiceLine,
    PurchaseOrder,
    PurchaseOrderLine,
    Vendor,
)
from jev_invoice.decision.jev_client import JevClient
from jev_invoice.decision.routing import route_exceptions
from jev_invoice.domain.decision import DecisionOutcome
from jev_invoice.domain.enums import ActionBand, DecisionAction, ReasonCode
from jev_invoice.domain.invoice import CheckFinding, ExtractedInvoice
from jev_invoice.evaluation.service import (
    STAGES,
    EvaluationStore,
    GeneratedOutput,
    ModelCall,
    encoded,
)
from jev_invoice.evaluation.telemetry import current_calls
from jev_invoice.ingest.parsing import parse_document
from jev_invoice.main import build_extraction
from jev_invoice.tax.massachusetts import check_tax

PILOT_IDS = tuple(f"receiving-demo-{number:03d}" for number in (15, 16, 38, 78, 1, 6, 19, 85))
VERSION = "isolated-stage-pilot-1"
SOURCE_ROOT = Path(__file__).resolve().parents[1]


def ledger_snapshot(settings: Settings) -> dict[str, Any]:
    """Read only the master data required by the pilot; never payment/bank details."""
    url = make_url(settings.database_url)
    if url.get_backend_name() != "sqlite" or not url.database:
        raise ValueError("This local pilot requires a SQLite source ledger.")
    uri = Path(url.database).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        return {
            "accounts": [
                dict(r)
                for r in db.execute(
                    "SELECT code,name,description,account_type,is_active "
                    "FROM gl_accounts ORDER BY code"
                )
            ],
            "policies": [
                dict(r)
                for r in db.execute(
                    "SELECT key,value FROM app_settings WHERE key LIKE 'accounting_%' ORDER BY key"
                )
            ],
            "vendors": [
                dict(r)
                for r in db.execute(
                    "SELECT name,default_gl_account_code FROM vendors ORDER BY name"
                )
            ],
            "invoice_count": db.execute("SELECT count(*) FROM invoices").fetchone()[0],
        }


def invoice_from_input(data: dict[str, Any]) -> Invoice:
    """Use supplied upstream fields, without calling extraction or reading expected answers."""
    fields = data["invoice_fields"]
    parsed = ExtractedInvoice.model_validate(fields)
    invoice = Invoice(id=uuid.uuid4(), additional_info=fields.get("additional_info", {}).copy())
    for name in (
        "vendor_name",
        "invoice_number",
        "invoice_date",
        "due_date",
        "currency",
        "subtotal",
        "discount",
        "shipping",
        "total",
        "amount_paid",
        "amount_due",
        "po_reference",
        "description",
        "bill_to_name",
        "vendor_tax_id",
    ):
        setattr(invoice, name, getattr(parsed, name))
    invoice.tax_total = parsed.tax
    invoice.lines = [
        InvoiceLine(id=uuid.uuid4(), line_no=i, **line.model_dump())
        for i, line in enumerate(parsed.line_items, 1)
    ]
    return invoice


def po_from_input(data: dict[str, Any], invoice: Invoice) -> PurchaseOrder | None:
    fixture = data.get("purchase_order")
    if not fixture:
        return None
    po = PurchaseOrder(po_number=fixture["po_number"], lines=[])
    for i, row in enumerate(fixture["lines"], 1):
        # This legacy fixture key represents the buyer's approved PO coding, not an assertion.
        line = PurchaseOrderLine(
            id=uuid.uuid4(),
            line_no=i,
            description=row["description"],
            quantity=Decimal(row["quantity"] or "1"),
            unit_price=Decimal(row["unit_price"] or row["amount"]),
            gl_account_code=row.get("gl_account_code", row.get("expected_gl_account_code")),
            finance_details=row.get("finance_details", {}),
        )
        po.lines.append(line)
    for invoice_line in invoice.lines:
        matches = [p for p in po.lines if p.description == invoice_line.description]
        if len(matches) == 1:
            invoice_line.purchase_order_line_id = matches[0].id
    return po


def tax_output(invoice: Invoice) -> dict[str, Any]:
    review = (invoice.additional_info or {})["tax_review"]
    rows = [r for r in review["lines"] if isinstance(r["line_no"], int)]
    rates = {r["expected_rate"] for r in rows}
    expected = review["expected_tax"]
    return {
        "expected_rate_percent": next(iter(rates)) if len(rates) == 1 else None,
        "expected_tax": expected,
        "rate_matches": (
            abs(Decimal(expected) - invoice.tax_total) <= Decimal("0.01")
            if expected is not None and invoice.tax_total is not None
            else None
        ),
        "evidence": [{"description": r["description"]} for r in rows],
        "tax_review": review,
    }


def select_batch(store: EvaluationStore, run_id: str) -> tuple[str, ...]:
    """Choose ten unrun cases per stage, spread across the frozen manifest."""
    run = store.run(run_id)
    existing = store.results(run_id)
    cases = store.dataset(run["dataset_id"])["cases"]
    selected: list[str] = []
    for stage in STAGES:
        candidates = [
            c["case_id"]
            for c in cases
            if c["stage"] == stage
            and c["case_id"] in run["case_ids"]
            and c["case_id"] not in existing
        ]
        if len(candidates) < 10:
            raise ValueError(f"Need ten unrun {stage} cases for this batch.")
        selected.extend(candidates[round(i * (len(candidates) - 1) / 9)] for i in range(10))
    return tuple(selected)


async def run_pilot(run_id: str, case_ids: tuple[str, ...] | None = None) -> dict[str, Any]:
    settings = get_settings()
    store = EvaluationStore(settings.storage_dir / "evaluations.sqlite3")
    run = store.run(run_id)
    selected = PILOT_IDS if case_ids is None else case_ids
    if not selected or len(selected) > 40 or len(set(selected)) != len(selected):
        raise ValueError("Choose one to forty unique cases.")
    # Discard expected answers before creating any service request.
    cases = [
        {k: v for k, v in store.case(run_id, key).items() if k != "expected"} for key in selected
    ]
    if any(key in store.results(run_id) for key in selected):
        raise ValueError("Pilot outputs already exist. Prepare a new run for retries.")
    for case in cases:
        contents = await asyncio.to_thread(Path(case["document"]).read_bytes)
        if hashlib.sha256(contents).hexdigest() != case["sha256"]:
            raise ValueError(f"Document changed: {case['case_id']}")
    snapshot = ledger_snapshot(settings)
    context = store.dataset(run["dataset_id"]).get("context", {})
    # Synthetic suite master data is part of the frozen dataset, never the live ledger.
    if context.get("master_data"):
        for key in ("accounts", "vendors"):
            if key in context["master_data"]:
                snapshot[key] = context["master_data"][key]
    safe_settings = {
        k: str(getattr(settings, k))
        for k in (
            "groq_model",
            "typesafe_model",
            "coding_min_confidence",
            "export_suspense_account",
            "jev_min_interval_seconds",
            "typesafe_timeout_seconds",
            "llm_max_output_tokens",
            "llm_timeout_seconds",
            "llm_max_attempts",
            "llm_cost_per_input_token_usd",
            "llm_cost_per_output_token_usd",
            "jev_cost_per_input_token_usd",
        )
    }
    root = SOURCE_ROOT
    code_hashes = {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for folder in ("evaluation", "coding", "tax", "decision", "extraction")
        for p in sorted((root / folder).glob("*"))
        if p.suffix in {".py", ".json"}
    }
    manifest = {
        "version": VERSION,
        "run_id": run_id,
        "dataset_id": run["dataset_id"],
        "cases": list(selected),
        "created_at": datetime.now(UTC).isoformat(),
        "master_data": snapshot,
        "settings": safe_settings,
        "code_sha256": code_hashes,
        "scope": "Stage-isolated PDF pilot; not end-to-end approval or vision accuracy.",
    }
    folder = settings.storage_dir / "evaluation-pilots" / run_id
    if case_ids is not None:
        folder = folder / f"batch-{len(selected)}-{uuid.uuid4().hex[:12]}"
    folder.mkdir(parents=True, exist_ok=True)
    manifest_path = folder / "manifest.json"
    with manifest_path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(encoded(manifest))
    versions = {
        "runner": VERSION,
        "dataset": run["dataset_id"],
        "scorer": "1",
        "configuration_sha256": hashlib.sha256(encoded(manifest).encode()).hexdigest(),
        "groq_model": settings.groq_model,
        "jev_model": settings.typesafe_model,
        "evaluation_date": datetime.now(UTC).date().isoformat(),
    }
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    extraction, sdk = build_extraction(settings)
    jev = JevClient.from_settings(settings)
    started = time.perf_counter()
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            session.add_all(GLAccount(**row) for row in snapshot["accounts"])
            session.add_all(
                AppSetting(**row, updated_by="evaluation snapshot") for row in snapshot["policies"]
            )
            await session.commit()
            for case in cases:
                key, stage = case["case_id"], case["stage"]
                print(f"START {stage} {key}", flush=True)
                calls: list[dict[str, Any]] = []
                token = current_calls.set(calls)
                start = time.perf_counter()
                actual: dict[str, Any] | None = None
                error = None
                try:
                    if stage == "extraction":
                        if extraction is None:
                            raise RuntimeError("Extraction model is not configured")
                        document = Path(case["document"])
                        contents = await asyncio.to_thread(document.read_bytes)
                        parsed = await asyncio.to_thread(parse_document, contents, document.name)
                        if parsed.needs_ocr or parsed.omitted_pages:
                            raise RuntimeError("This text-PDF pilot does not run vision")
                        waited = await extraction.wait_for_capacity()
                        result = await extraction.extract(parsed.text)
                        actual = result.invoice.model_dump(mode="json")
                        actual["raw_extraction"] = result.raw
                        if calls:
                            calls[-1]["wait_ms"] = (calls[-1].get("wait_ms") or 0) + waited * 1000
                    elif stage == "classification":
                        invoice = invoice_from_input(case["input"])
                        vendor_row = next(
                            (r for r in snapshot["vendors"] if r["name"] == invoice.vendor_name),
                            None,
                        )
                        result_coding = await CodingService(
                            session,
                            jev,
                            suspense_account=settings.export_suspense_account,
                            min_confidence=settings.coding_min_confidence,
                        ).code(
                            invoice.lines,
                            vendor=Vendor(**vendor_row) if vendor_row else None,
                            purchase_order=po_from_input(case["input"], invoice),
                            invoice_description=invoice.description,
                            document_text=encoded(
                                case["input"]["invoice_fields"].get("additional_info", {})
                            ),
                            industry_context=context.get("industries", {})
                            .get(case.get("industry"), {})
                            .get("description"),
                        )
                        lines = []
                        for line in invoice.lines:
                            details = line.finance_classification or {}
                            fields = details.get("fields", {})
                            lines.append(
                                {
                                    "line_no": line.line_no,
                                    "gl_account_code": line.gl_account_code,
                                    "gl_source": line.gl_source,
                                    **{k: v["value"] for k, v in fields.items()},
                                    "evidence": fields.get("purchase_category", {}).get("evidence"),
                                    "details": details,
                                }
                            )
                        actual = {"lines": lines, "metadata": asdict(result_coding)}
                        if result_coding.classification.get("status") in {
                            "unavailable",
                            "not_configured",
                        }:
                            error = (
                                "Classification model unavailable; fallback is not model success."
                            )
                    elif stage == "tax":
                        invoice = invoice_from_input(case["input"])
                        await check_tax(invoice, jev)
                        actual = tax_output(invoice)
                        if actual["tax_review"]["model_status"] in {
                            "unavailable",
                            "not_configured",
                        }:
                            error = "Tax model unavailable; fallback is not model success."
                    else:
                        findings = [CheckFinding(**r) for r in case["input"]["exceptions"]]
                        outcome = DecisionOutcome(
                            action=DecisionAction.HOLD_REVIEW,
                            band=ActionBand.HUMAN,
                            reason_codes=[ReasonCode(f.code) for f in findings],
                            findings=findings,
                            rationale="Supplied stage fixture findings",
                            jev_status="not_run",
                            question_set_version="routing1",
                            policy_version="fixture",
                            inputs_sha256=hashlib.sha256(
                                encoded(case["input"]).encode()
                            ).hexdigest(),
                        )
                        routes = await route_exceptions(
                            outcome, jev, settings.coding_min_confidence
                        )
                        actual = {"routes": routes}
                        if any(r["jev_status"] != "ok" for r in routes):
                            error = "Routing model unavailable; fallback is not model success."
                    if any(c["status"] == "failed" for c in calls):
                        error = "One or more model calls failed; any fallback output is retained."
                except Exception as exc:
                    # Store error types without sensitive provider request data.
                    error = f"{type(exc).__name__}: stage execution failed."
                finally:
                    current_calls.reset(token)
                elapsed = (time.perf_counter() - start) * 1000
                output = GeneratedOutput(
                    status="failed" if error else "completed",
                    actual=actual,
                    error=error,
                    wall_time_ms=elapsed,
                    calls=[ModelCall.model_validate(c) for c in calls],
                    versions=versions,
                    stage_timings_ms={stage: elapsed},
                )
                saved = store.submit(run_id, key, output)
                (folder / f"{key}.json").write_text(encoded(saved), encoding="utf-8")
                print(f"SAVED {stage} {key} {output.status} {elapsed / 1000:.1f}s", flush=True)
    finally:
        if sdk:
            await sdk.close()
        if jev:
            await jev.aclose()
        await engine.dispose()
    summary = store.summary(run_id)
    results = store.results(run_id)
    summary["batch_metrics"] = store.metrics(cases, results)
    summary["batch_segments"] = {
        stage: store.metrics([case for case in cases if case["stage"] == stage], results)
        for stage in STAGES
    }
    summary["pilot_batch_wall_time_ms"] = (time.perf_counter() - started) * 1000
    summary["batch_case_ids"] = list(selected)
    summary["batch_folder"] = str(folder.resolve())
    summary["live_invoice_count_before"] = snapshot["invoice_count"]
    summary["live_invoice_count_after"] = ledger_snapshot(settings)["invoice_count"]
    (folder / "summary.json").write_text(encoded(summary), encoding="utf-8")
    return summary


def select_pending(
    store: EvaluationStore, run_id: str, *, split: str = "development", limit: int = 40
) -> tuple[str, ...]:
    """Select only unrun cases in the requested split; holdout is never implicit."""
    if not 1 <= limit <= 40:
        raise ValueError("Choose one to forty cases.")
    run, results = store.run(run_id), store.results(run_id)
    candidates = [
        c
        for c in store.dataset(run["dataset_id"])["cases"]
        if c["case_id"] in run["case_ids"]
        and c["case_id"] not in results
        and c.get("split", "legacy") == split
    ]
    # Interleave industries and stages rather than consuming one entire segment first.
    groups: dict[tuple[str, str], list[str]] = {}
    for case in candidates:
        groups.setdefault((case.get("industry", "legacy_technology"), case["stage"]), []).append(
            case["case_id"]
        )
    selected: list[str] = []
    while groups and len(selected) < limit:
        for key in list(groups):
            selected.append(groups[key].pop(0))
            if not groups[key]:
                del groups[key]
            if len(selected) == limit:
                break
    if not selected:
        raise ValueError(f"No unrun {split} cases remain.")
    return tuple(selected)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--batch-size", type=int, choices=[40])
    parser.add_argument("--split", choices=["development", "holdout", "legacy"])
    args = parser.parse_args()
    selected = None
    if args.batch_size or args.split:
        store = EvaluationStore(get_settings().storage_dir / "evaluations.sqlite3")
        selected = (
            select_pending(store, args.run, split=args.split)
            if args.split
            else select_batch(store, args.run)
        )
    print(encoded(asyncio.run(run_pilot(args.run, selected))))


if __name__ == "__main__":
    main()

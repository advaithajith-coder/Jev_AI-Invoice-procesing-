"""Prepare and run a separate synthetic IT suite through the live HTTP application.

The original industry benchmark and its reserved cases remain immutable. Seeding
uses public services, is resumable, and never approves or pays reviewed invoices.
"""

import argparse
import hashlib
import importlib.util
import json
import sqlite3
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from jev_invoice.evaluation.industries import STAGES, assertion, make_record, money, write

VENDORS = {
    "goods": ("Synthetic IT Office Supply", "6000", True),
    "equipment": ("Synthetic IT Equipment Supply", "1500", True),
    "software": ("Synthetic IT Software Services", "6010", False),
    "consulting": ("Synthetic IT Consulting", "6020", False),
    "project": ("Synthetic IT Project Services", "6020", False),
    "freight": ("Synthetic IT Freight Services", "6800", False),
    "phone": ("Synthetic IT Telecom", "6500", False),
}
ROOT = Path("invoice generator/sector runs/technology-live-v1")


def prepare(project: Path, root: Path) -> None:
    if root.exists() and any(root.iterdir()):
        raise ValueError("Choose a new empty folder; prepared expected answers are frozen.")
    spec = importlib.util.spec_from_file_location(
        "invoice_layouts", project / "invoice generator/generate.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Invoice layout renderer not found")
    renderer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer)
    vendor_plans = []
    for index, (kind, (name, gl, goods)) in enumerate(VENDORS.items(), 1):
        vendor_plans.append(
            {
                "kind": kind,
                "name": name,
                "default_gl_account_code": gl,
                "po_required": goods,
                "tax_id": f"99-100000{index}",
                "default_currency": "USD",
                "payment_terms_days": 30,
                "created_by": "Synthetic IT sector test",
            }
        )
    by_kind = {v["kind"]: v for v in vendor_plans}
    manifests: dict[str, list[dict[str, Any]]] = {s: [] for s in STAGES}
    plans = []
    config = {"customer_reference": "NORTHWIND-LIVE-IT-TEST", "service_period": "October 2026"}
    for stage_index, stage in enumerate(STAGES):
        for variant in range(5):
            record, category, _ = make_record("technology", 0, stage, stage_index, variant)
            source = record["case_id"]
            key = f"NW-LIVE-IT-{stage_index + 1}-{variant + 1}"
            kind = ["equipment", "software", "consulting", "project", "freight"][variant]
            has_po, receiving = True, "full"
            exceptions: list[dict[str, str]] = []
            if stage == "classification":
                has_po = variant in {0, 3}
                if variant == 4:
                    kind, category = "equipment", "goods"
                    record["description"] = "Laptop computers for Massachusetts office use"
                    record["line_items"] = [
                        {
                            "description": "Laptop computer for Massachusetts office use",
                            "quantity": "2",
                            "unit_price": "1200.00",
                            "amount": "2400.00",
                        }
                    ]
                    record["tax_rate_percent"] = "6.25"
            if stage == "tax":
                kind = ["goods", "software", "consulting", "phone", "goods"][variant]
                has_po = variant != 2
                if variant == 4:
                    receiving = "partial"
            if stage == "routing" and variant > 0:
                kind, category = "goods", "goods"
                record["description"] = "Office supplies for Massachusetts office use"
                record["line_items"] = [
                    {
                        "description": "Printer paper cartons for Massachusetts office use",
                        "quantity": "8",
                        "unit_price": "25.00",
                        "amount": "200.00",
                    },
                    {
                        "description": "Office pens boxes for Massachusetts office use",
                        "quantity": "6",
                        "unit_price": "12.00",
                        "amount": "72.00",
                    },
                ]
                record["tax_rate_percent"] = "6.25"
                has_po = variant != 1
                receiving = {1: "none", 2: "partial", 3: "none", 4: "full"}[variant]
                code = {
                    1: "NO_PURCHASE_ORDER",
                    2: "QUANTITY_VARIANCE",
                    3: "QUANTITY_VARIANCE",
                    4: "PRICE_VARIANCE",
                }[variant]
                exceptions = [
                    {"code": code, "route": "delivery" if variant in {2, 3} else "purchasing"}
                ]
            vendor = by_kind[kind]
            goods = bool(vendor["po_required"])
            subtotal = sum((Decimal(line["amount"]) for line in record["line_items"]), Decimal(0))
            rate = Decimal(record["tax_rate_percent"])
            record.update(
                case_id=key,
                invoice_number=key,
                vendor_name=vendor["name"],
                vendor_tax_id=vendor["tax_id"],
                po_reference=f"PO-LIVE-IT-{stage_index + 1}-{variant + 1}" if has_po else None,
                needs_receiving=goods,
                subtotal=money(subtotal),
                tax=money(subtotal * rate / 100),
                total=money(subtotal + Decimal(money(subtotal * rate / 100))),
                notes=["Synthetic live IT test invoice. No payment instructions."],
            )
            record["amount_due"] = record["total"]
            record["other_details"] = [
                {"label": k.replace("_", " ").title(), "value": v} for k, v in config.items()
            ]
            if stage == "tax" and variant == 4:
                # Render prints the first note, so put the unresolved exemption there.
                record["notes"] = ["Synthetic test. Exemption claimed; certificate not supplied."]
            po_lines = []
            for line in record["line_items"]:
                po_price = line["unit_price"] or line["amount"]
                if stage == "routing" and variant == 4:
                    po_price = money(Decimal(po_price) * Decimal("0.8"))
                finance = {
                    "purchase_category": category,
                    "accounting_treatment": "expense",
                    "department": "IT",
                    "project": "Parser rollout",
                    "service_dates_required": False,
                    "asset_details_required": False,
                }
                if category == "subscription":
                    finance.update(
                        service_dates_required=True,
                        service_start="2026-10-01",
                        service_end="2026-10-31",
                    )
                po_lines.append(
                    {
                        "description": line["description"],
                        "quantity": line["quantity"] or "1",
                        "unit_price": po_price,
                        "gl_account_code": vendor["default_gl_account_code"],
                        "finance_details": finance,
                        "tax_rate_percent": "6.25"
                        if category in {"goods", "subscription", "utilities"}
                        else "0",
                    }
                )
            split = "holdout" if variant == 0 else "development"
            plan = {
                "case_id": key,
                "source_case_id": source,
                "stage": stage,
                "split": split,
                "vendor_kind": kind,
                "category": category,
                "record": record,
                "receipt_scenario": receiving if goods else "not_required",
                "po": {
                    "po_number": record["po_reference"],
                    "issued_date": "2026-10-06",
                    "currency": "USD",
                    "needs_receiving": goods,
                    "lines": po_lines,
                    "created_by": "Synthetic IT sector test",
                }
                if has_po
                else None,
            }
            plans.append(plan)
            document = root / "invoices" / f"{key}.pdf"
            document.parent.mkdir(parents=True, exist_ok=True)
            renderer.render(document, record, config)
            write(root / "records" / f"{key}.json", record)
            checks = [assertion("vendor_matched", True), assertion("invoice_number", key)]
            if stage == "extraction":
                for field in (
                    "vendor_name",
                    "vendor_tax_id",
                    "bill_to_name",
                    "invoice_date",
                    "due_date",
                    "currency",
                    "description",
                    "po_reference",
                ):
                    checks.append(assertion(field, record[field]))
                for field in ("subtotal", "tax", "total"):
                    checks.append(assertion(field, record[field], mode="decimal"))
                checks.append(assertion("lines.length", len(record["line_items"])))
                for i, line in enumerate(record["line_items"]):
                    checks.append(assertion(f"lines.{i}.description", line["description"]))
                    for field in ("quantity", "unit_price", "amount"):
                        checks.append(assertion(f"lines.{i}.{field}", line[field], mode="decimal"))
            elif stage == "classification":
                for i in range(len(record["line_items"])):
                    checks += [
                        assertion(f"coding.{i}.account", vendor["default_gl_account_code"]),
                        assertion(
                            f"lines.{i}.finance_classification.fields.purchase_category.value",
                            category,
                        ),
                        assertion(
                            f"lines.{i}.finance_classification.fields.purchase_category.evidence",
                            mode="nonempty",
                        ),
                    ]
            elif stage == "tax":
                expected_tax: str | None = (
                    money(subtotal * Decimal("6.25") / 100)
                    if category in {"goods", "subscription", "utilities"}
                    else "0.00"
                )
                if variant == 4:
                    expected_tax = None
                checks += [
                    assertion(
                        "additional_info.tax_review.expected_tax", expected_tax, mode="decimal"
                    ),
                    assertion(
                        "additional_info.tax_review.status",
                        "Finance review" if variant in {3, 4} else "Matches configured rule",
                    ),
                ]
            elif exceptions:
                # Check the target exception explicitly; additional controls may also flag review.
                for exception in exceptions:
                    checks += [
                        assertion(f"control_routes.{exception['code']}.route", exception["route"]),
                        assertion(f"control_routes.{exception['code']}.evidence", mode="nonempty"),
                    ]
            else:
                checks.append(assertion("purchase_order_number", record["po_reference"]))
            common = {
                "case_id": key,
                "invoice_number": key,
                "style_id": record["style_id"],
                "industry": "technology",
                "split": split,
                "scenario": record["description"],
                "invoice": f"../invoices/{key}.pdf",
                "sha256": hashlib.sha256(document.read_bytes()).hexdigest(),
            }
            write(
                root / stage / "inputs" / f"{key}.json",
                {**common, "fixture": plan, "exceptions": exceptions},
            )
            write(
                root / stage / "expected" / f"{key}.json",
                {
                    "assertions": checks,
                    "ground_truth_source": "Synthetic live purchase fixture; "
                    "actual company chart. Authoring precedes model execution.",
                    "source_sha256": hashlib.sha256(
                        (project / "src/jev_invoice/tax/ma_guide.json").read_bytes()
                    ).hexdigest(),
                    "limitations": "Primary segment score only. Review citations semantically; "
                    "GL vendor/PO defaults do not measure independent AI accuracy.",
                },
            )
            manifests[stage].append(
                {**common, "input": f"inputs/{key}.json", "expected": f"expected/{key}.json"}
            )
    for stage, cases in manifests.items():
        write(root / stage / "manifest.json", {"stage": stage, "cases": cases})
    write(root / "plan.json", {"vendors": vendor_plans, "cases": plans})
    write(
        root / "suite.json",
        {
            "name": "Live IT end-to-end v1",
            "synthetic": True,
            "buyer": "Northwind Analytics Inc",
            "industry": "technology",
            "scope": "Live derivative of the frozen industry suite; development "
            "only. Four reserved invoices are not seeded or processed.",
        },
    )


def seed(root: Path, base: str) -> None:
    import httpx

    plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
    backup = root / "before-live.sqlite3"
    if not backup.exists():
        with sqlite3.connect("data/jev_invoice.db") as source, sqlite3.connect(backup) as target:
            source.backup(target)
    with httpx.Client(base_url=base, timeout=60) as client:

        def get(path: str) -> Any:
            response = client.get(path)
            response.raise_for_status()
            return response.json()

        def post(path: str, body: dict[str, Any]) -> Any:
            response = client.post(path, json=body)
            response.raise_for_status()
            return response.json()

        vendors = {v["name"]: v for v in get("/api/v1/vendors")}
        ids = {}
        for vendor in plan["vendors"]:
            data = {k: v for k, v in vendor.items() if k != "kind"}
            existing = vendors.get(vendor["name"])
            if existing and any(
                existing[k] != vendor[k]
                for k in ("tax_id", "po_required", "default_gl_account_code")
            ):
                raise ValueError("Existing test vendor differs from frozen plan")
            row = existing or post("/api/v1/vendors", data)
            ids[vendor["kind"]] = row["id"]
        existing_pos = {p["po_number"]: p for p in get("/api/v1/purchase-orders")}
        seeded = []
        for case in plan["cases"]:
            if case["split"] == "holdout" or not case["po"]:
                continue
            payload = {**case["po"], "vendor_id": ids[case["vendor_kind"]]}
            po = existing_pos.get(payload["po_number"]) or post("/api/v1/purchase-orders", payload)
            po = get(f"/api/v1/purchase-orders/{po['id']}")
            if po["vendor_id"] != payload["vendor_id"] or len(po["lines"]) != len(payload["lines"]):
                raise ValueError("Existing PO differs from plan")
            quantities = {}
            for index, line in enumerate(po["lines"]):
                planned = payload["lines"][index]
                if line["description"] != planned["description"] or any(
                    Decimal(line[k]) != Decimal(planned[k]) for k in ("quantity", "unit_price")
                ):
                    raise ValueError("Existing PO line differs from plan")
                factor = {"full": Decimal(1), "partial": Decimal("0.5")}.get(
                    case["receipt_scenario"], Decimal(0)
                )
                target_quantity = Decimal(line["quantity"]) * factor
                received = Decimal(line["received"])
                if received > target_quantity:
                    raise ValueError("Recorded deliveries exceed planned fixture")
                if target_quantity > received:
                    quantities[line["id"]] = str(target_quantity - received)
            if quantities:
                post(
                    f"/api/v1/purchase-orders/{po['id']}/receipts",
                    {
                        "received_date": "2026-10-07",
                        "received_by": "Synthetic IT sector test",
                        "quantities": quantities,
                    },
                )
            seeded.append(
                {
                    "case_id": case["case_id"],
                    "po_id": po["id"],
                    "receipt_scenario": case["receipt_scenario"],
                }
            )
    write(root / "seeded.json", {"vendor_ids": ids, "purchase_orders": seeded})
    print(f"PREPARED {len(ids)} vendors, {len(seeded)} POs; reserved cases untouched", flush=True)


def control_routes(actual: dict[str, Any]) -> dict[str, Any]:
    """Canonical route labels for scoring; retain the unmodified live decision too."""
    labels = {
        "Purchasing": "purchasing",
        "Delivery team": "delivery",
        "Budget owner": "budget_owner",
        "Finance": "finance",
        "Supplier clarification needed": "supplier",
    }
    return {
        row["code"]: {
            **row,
            "route_label": row["route"],
            "route": labels.get(row["route"], row["route"]),
        }
        for row in (actual.get("decision") or {}).get("exception_routing", [])
    }


def run(root: Path, base: str, *, rescore: bool = False) -> None:
    import httpx

    from jev_invoice.config import Settings
    from jev_invoice.evaluation.service import EvaluationStore, GeneratedOutput

    if not (root / "seeded.json").exists():
        raise ValueError("Seed and verify the live vendor/PO/delivery fixtures first")
    store = EvaluationStore(Path(Settings().storage_dir) / "evaluations.sqlite3")
    tracking = root / ("rescored-run.json" if rescore else "run.json")
    if tracking.exists():
        run_info = json.loads(tracking.read_text(encoding="utf-8"))
    else:
        imported = store.import_dataset(root)
        name = "Live IT sector — end-to-end"
        if rescore:
            name += " / canonical route labels"
        run_info = store.prepare(imported["dataset_id"], name, list(STAGES))
        write(tracking, run_info)
    print(f"LIVE EVALUATION {base}/evaluations?run={run_info['run_id']}", flush=True)
    recorded = store.results(run_info["run_id"])
    plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
    with httpx.Client(base_url=base, timeout=600) as client:
        for case in plan["cases"]:
            key = case["case_id"]
            if case["split"] == "holdout" or key in recorded:
                continue
            target = root / "actual" / f"{key}.json"
            if target.exists():
                actual = json.loads(target.read_text(encoding="utf-8"))
            else:
                if rescore:
                    raise ValueError("Rescoring requires saved live outputs; no models are called")
                print(f"PROCESSING {key} {case['stage']}", flush=True)
                start = time.perf_counter()
                document = root / "invoices" / f"{key}.pdf"
                with document.open("rb") as file:
                    response = client.post(
                        "/api/v1/invoices", files={"file": (document.name, file, "application/pdf")}
                    )
                response.raise_for_status()
                actual = response.json()
                actual["upload_wall_time_ms"] = (time.perf_counter() - start) * 1000
                write(target, actual)
            invoice_id = actual["id"]
            response = client.get(f"/api/v1/evaluations/measurements/invoices/{invoice_id}")
            response.raise_for_status()
            measurements = response.json()
            actual["measurements"] = measurements
            actual["live_invoice_url"] = f"{base}/invoices/{invoice_id}"
            with sqlite3.connect("data/jev_invoice.db") as db:
                db.row_factory = sqlite3.Row
                steps = [
                    dict(row)
                    for row in db.execute(
                        "SELECT step, details FROM pipeline_steps "
                        "WHERE invoice_id=? ORDER BY position",
                        (invoice_id.replace("-", ""),),
                    )
                ]
            details = {s["step"]: json.loads(s["details"]) for s in steps}
            actual["coding"] = details.get("coding", {}).get("lines", [])
            actual["control_routes"] = control_routes(actual)
            write(target, actual)
            attempts = measurements.get("attempts", [])
            attempt = attempts[-1] if attempts else {}
            failed = actual["status"] == "failed"
            output = GeneratedOutput(
                status="failed" if failed else "completed",
                actual=actual,
                error=actual.get("error_message") or "Live processing failed" if failed else None,
                wall_time_ms=attempt.get("wall_time_ms", actual.get("upload_wall_time_ms")),
                calls=attempt.get("calls", []),
                stage_timings_ms={
                    s["step"]: s["duration_ms"]
                    for s in measurements["steps"]
                    if s["duration_ms"] is not None
                },
                versions={
                    "scope": "live-full-pipeline",
                    "suite": "technology-live-v1",
                    "route_labels": "canonical1",
                },
            )
            saved = store.submit(run_info["run_id"], key, output)
            write(
                root / ("rescored-comparisons" if rescore else "comparisons") / f"{key}.json", saved
            )
            print(
                f"SAVED {key} {actual['status']} {output.wall_time_ms}ms invoice={invoice_id}",
                flush=True,
            )
    write(
        root / ("rescored-summary.json" if rescore else "summary.json"),
        store.summary(run_info["run_id"]),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "seed", "run"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--rescore",
        action="store_true",
        help="Compare saved live outputs in a new run without calling models",
    )
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(Path.cwd(), args.root)
    elif args.action == "seed":
        seed(args.root, args.base)
    else:
        run(args.root, args.base, rescore=args.rescore)


if __name__ == "__main__":
    main()

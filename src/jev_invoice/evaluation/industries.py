"""Prepare a synthetic, disjoint multi-industry benchmark without invoking any models.

Reuses the project's ten invoice layouts. Expected answers are authored from purchase
fixtures, never from model responses. No application master data is changed.
"""

import argparse
import hashlib
import importlib.util
import json
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote

STAGES = ("extraction", "classification", "tax", "routing")

COMPANY = "Northwind Analytics Inc"
INDUSTRIES = {
    "technology": (
        "Technology",
        "Software development and data analytics",
        "Computer monitors",
        "Software access subscription",
        "Server setup consulting",
        "Technology project milestone",
    ),
    "light_manufacturing": (
        "Light manufacturing",
        "Assembly of small mechanical products",
        "Aluminium production blanks",
        "Production planning software access subscription",
        "Manufacturing process consulting",
        "Assembly line commissioning milestone",
    ),
    "professional_services": (
        "Professional services",
        "Consulting and advisory services",
        "Office printer paper",
        "Practice management software access subscription",
        "Accounting advisory services",
        "Client research project milestone",
    ),
    "wholesale": (
        "Wholesale distribution",
        "Distribution of finished products to business customers",
        "Merchandise cartons for resale",
        "Warehouse management software access subscription",
        "Distribution planning consulting",
        "Warehouse migration milestone",
    ),
    "retail": (
        "Retail",
        "Sales of finished products to consumers",
        "Retail merchandise for resale",
        "Point of sale software access subscription",
        "Retail operations consulting",
        "Store refit design milestone",
    ),
    "construction": (
        "Construction",
        "Building and refurbishment projects",
        "Building materials for installation",
        "Construction scheduling software access subscription",
        "Structural engineering consulting",
        "Site survey project milestone",
    ),
    "hospitality": (
        "Hospitality",
        "Hotel and accommodation operations",
        "Hotel housekeeping supplies",
        "Hotel booking software access subscription",
        "Hotel operations consulting",
        "Guest services rollout milestone",
    ),
    "logistics": (
        "Logistics",
        "Freight transport and warehouse services",
        "Warehouse packing materials",
        "Fleet management software access subscription",
        "Transport planning consulting",
        "Depot network planning milestone",
    ),
    "healthcare": (
        "Healthcare",
        "Outpatient clinical services; no exemption presumed",
        "Medical clinic consumable supplies",
        "Appointment scheduling software access subscription",
        "Clinical operations consulting",
        "Clinic administration rollout milestone",
    ),
    "property_management": (
        "Property management",
        "Managing commercial properties and maintenance",
        "Property maintenance repair supplies",
        "Property management software access subscription",
        "Property management advisory services",
        "Property inspection project milestone",
    ),
}

ACCOUNTS = [
    (
        "1400",
        "Raw materials inventory",
        "Production inputs awaiting consumption in manufacturing",
        "asset",
    ),
    (
        "1410",
        "Merchandise inventory",
        "Goods bought for resale by wholesale or retail businesses",
        "asset",
    ),
    (
        "1500",
        "Computer equipment",
        "Purchased computers and computer monitors under the demo capitalization policy",
        "asset",
    ),
    ("6000", "Office supplies", "Office paper, stationery and printer consumables", "expense"),
    (
        "6010",
        "Operating supplies",
        "Housekeeping, packing, clinic consumables and property repair supplies "
        "consumed in operations",
        "expense",
    ),
    (
        "6020",
        "Construction materials",
        "Materials installed on construction projects under the demo direct cost policy",
        "expense",
    ),
    (
        "6100",
        "Professional services",
        "Consulting, accounting, engineering, advisory and project milestone services",
        "expense",
    ),
    (
        "6200",
        "Software subscriptions",
        "Purchased recurring software access, not consulting or bespoke implementation",
        "expense",
    ),
    ("6300", "Rent", "Commercial office and operating premises rent", "expense"),
    ("6400", "Telephone", "Telephone communications service charges", "expense"),
    (
        "6500",
        "Freight",
        "Courier and freight transportation charges, not purchase of transported goods",
        "expense",
    ),
    ("9999", "Suspense", "Unresolved coding requiring Finance review", "expense"),
]
GOODS_ACCOUNTS = {
    "technology": "1500",
    "light_manufacturing": "1400",
    "professional_services": "6000",
    "wholesale": "1410",
    "retail": "1410",
    "construction": "6020",
}


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )


def money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def assertion(
    path: str, expected: Any = None, *, mode: str = "equal", critical: bool = True
) -> dict[str, Any]:
    return {"path": path, "mode": mode, "expected": expected, "critical": critical}


def purchase(industry: str, stage: str, variant: int) -> tuple[str, str, str, bool]:
    profile = INDUSTRIES[industry]
    if stage == "tax":
        # Narrow assumptions: ordinary office use, software access, pure advice,
        # deliberately wrong rate, and a conditional exemption without evidence.
        return [
            (
                "Office printer paper for Massachusetts office use; no exemption claimed",
                "goods",
                "6000",
                True,
            ),
            (profile[3] + "; software access only", "subscription", "6200", False),
            (
                profile[4] + "; pure professional services, no goods or software",
                "professional_service",
                "6100",
                False,
            ),
            (
                "Telephone voice service in Massachusetts; no Internet access",
                "utilities",
                "6400",
                False,
            ),
            (
                profile[2] + "; exemption claimed, certificate not supplied",
                "goods",
                GOODS_ACCOUNTS.get(industry, "6010"),
                True,
            ),
        ][variant]
    return [
        (profile[2], "goods", GOODS_ACCOUNTS.get(industry, "6010"), True),
        (profile[3] + ", October 2026", "subscription", "6200", False),
        (profile[4] + "; fixed fee", "professional_service", "6100", False),
        (profile[5] + "; fixed fee", "milestone", "6100", False),
        ("Freight shipment for " + profile[0].lower() + " operations", "other", "6500", False),
    ][variant]


def make_record(
    industry: str, industry_index: int, stage: str, stage_index: int, variant: int
) -> tuple[dict[str, Any], str, str]:
    description, category, account, goods = purchase(industry, stage, variant)
    number = f"NW-IND-{industry_index + 1:02}-{stage_index + 1}-{variant + 1}"
    # Whole-suite layout balance: 20 PDFs per layout, and 2 layouts per industry/stage.
    style = (industry_index * 2 + stage_index * 5 + variant) % 10 + 1
    lines: list[dict[str, Any]] = []
    for line_index in range(3 if goods else 1):
        quantity = Decimal(3 + industry_index + variant + line_index) if goods else None
        price = Decimal(24 + industry_index * 11 + variant * 7 + line_index * 4) + Decimal("0.35")
        amount = quantity * price if quantity is not None else price * 13
        lines.append(
            {
                "description": description + (f" - item {line_index + 1}" if goods else ""),
                "quantity": str(quantity) if quantity is not None else None,
                "unit_price": money(price) if goods else None,
                "amount": money(amount),
            }
        )
    subtotal = sum((Decimal(line["amount"]) for line in lines), Decimal(0))
    rate = Decimal("6.25") if category in {"goods", "subscription", "utilities"} else Decimal(0)
    if stage == "tax" and variant == 3:
        rate = Decimal("5.00")  # Deliberate supplier rate error; truth remains 6.25%.
    if stage == "tax" and variant == 4:
        rate = Decimal(0)  # Claim is not a validated exemption; expected result is review.
    tax = Decimal(money(subtotal * rate / 100))
    total = subtotal + tax
    po = (
        f"PO-IND-{industry_index + 1:02}-{stage_index + 1}-{variant + 1}"
        if variant in {0, 1}
        else None
    )
    record: dict[str, Any] = {
        "case_id": number,
        "style_id": style,
        "vendor_name": f"Synthetic {INDUSTRIES[industry][0]} Supplier {variant + 1}",
        "vendor_tax_id": f"99-{industry_index + 1:02}{stage_index + 1}{variant + 1}000",
        "bill_to_name": COMPANY,
        "invoice_number": number,
        "invoice_date": "2026-10-07",
        "due_date": "2026-11-06",
        "currency": "USD",
        "description": description,
        "po_reference": po,
        "subtotal": money(subtotal),
        "discount": "0.00",
        "shipping": "0.00",
        "tax": money(tax),
        "total": money(total),
        "amount_paid": "0.00",
        "amount_due": money(total),
        "line_items": lines,
        "description_printed": True,
        "tax_rate_percent": str(rate),
        "needs_receiving": goods,
        "payment_terms_text": "Net 30",
        "remittance_details": "Synthetic supplier; no payment instructions.",
        "notes": ["Synthetic invoice for application testing. Industry scenario: " + industry],
        "other_details": [
            {"label": "Customer reference", "value": "NORTHWIND-INDUSTRY-TEST"},
            {"label": "Service period", "value": "October 2026"},
        ],
    }
    if stage == "tax" and variant == 4:
        record["notes"].append("Exemption claimed; eligibility and certificate are not provided.")
    return record, category, account


def fields(record: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "vendor_name",
        "vendor_tax_id",
        "bill_to_name",
        "invoice_number",
        "invoice_date",
        "due_date",
        "currency",
        "description",
        "po_reference",
        "subtotal",
        "discount",
        "shipping",
        "tax",
        "total",
        "amount_paid",
        "amount_due",
        "line_items",
    )
    return {
        **{key: record[key] for key in keys},
        "additional_info": {
            key: record[key]
            for key in (
                "description_printed",
                "payment_terms_text",
                "remittance_details",
                "notes",
                "other_details",
            )
        },
    }


def expected_checks(
    stage: str,
    record: dict[str, Any],
    category: str,
    account: str,
    variant: int,
    citations: dict[str, str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    data: dict[str, Any] = (
        {"invoice_fields": fields(record)} if stage in {"classification", "tax"} else {}
    )
    checks = []
    if stage == "extraction":
        for key, value in fields(record).items():
            if key not in {"line_items", "additional_info"}:
                checks.append(
                    assertion(
                        key,
                        value,
                        mode="decimal"
                        if key
                        in {
                            "subtotal",
                            "discount",
                            "shipping",
                            "tax",
                            "total",
                            "amount_paid",
                            "amount_due",
                        }
                        else "equal",
                    )
                )
        checks.append(assertion("line_items.length", len(record["line_items"])))
        for index, line in enumerate(record["line_items"]):
            checks.extend(
                assertion(
                    f"line_items.{index}.{key}",
                    value,
                    mode="equal" if key == "description" else "decimal",
                )
                for key, value in line.items()
            )
    elif stage == "classification":
        data["purchase_order"] = (
            {
                "po_number": record["po_reference"],
                "needs_receiving": record["needs_receiving"],
                "lines": [
                    {
                        **line,
                        "gl_account_code": account,
                        "finance_details": {"purchase_category": category},
                    }
                    for line in record["line_items"]
                ],
            }
            if record["po_reference"]
            else None
        )
        checks.append(assertion("lines.length", len(record["line_items"])))
        for index, _ in enumerate(record["line_items"]):
            checks.extend(
                [
                    assertion(f"lines.{index}.line_no", index + 1),
                    assertion(f"lines.{index}.gl_account_code", account),
                    assertion(f"lines.{index}.purchase_category", category),
                    assertion(f"lines.{index}.department", None),
                    assertion(f"lines.{index}.project", None),
                    assertion(f"lines.{index}.evidence", mode="nonempty", critical=False),
                    assertion(f"lines.{index}.gl_source", "po" if record["po_reference"] else "ai"),
                ]
            )
    elif stage == "tax":
        data.update(
            jurisdiction="Massachusetts",
            assumptions="Massachusetts business use; no certificates supplied. "
            "Office goods are for own use, not resale or production. Software lines "
            "are access only; advisory lines contain no goods or software.",
        )
        if variant == 4:
            checks = [
                assertion("tax_review.status", "Finance review"),
                assertion("expected_tax", None),
                assertion("rate_matches", None),
                assertion("evidence", mode="nonempty", critical=False),
            ]
        else:
            rate = "0" if variant == 2 else "6.25"
            source_key = "services" if variant == 2 else "software" if variant == 1 else "sales-tax"
            checks = [
                assertion("expected_rate_percent", rate, mode="decimal"),
                assertion(
                    "expected_tax",
                    money(Decimal(record["subtotal"]) * Decimal(rate) / 100),
                    mode="decimal",
                ),
                assertion("rate_matches", variant != 3),
                assertion(
                    "source_url",
                    [citations[source_key], citations["-telecommunications-"]],
                    mode="one_of",
                )
                if variant == 3
                else assertion("source_url", citations[source_key]),
                assertion("evidence", mode="nonempty", critical=False),
            ]
    else:
        code, message, owner = [
            (
                "GOODS_NOT_RECEIVED",
                "Goods billed but no accepted delivery is recorded; "
                "record or confirm the delivery quantities.",
                "Delivery team",
            ),
            (
                "NO_PURCHASE_ORDER",
                "Vendor requires an order but the invoice has no valid PO reference; "
                "obtain the approved order.",
                "Purchasing",
            ),
            (
                "TAX_REVIEW_REQUIRED",
                "Supplier applied 5% tax to ordinary office goods in Massachusetts; "
                "reconcile tax treatment and rate.",
                "Finance",
            ),
            (
                "OVER_APPROVAL_LIMIT",
                "Invoice exceeds the company approval limit and needs the budget owner's approval.",
                "Budget owner",
            ),
            (
                "PRICE_VARIANCE",
                "Supplier invoiced a unit price higher than the approved PO; "
                "purchasing must resolve the price discrepancy with the supplier.",
                "Purchasing",
            ),
        ][variant]
        data = {"exceptions": [{"code": code, "message": message}]}
        checks = [
            assertion("routes.length", 1),
            assertion("routes.0.code", code),
            assertion(
                "routes.0.owner", [owner, "Finance"] if variant == 3 else [owner], mode="one_of"
            ),
            assertion(
                "routes.0.route",
                [owner, "Supplier clarification needed", "Finance"]
                if variant == 3
                else [owner, "Supplier clarification needed"],
                mode="one_of",
            ),
            assertion("routes.0.next_action", mode="nonempty", critical=False),
            assertion("routes.0.evidence", mode="nonempty", critical=False),
        ]
    return data, checks


def prepare(root: Path, project: Path) -> dict[str, Any]:
    if root.exists() and any(root.iterdir()):
        raise ValueError(
            "Destination is not empty; preserve the frozen suite and choose a new folder."
        )
    guide_path = project / "src/jev_invoice/tax/ma_guide.json"
    guide = json.loads(guide_path.read_text(encoding="utf-8"))
    citations = {
        section["id"]: section.get("url") or guide["url"] + "#" + quote(section["id"], safe="-")
        for section in guide["sections"]
    }
    # Names used by the existing source snapshot, not fabricated URLs.
    citations["services"] = citations["personal-or-professional-services"]
    citations["software"] = citations["-software-&-digital-goods-"]
    spec = importlib.util.spec_from_file_location(
        "invoice_layouts", project / "invoice generator/generate.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("The project's invoice layout renderer is unavailable.")
    renderer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer)
    config = {"customer_reference": "NORTHWIND-INDUSTRY-TEST", "service_period": "October 2026"}
    manifests: dict[str, list[dict[str, Any]]] = {stage: [] for stage in STAGES}
    for industry_index, industry in enumerate(INDUSTRIES):
        for stage_index, stage in enumerate(STAGES):
            for variant in range(5):
                record, category, account = make_record(
                    industry, industry_index, stage, stage_index, variant
                )
                key = record["case_id"]
                document = root / "invoices" / (key + ".pdf")
                document.parent.mkdir(parents=True, exist_ok=True)
                renderer.render(document, record, config)
                write(root / "records" / (key + ".json"), record)
                data, checks = expected_checks(stage, record, category, account, variant, citations)
                split = "holdout" if variant == industry_index % 5 else "development"
                common = {
                    "case_id": key,
                    "invoice_number": key,
                    "style_id": record["style_id"],
                    "industry": industry,
                    "split": split,
                    "scenario": record["description"],
                    "invoice": "../invoices/" + document.name,
                    "sha256": hashlib.sha256(document.read_bytes()).hexdigest(),
                }
                write(root / stage / "inputs" / (key + ".json"), {**common, **data})
                write(
                    root / stage / "expected" / (key + ".json"),
                    {
                        "case_id": key,
                        "assertions": checks,
                        "ground_truth_source": "Authored synthetic purchase fixture and frozen "
                        "company chart of accounts. Not model-generated truth.",
                        "source_sha256": hashlib.sha256(guide_path.read_bytes()).hexdigest()
                        if stage == "tax"
                        else None,
                        "manual_checks": [
                            "Confirm citation relevance and semantic evidence; "
                            "nonempty evidence alone is not correctness.",
                            "Review demo GL policies and tax assumptions "
                            "before making production claims.",
                        ],
                    },
                )
                manifests[stage].append(
                    {
                        **common,
                        "input": "inputs/" + key + ".json",
                        "expected": "expected/" + key + ".json",
                    }
                )
    for stage, cases in manifests.items():
        write(root / stage / "manifest.json", {"stage": stage, "count": len(cases), "cases": cases})
    suite = {
        "name": "Ten industry benchmark v1",
        "version": "industries1",
        "synthetic": True,
        "buyer": COMPANY,
        "industries": {
            key: {"name": profile[0], "description": profile[1]}
            for key, profile in INDUSTRIES.items()
        },
        "master_data": {
            "accounts": [
                {
                    "code": code,
                    "name": name,
                    "description": description,
                    "account_type": kind,
                    "is_active": True,
                }
                for code, name, description, kind in ACCOUNTS
            ],
            "vendors": [],
        },
        "policy": "Demo policies: manufacturing inputs and resale goods are inventory; "
        "computer monitors are equipment; construction materials and operating supplies "
        "use their stated cost accounts. Every non-PO GL must be suggested by JEV. "
        "Tax eligibility is not inferred from industry.",
        "split_policy": "Four development and one held-out scenario per industry/stage; "
        "reserved scenarios rotate across industries. These use the same synthetic "
        "template families; they are not an independent real-world generalization test.",
        "limitations": "Text PDFs only; no vision tests, real production data or fine-tuning. "
        "Twenty cases per industry are an initial diagnostic sample. Tax truth needs "
        "independent Finance validation before production use.",
    }
    write(root / "suite.json", suite)
    all_cases = [case for cases in manifests.values() for case in cases]
    coverage = {
        "status": "NOT RUN",
        "unique_invoices": len(all_cases),
        "industries": dict(Counter(case["industry"] for case in all_cases)),
        "stages": dict(
            Counter(
                case["stage"]
                for case in [
                    {**case, "stage": stage} for stage, cases in manifests.items() for case in cases
                ]
            )
        ),
        "splits": dict(Counter(case["split"] for case in all_cases)),
        "layouts": dict(Counter(case["style_id"] for case in all_cases)),
    }
    write(root / "coverage.json", coverage)
    return coverage


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("invoice generator/industry tests/ten-industries-v1")
    )
    args = parser.parse_args()
    print(json.dumps(prepare(args.output, Path.cwd()), indent=2))


if __name__ == "__main__":
    main()

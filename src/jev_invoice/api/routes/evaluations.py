"""Prepare evaluations and compare saved outputs. No endpoint invokes models."""

import csv
import hashlib
import io
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from jev_invoice.api.dependencies import SessionDep
from jev_invoice.core.errors import ConflictError, NotFoundError
from jev_invoice.db.models import Invoice
from jev_invoice.evaluation.service import STAGES, EvaluationStore, GeneratedOutput
from jev_invoice.pipeline.service import invoice_query

router = APIRouter(prefix="/api/v1/evaluations", tags=["evaluations"])


def store(request: Request) -> EvaluationStore:
    return EvaluationStore(Path(request.app.state.settings.storage_dir) / "evaluations.sqlite3")


Store = Annotated[EvaluationStore, Depends(store)]


class RunInput(BaseModel):
    dataset_id: str
    name: str = Field(default="Invoice evaluation", min_length=1, max_length=200)
    stages: list[str] = Field(default_factory=lambda: list(STAGES))


@router.get("/measurements/invoices/{invoice_id}")
async def invoice_measurements(invoice_id: uuid.UUID, session: SessionDep) -> dict[str, Any]:
    invoice = await session.scalar(invoice_query().where(Invoice.id == invoice_id))
    if invoice is None:
        raise NotFoundError("Invoice not found")
    return {
        "invoice_id": str(invoice_id),
        "attempts": (invoice.additional_info or {}).get("processing_measurements", []),
        "steps": [
            {"step": row.step, "duration_ms": row.duration_ms, "status": row.status}
            for row in invoice.steps
        ],
    }


@router.post("/datasets/import", status_code=201)
def import_dataset(request: Request, db: Store) -> dict[str, Any]:
    return db.import_dataset(request.app.state.settings.evaluation_dataset_dir)


@router.get("/datasets")
def datasets(db: Store) -> list[dict[str, Any]]:
    return db.datasets()


@router.post("/runs", status_code=201)
def prepare_run(body: RunInput, db: Store) -> dict[str, Any]:
    return db.prepare(body.dataset_id, body.name, body.stages)


@router.get("/runs")
def runs(db: Store) -> list[dict[str, Any]]:
    return db.runs()


@router.get("/runs/{run_id}")
def summary(run_id: str, db: Store) -> dict[str, Any]:
    return db.summary(run_id)


@router.get("/runs/{run_id}/inputs")
def inputs(run_id: str, db: Store) -> list[dict[str, Any]]:
    run = db.run(run_id)
    return [
        {
            "case_id": c["case_id"],
            "stage": c["stage"],
            "input": c["input"],
            "document_sha256": c["sha256"],
        }
        for c in db.dataset(run["dataset_id"])["cases"]
        if c["case_id"] in run["case_ids"]
    ]


@router.get("/runs/{run_id}/cases/{case_id}")
def comparison(run_id: str, case_id: str, db: Store) -> dict[str, Any]:
    return db.comparison(run_id, case_id)


@router.get("/runs/{run_id}/cases")
def cases(run_id: str, db: Store) -> list[dict[str, Any]]:
    run, results = db.run(run_id), db.results(run_id)
    rows = []
    for case in db.dataset(run["dataset_id"])["cases"]:
        if case["case_id"] not in run["case_ids"]:
            continue
        output = results.get(case["case_id"], {})
        scored = output.get("score") or {}
        status = "not_run"
        if output:
            status = (
                "failed"
                if output["status"] == "failed"
                else (
                    "passed"
                    if scored.get("fields_passed") == scored.get("fields_checked")
                    else "differences"
                )
            )
        metrics = db.metrics([case], results)
        rows.append(
            {
                "case_id": case["case_id"],
                "invoice_number": case["invoice_number"],
                "stage": case["stage"],
                "industry": case.get("industry", "legacy_technology"),
                "split": case.get("split", "legacy"),
                "style_id": case["style_id"],
                "status": status,
                "assertions": len(case["expected"]["assertions"]),
                "fields_passed": scored.get("fields_passed"),
                "fields_checked": scored.get("fields_checked"),
                "manual_review_required": scored.get("manual_review_required", False),
                "wall_time_ms": output.get("wall_time_ms"),
                "known_cost_usd": metrics["known_cost_usd"],
                "cost_incomplete": bool(
                    metrics["calls_missing_cost"] or metrics["cases_missing_call_telemetry"]
                ),
            }
        )
    return rows


@router.get("/runs/{run_id}/cases/{case_id}/document")
def document(run_id: str, case_id: str, db: Store) -> Response:
    case = db.case(run_id, case_id)
    content = Path(case["document"]).read_bytes()
    if hashlib.sha256(content).hexdigest() != case["sha256"]:
        raise ConflictError("The source document changed after this dataset was frozen.")
    return Response(content, media_type="application/pdf")


@router.post("/runs/{run_id}/cases/{case_id}/output", status_code=201)
def submit(run_id: str, case_id: str, body: GeneratedOutput, db: Store) -> dict[str, Any]:
    return db.submit(run_id, case_id, body)


@router.get("/runs/{run_id}/report.csv")
def report(run_id: str, db: Store) -> Response:
    run, results = db.run(run_id), db.results(run_id)
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(
        [
            "case_id",
            "stage",
            "industry",
            "split",
            "status",
            "fields_passed",
            "fields_checked",
            "critical_pass",
            "wall_time_ms",
            "known_cost_usd",
            "missing_cost_calls",
        ]
    )
    for case in db.dataset(run["dataset_id"])["cases"]:
        if case["case_id"] not in run["case_ids"]:
            continue
        output = results.get(case["case_id"], {})
        scored = output.get("score") or {}
        metrics = db.metrics([case], results)
        writer.writerow(
            [
                case["case_id"],
                case["stage"],
                case.get("industry", "legacy_technology"),
                case.get("split", "legacy"),
                output.get("status", "NOT RUN"),
                scored.get("fields_passed"),
                scored.get("fields_checked"),
                scored.get("critical_pass"),
                output.get("wall_time_ms"),
                metrics["known_cost_usd"],
                metrics["calls_missing_cost"],
            ]
        )
    return Response(
        stream.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="evaluation.csv"'},
    )

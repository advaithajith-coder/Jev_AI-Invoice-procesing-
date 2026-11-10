"""Frozen datasets, immutable generated outputs, scoring and usage in a separate SQLite DB."""

import hashlib
import json
import math
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from jev_invoice.core.errors import ConflictError, NotFoundError
from jev_invoice.evaluation.scoring import score

STAGES = ("extraction", "classification", "tax", "routing")


class ModelCall(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    stage: str
    provider: str
    model: str
    status: Literal["success", "failed"] = "success"
    latency_ms: float = Field(ge=0)
    wait_ms: float | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost_usd: Decimal | None = Field(default=None, ge=0)
    cost_basis: Literal["estimated", "reported", "unknown"] = "estimated"
    attempts: int | None = Field(default=None, ge=1)


class GeneratedOutput(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    status: Literal["completed", "failed"] = "completed"
    actual: dict[str, Any] | None = None
    error: str | None = None
    wall_time_ms: float | None = Field(default=None, ge=0)
    calls: list[ModelCall] = Field(default_factory=list)
    versions: dict[str, str] = Field(default_factory=dict)
    stage_timings_ms: dict[str, float] = Field(default_factory=dict)


def encoded(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, default=str)


class EvaluationStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def db(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=30) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS datasets (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES datasets(id),
                    created_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS results (
                    run_id TEXT NOT NULL REFERENCES runs(id), case_id TEXT NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY(run_id, case_id));
            """)
            yield connection

    def import_dataset(self, root: Path) -> dict[str, Any]:
        root = root.resolve()
        cases = []
        seen: set[str] = set()
        for stage in STAGES:
            folder = root / stage
            manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
            for entry in manifest["cases"]:
                key = entry["case_id"]
                if key in seen:
                    raise ConflictError(f"Case appears in multiple segments: {key}")
                seen.add(key)
                contents = {}
                for kind in ("input", "expected"):
                    target = (folder / entry[kind]).resolve()
                    if not target.is_relative_to(folder):
                        raise ConflictError("Dataset JSON path is outside its segment.")
                    contents[kind] = json.loads(target.read_text(encoding="utf-8"))
                document = (folder / entry["invoice"]).resolve()
                if not document.is_relative_to(root.parent):
                    raise ConflictError("Dataset document is outside the invoice generator folder.")
                digest = hashlib.sha256(document.read_bytes()).hexdigest()
                if digest != entry["sha256"]:
                    raise ConflictError(f"Invoice changed since preparation: {key}")
                assertions = contents["expected"].get("assertions", [])
                if not assertions or any(
                    row["mode"] not in {"exact", "equal", "decimal", "one_of", "nonempty"}
                    for row in assertions
                ):
                    raise ConflictError(f"Missing or unsupported assertions: {key}")
                cases.append(
                    {
                        "case_id": key,
                        "stage": stage,
                        "invoice_number": entry["invoice_number"],
                        "style_id": entry["style_id"],
                        "document": str(document),
                        "sha256": digest,
                        **{k: entry[k] for k in ("industry", "split", "scenario") if k in entry},
                        **contents,
                    }
                )
        payload = {"cases": cases, "scorer_version": "1"}
        context_file = root / "suite.json"
        if context_file.exists():
            payload["context"] = json.loads(context_file.read_text(encoding="utf-8"))
        key = hashlib.sha256(encoded(payload).encode()).hexdigest()
        with self.db() as db:
            db.execute(
                "INSERT OR IGNORE INTO datasets VALUES (?, ?, ?)",
                (key, datetime.now(UTC).isoformat(), encoded(payload)),
            )
        return {
            "dataset_id": key,
            "cases": len(cases),
            "segments": {stage: sum(c["stage"] == stage for c in cases) for stage in STAGES},
        }

    def dataset(self, key: str) -> dict[str, Any]:
        with self.db() as db:
            row = db.execute("SELECT payload FROM datasets WHERE id=?", (key,)).fetchone()
        if row is None:
            raise NotFoundError("Evaluation dataset not found")
        return dict(json.loads(row["payload"]))

    def datasets(self) -> list[dict[str, Any]]:
        with self.db() as db:
            return [
                {
                    "dataset_id": row["id"],
                    "created_at": row["created_at"],
                    "cases": len(json.loads(row["payload"])["cases"]),
                }
                for row in db.execute("SELECT * FROM datasets ORDER BY created_at DESC")
            ]

    def prepare(self, dataset_id: str, name: str, stages: list[str]) -> dict[str, Any]:
        dataset = self.dataset(dataset_id)
        if not stages or any(stage not in STAGES for stage in stages):
            raise ConflictError("Choose extraction, classification, tax or routing.")
        key = str(uuid.uuid4())
        payload = {
            "run_id": key,
            "dataset_id": dataset_id,
            "name": name,
            "created_at": datetime.now(UTC).isoformat(),
            "stages": stages,
            "case_ids": [c["case_id"] for c in dataset["cases"] if c["stage"] in stages],
        }
        with self.db() as db:
            db.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?)",
                (key, dataset_id, payload["created_at"], encoded(payload)),
            )
        return payload

    def run(self, key: str) -> dict[str, Any]:
        with self.db() as db:
            row = db.execute("SELECT payload FROM runs WHERE id=?", (key,)).fetchone()
        if row is None:
            raise NotFoundError("Evaluation run not found")
        return dict(json.loads(row["payload"]))

    def runs(self) -> list[dict[str, Any]]:
        with self.db() as db:
            return [
                json.loads(row["payload"])
                for row in db.execute("SELECT payload FROM runs ORDER BY created_at DESC")
            ]

    def case(self, run_id: str, case_id: str) -> dict[str, Any]:
        run = self.run(run_id)
        if case_id not in run["case_ids"]:
            raise NotFoundError("Case is not part of this run")
        return next(c for c in self.dataset(run["dataset_id"])["cases"] if c["case_id"] == case_id)

    def submit(self, run_id: str, case_id: str, result: GeneratedOutput) -> dict[str, Any]:
        case = self.case(run_id, case_id)
        if result.status == "completed" and result.actual is None:
            raise ConflictError("Completed outputs require an actual response.")
        if result.status == "failed" and not result.error:
            raise ConflictError("Failed outputs require an error description.")
        if any(not math.isfinite(value) or value < 0 for value in result.stage_timings_ms.values()):
            raise ConflictError("Stage durations cannot be negative.")
        payload = result.model_dump(mode="json")
        try:
            encoded(result.actual)
        except (ValueError, TypeError) as exc:
            raise ConflictError("Generated outputs must contain finite JSON values.") from exc
        payload.update(case_id=case_id, stage=case["stage"], saved_at=datetime.now(UTC).isoformat())
        payload["score"] = (
            score(case, result.actual)
            if result.status == "completed" and result.actual is not None
            else None
        )
        try:
            with self.db() as db:
                db.execute(
                    "INSERT INTO results VALUES (?, ?, ?)", (run_id, case_id, encoded(payload))
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                "This run already has an output for this case. Prepare a new run to retry."
            ) from exc
        return payload

    def results(self, run_id: str) -> dict[str, dict[str, Any]]:
        self.run(run_id)
        with self.db() as db:
            return {
                row["case_id"]: json.loads(row["payload"])
                for row in db.execute("SELECT * FROM results WHERE run_id=?", (run_id,))
            }

    def comparison(self, run_id: str, case_id: str) -> dict[str, Any]:
        case = self.case(run_id, case_id)
        return {
            **case,
            "generated": self.results(run_id).get(case_id),
            "status": "RECORDED" if case_id in self.results(run_id) else "NOT RUN",
        }

    def summary(self, run_id: str) -> dict[str, Any]:
        run, results = self.run(run_id), self.results(run_id)
        cases = [
            c for c in self.dataset(run["dataset_id"])["cases"] if c["case_id"] in run["case_ids"]
        ]
        groups = {
            stage: self.metrics([c for c in cases if c["stage"] == stage], results)
            for stage in run["stages"]
        }
        industry_names = sorted({c.get("industry", "legacy_technology") for c in cases})
        industries = {}
        for industry in industry_names:
            subset = [c for c in cases if c.get("industry", "legacy_technology") == industry]
            industries[industry] = {
                **self.metrics(subset, results),
                "segments": {
                    s: self.metrics([c for c in subset if c["stage"] == s], results)
                    for s in run["stages"]
                },
                "splits": {
                    split: self.metrics(
                        [c for c in subset if c.get("split", "legacy") == split], results
                    )
                    for split in sorted({c.get("split", "legacy") for c in subset})
                },
            }
        routing = [c for c in cases if c["stage"] == "routing"]
        calls = [call for record in results.values() for call in record["calls"]]
        by_model: dict[str, dict[str, Any]] = {}
        for call in calls:
            key = f"{call['provider']} / {call['model']} / {call['stage']}"
            group = by_model.setdefault(
                key,
                {
                    "calls": 0,
                    "failed": 0,
                    "latency_ms": 0.0,
                    "known_cost_usd": Decimal(0),
                    "missing_cost": 0,
                },
            )
            group["calls"] += 1
            group["failed"] += call["status"] == "failed"
            group["latency_ms"] += call["latency_ms"]
            if call["cost_usd"] is None:
                group["missing_cost"] += 1
            else:
                group["known_cost_usd"] += Decimal(call["cost_usd"])
        for group in by_model.values():
            group["known_cost_usd"] = (
                str(group["known_cost_usd"]) if group["missing_cost"] < group["calls"] else None
            )
        return {
            **run,
            **self.metrics(cases, results),
            "segments": groups,
            "industries": industries,
            "splits": {
                split: self.metrics(
                    [c for c in cases if c.get("split", "legacy") == split], results
                )
                for split in sorted({c.get("split", "legacy") for c in cases})
            },
            "models": by_model,
            "routing_groups": {
                name: self.metrics(
                    [c for c in routing if bool(c["input"].get("exceptions")) == flag], results
                )
                for name, flag in (("with_findings", True), ("without_findings", False))
            },
        }

    @staticmethod
    def metrics(cases: list[dict[str, Any]], results: dict[str, dict[str, Any]]) -> dict[str, Any]:
        records = [results[c["case_id"]] for c in cases if c["case_id"] in results]
        scored = [r["score"] for r in records if r["score"] is not None]
        calls = [call for record in records for call in record["calls"]]
        costs = [Decimal(call["cost_usd"]) for call in calls if call["cost_usd"] is not None]
        timings = sorted(r["wall_time_ms"] for r in records if r["wall_time_ms"] is not None)
        fields = sum(row["fields_checked"] for row in scored)
        passed = sum(row["fields_passed"] for row in scored)
        dimensions = {}
        assertions_all = [item for scored_case in scored for item in scored_case["details"]]
        for label, suffix in (
            ("gl_coding", (".gl_account_code", ".account")),
            ("purchase_category", (".purchase_category", ".purchase_category.value")),
            ("supporting_evidence", (".evidence",)),
        ):
            assertions = [
                item
                for record in records
                if record["stage"] == "classification" and record["score"] is not None
                for item in record["score"]["details"]
                if item["path"].endswith(suffix)
            ]
            dimensions[label] = {
                "checked": len(assertions),
                "passed": sum(item["passed"] for item in assertions),
                "accuracy": sum(item["passed"] for item in assertions) / len(assertions)
                if assertions
                else None,
                "manual_review_required": label == "supporting_evidence",
            }
        return {
            "status": "NOT RUN"
            if not records
            else "COMPLETE"
            if len(records) == len(cases)
            else "PARTIAL",
            "cases": len(cases),
            "not_run": len(cases) - len(records),
            "completed": len(scored),
            "failed": sum(r["status"] == "failed" for r in records),
            "fields_checked": fields,
            "fields_passed": passed,
            "field_accuracy": passed / fields if fields else None,
            "classification_dimensions": dimensions,
            "unresolved_field_checks": sum(
                not item["passed"] and (item["missing"] or item["actual"] is None)
                for item in assertions_all
            ),
            "answered_differences": sum(
                not item["passed"] and not item["missing"] and item["actual"] is not None
                for item in assertions_all
            ),
            "gl_sources": {
                source: sum(
                    line.get("gl_source", line.get("source")) == source
                    for record in records
                    if record["stage"] == "classification"
                    for line in (
                        (record.get("actual") or {}).get("coding")
                        or (record.get("actual") or {}).get("lines", [])
                    )
                )
                for source in ("po", "ai", "vendor", "suspense", "validated_correction")
            },
            "critical_pass_cases": sum(s["critical_pass"] is True for s in scored),
            "critical_pass_rate_among_completed": (
                sum(s["critical_pass"] is True for s in scored) / len(scored) if scored else None
            ),
            "critical_pass_rate_among_attempted": (
                sum(s["critical_pass"] is True for s in scored) / len(records) if records else None
            ),
            "manual_review_cases": sum(s["manual_review_required"] for s in scored),
            "model_calls": len(calls),
            "known_cost_usd": str(sum(costs, Decimal(0))) if costs else None,
            "calls_missing_cost": len(calls) - len(costs),
            "cases_missing_call_telemetry": sum(not r["calls"] for r in records),
            "cases_missing_wall_time": sum(r["wall_time_ms"] is None for r in records),
            "sum_case_wall_time_ms": sum(timings) if timings else None,
            "p95_case_wall_time_ms": timings[max(0, (95 * len(timings) + 99) // 100 - 1)]
            if timings
            else None,
            "note": "Costs are recorded estimates/reports. Missing usage is unknown, not zero. "
            "Summed case time is not concurrent batch elapsed time.",
        }

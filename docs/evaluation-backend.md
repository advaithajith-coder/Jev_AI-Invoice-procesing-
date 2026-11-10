# Invoice evaluation backend

Open **Evaluations** in the site's navigation, or visit `/evaluations`, for the React dashboard.
It provides run selection, segment/result filters, invoice search, expected/generated comparisons,
original PDFs, model cost/time records and report downloads. Preparing a run does not start tests.
In a case's comparison dialog, **Import saved output** accepts a raw stage response JSON or the
measurement envelope below. A raw response records unknown cost/time. Saved outputs are immutable;
prepare a new run when comparing a revised response.

The backend stores frozen expected answers, generated outputs, field comparisons, errors,
model usage and timing separately from live invoices. Preparing a dataset or run does not
invoke models, upload invoices, record deliveries or create payables.

The existing 100 PDFs remain four non-overlapping groups of 25: extraction, classification,
tax and routing. Ten scanned references remain separate. Input JSON and expected assertions
are copied into a separate SQLite database at `<storage_dir>/evaluations.sqlite3`. Each source
PDF's hash is verified. Source edits require importing a new dataset version. Expected answers
are never replaced by model answers. A run accepts one immutable response per case; use a new
run for a retry or another model/prompt version.

## Prepare without testing

```powershell
uv run python -m jev_invoice.evaluation prepare --name "100-invoice baseline"
```

This prints the run ID, 100 NOT RUN cases and empty accuracy/cost metrics. `EVALUATION_DATASET_DIR`
can override the default `invoice generator/testing segments` source directory.

## API

| Method | Endpoint under `/api/v1/evaluations` | Purpose |
| --- | --- | --- |
| POST | `/datasets/import` | Freeze the configured local dataset; verify document hashes |
| GET | `/datasets` | List frozen versions |
| POST | `/runs` | Prepare a run with `dataset_id`, `name`, optional `stages` |
| GET | `/runs` | List runs |
| GET | `/runs/{run_id}` | Accuracy, failure, cost and time summary by segment and model |
| GET | `/runs/{run_id}/inputs` | Model inputs only; expected answers are excluded |
| GET | `/runs/{run_id}/cases/{case_id}` | Expected and generated outputs, differences and usage |
| GET | `/runs/{run_id}/cases/{case_id}/document` | Hash-verified original PDF |
| POST | `/runs/{run_id}/cases/{case_id}/output` | Store and score a saved generated response |
| GET | `/runs/{run_id}/report.csv` | Download the case summary |
| GET | `/measurements/invoices/{invoice_id}` | Future pipeline runs' model calls and step durations |

Example saved-response envelope:

```json
{
  "status": "completed",
  "actual": {"vendor_name": "Example supplier", "total": "106.25"},
  "wall_time_ms": 1350,
  "stage_timings_ms": {"extraction": 1100},
  "versions": {"model": "exact-model-version", "prompt": "prompt-version"},
  "calls": [{
    "stage": "extraction", "provider": "provider-name", "model": "exact-model-version",
    "status": "success", "latency_ms": 1100, "wait_ms": 200,
    "input_tokens": 1000, "output_tokens": 300,
    "cost_usd": "0.00033", "cost_basis": "estimated", "attempts": 1
  }]
}
```

Use each segment's documented actual-response shape inside `actual`. The example is deliberately
partial and is not a passing expected response. For a failed attempt, send `status: failed`, an
`error`, `actual: null`, and any available measurements. Missing measurements stay unknown, not zero.

```powershell
uv run python -m jev_invoice.evaluation submit --run RUN_ID --case CASE_ID --response output.json
uv run python -m jev_invoice.evaluation report --run RUN_ID
```

## Scoring and interpretation

The explicit eight-case pilot can invoke the configured live models:

```powershell
uv run python -m jev_invoice.evaluation.pilot --run RUN_ID
```

For an explicitly requested batch of 40 additional cases:

```powershell
uv run python -m jev_invoice.evaluation.pilot --run RUN_ID --batch-size 40
```

This selects ten unrun cases per segment, spread across each manifest. Existing outputs
are excluded. Batch manifests and summaries are saved in separate subfolders under the run.

It runs two cases per segment, opens the source ledger read-only, and copies the chart of
accounts and company policies into an in-memory database. Expected answers are excluded from
model requests. Model errors retain any fallback output but count as failed attempts. The
other 92 cases remain unrun. Repeat attempts require a new run; saved outputs are immutable.
Configuration, master data and source-code hashes are captured before execution, with JSON
outputs and a batch summary under `<storage_dir>/evaluation-pilots/RUN_ID/`. This text-PDF pilot
measures individual stages; it does not measure vision transcription or end-to-end approval.


The scorer uses frozen assertions: normalized text equality, exact Decimal values, accepted
alternatives and nonempty evidence. Missing fields differ from explicit nulls. Classification
lines are ordered by line number; routes are aligned to supplied exception codes. Evidence
presence is a mechanical check; evidence meaning still requires human assessment. Routing
summaries separate cases with findings from empty cases.

Reports expose both critical pass rate among completed cases and among attempted cases, so
failed executions cannot disappear behind a good accuracy score. Cases without submitted
outputs remain NOT RUN. A COMPLETE run means every case has a recorded result, not that all passed.

Recorded costs retain their estimated/reported/unknown basis. Summaries show known subtotal,
missing-cost calls, calls per model/stage, durations, and missing measurements. Sum of case
durations is not concurrent batch elapsed time. Local OCR records zero API charge; this excludes
electricity/hardware cost. SDK-internal retries can have unreported charges or counts.

New normal invoice processing captures model-call measurements automatically and stores job wall
time including waits/retries. Existing pipeline steps provide control-step durations. Measurements
exclude pre-job upload queue time. Historical invoices are not backfilled with invented metrics.

The backend compares saved responses; it intentionally has no live-model execution endpoint.
For isolated evaluations, supply only `/inputs` to the stage runner and submit its output/usage.
Do not treat a full-pipeline measurement as an isolated stage measurement. No live sample testing
has been started by this implementation.

# Jev Invoice

Accounts-payable automation that reads supplier invoices, checks them against your vendors,
purchase orders and deliveries, and decides what happens next: **auto-approve**, **route for
approval**, **hold for review**, **reject**, or **block for fraud review**, with the reasons
written down.

The design splits the work by what each part is good at:

| Step | Done by | Why |
|---|---|---|
| Transcribe scans and photos | Vision LLM (Groq, `qwen/qwen3.8-27b`) | Jev reads text only, so images become text first |
| Read the invoice | LLM (Groq, `openai/gpt-oss-120b`) | Invoices vary too much for templates |
| Check facts | Code | Arithmetic, dates, duplicates, PO matching have one right answer |
| Judge the document | [TypeSafe Jev](https://typesafe.ai) | Fraud pressure, injected instructions, document type: calibrated probabilities, not prose |
| Decide | Code policy (versioned) | Thresholds and priorities you can test, audit, and tune |

```
upload (PDF / scan / photo / .txt / .eml)
  → parse + store original (SHA-256; same file twice = processed once)
  → scans and photos: vision model transcribes each page to text (Groq, qwen)
  → extract fields (Groq)            Decimal money, real dates, raw bank account never stored
  → document checks (code)           arithmetic incl. discount + tax, dates, approval limit
  → master-data checks (code)        vendor known / on hold / blocked, bank account vs file,
                                     currency, duplicate, PO 2-way + 3-way match
  → judgment (Jev)                   document type, payable?, injection, bank-change,
                                     urgency, unusual payment method, payment risk
  → policy → auto-approve, or open a review task with every reason
  → append-only audit trail of every step
```

## Decision policy

Every applicable reason is recorded; the action comes from the highest-priority group.
**Auto-approve requires zero reasons.** If Jev is missing, slow, or unsure, a person decides.

| Priority | Action | Band | Triggered by |
|---|---|---|---|
| 1 | `fraud_review` | block | bank account differs from file, vendor blocked, Jev: bank details changed, unusual payment method, high risk |
| 2 | `hold_review` | human | Jev: prompt injection; Jev not configured or unavailable |
| 3 | `reject_document` | confirm | duplicate invoice; Jev: not an invoice, not payable |
| 4 | `hold_review` | human | credit note, missing fields, totals/dates wrong, unknown or on-hold vendor, currency mismatch, PO problems, goods not received, urgency, moderate risk, Jev uncertain |
| 5 | `route_approval` | confirm | over approval limit, no purchase order |
| – | `auto_approve` | auto | nothing above |

Details: [ADR 001](docs/decisions/001-extraction-checks-judgment-policy.md),
[ADR 003](docs/decisions/003-decision-policy.md).

## Run it

Needs [uv](https://docs.astral.sh/uv/). No Docker or database server: local runs use SQLite.

```bash
cp .env.example .env        # add GROQ_API_KEY and TYPESAFE_API_KEY
uv sync
uv run poe setup            # create the database and load demo vendors, POs, deliveries
uv run poe dev              # http://localhost:8000 (screens), /docs (API)
```

Open http://localhost:8000: upload an invoice, then decide it from the **Review queue**.
Every invoice page shows the original, what was read, the checks, Jev's judgment, the
decision in plain language, and the audit history. Approving an invoice blocked as possible
fraud requires a written note on how it was verified.

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/invoices` | Upload a PDF, scan or photo (PNG/JPG/WEBP/TIFF), `.txt`, or `.eml` and run the pipeline |
| `GET /api/v1/invoices[?status=]` | List invoices |
| `GET /api/v1/invoices/{id}` | One invoice with lines, decision, and findings |
| `GET /api/v1/review` | Invoices waiting for a person, with reasons |
| `POST /api/v1/review/{task_id}/approve` or `/reject` | Resolve a review (`{"reviewer": "...", "note": "..."}`) |
| `GET /health/live`, `/health/ready` | Probes |

Jev can be called directly from TypeSafe or through [BeatAPI](https://beatapi.io/jev-api)
(same API): set `TYPESAFE_BASE_URL=https://api.beatapi.io` and `TYPESAFE_MODEL=jev-1.13-free`.

## Development

| Command | What it does |
|---|---|
| `uv run poe check` | lint + strict typecheck + tests (what CI runs) |
| `uv run poe fmt` | format and autofix |
| `LIVE=1 uv run pytest -m live -v -s` | end-to-end with real Groq and Jev (uses `.env` keys) |

Tests use a fresh SQLite database per test, a scripted LLM, and the real TypeSafe SDK on an
in-memory transport, so request and response formats are exercised without network.

## Project layout

```
src/jev_invoice/
  ingest/       parsing (PDF, text, email) and content-addressed file storage
  extraction/   LLM client (retry, circuit breaker, daily budget), Groq adapter, prompt
  domain/       typed invoice, document checks, enums, vendor identity helpers
  matching/     vendor, bank account, duplicate, and purchase-order checks
  decision/     Jev client, question set, policy, decision service
  pipeline/     orchestration with audit trail; human review workflow
  api/          FastAPI routes and schemas
  db/           models (SQLite + PostgreSQL), seed data
migrations/     Alembic (append-only triggers on PostgreSQL)
```

## Acknowledgements

The feature scope was informed by studying
[afras23/invoice-processing-automation](https://github.com/afras23/invoice-processing-automation).
No code from that repository is used here.

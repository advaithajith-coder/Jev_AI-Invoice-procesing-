# CLAUDE.md

Guide for Claude Code working in this repository. Keep it short and current: if you learn
something non-obvious about this project, add it here.

## What this is

Jev Invoice: accounts-payable automation. Reads supplier invoices (PDF, scan, photo, `.txt`,
`.eml`), checks them against vendors / POs / deliveries, and decides: auto-approve, route for
approval, hold for review, reject, or block for fraud review, with every reason recorded.

Pipeline (see `README.md` and `docs/decisions/`):
extract (Groq LLM, vision for scans) → code checks → judgment (TypeSafe Jev) → code policy → audit trail.

## Commands (Python 3.12, uv)

```bash
uv sync                        # install
uv run poe check               # lint + strict mypy + tests: run before every commit (CI runs this)
uv run poe fmt                 # format and autofix
uv run pytest tests/unit/test_checks.py -q   # one file
uv run poe setup               # create SQLite DB + seed demo data
uv run poe dev                 # http://localhost:8000
```

- CI (`.github/workflows/ci.yml`): `poe lint`, `poe typecheck`, `pytest -m "not live"`, docker build.
- `-m live` tests call real Groq/TypeSafe and need keys in `.env`. Never run them unattended.
- Do not run `poe demo`: it wipes `data/jev_invoice.db`.

## Layout

```
src/jev_invoice/
  ingest/      parsing (PDF, text, email) + content-addressed storage
  extraction/  LLM client (retry, circuit breaker, daily budget), Groq + vision, prompts
  domain/      typed invoice, document checks, enums, ledger, payment, vendor helpers
  matching/    vendor, bank account, duplicate, PO 2-/3-way checks
  decision/    Jev client, question set, policy (pure function), decision service
  coding/      GL account coding (own Jev question set)
  pipeline/    orchestration + human review workflow
  payables/    payables overview, discounts
  export/      accounting export batches
  masterdata/  vendors, accounts, POs, import, rules
  api/         FastAPI JSON routes (/api/v1/...)
  web/         server-rendered UI (Jinja templates, static JS/CSS)
  db/          SQLAlchemy models, session, seed data
migrations/    Alembic, files named YYYYMMDD_<rev>_<slug>.py
tests/         unit/, integration/, live/; fakes in tests/fakes.py
docs/          decisions/ (ADRs), roadmap.md
```

## Rules that matter

- **Money is `Decimal`, never float.** Dates are `date`. Parse and round amounts with `to_money()` / `to_decimal()` in `domain/invoice.py`.
- **Fail closed.** If Jev is missing, slow, errors, or is uncertain, the invoice goes to a person. Never add a path that auto-approves without zero reasons.
- **Policy changes:** `decision/policy.py` is a pure function. Bump `POLICY_VERSION` (with a short comment why) whenever a threshold or rule changes. Every `ReasonCode` belongs to exactly one priority group (a test enforces this).
- **Question changes:** bump `QUESTION_SET_VERSION` in `decision/questions.py` (or `coding/service.py`) when wording or options change.
- **Audit:** every state change on an invoice goes through `core/audit.record(...)`. Decisions and audit events are append-only.
- **Schema changes:** add an Alembic migration (`uv run alembic revision --autogenerate -m "..."`); it must work on SQLite (batch mode) and PostgreSQL. A test runs `alembic check`.
- **Never store raw bank account numbers**; only HMAC fingerprints.
- Tests must not hit the network: use `tests/fakes.py` (scripted LLM, in-memory Jev transport). Each test gets its own SQLite file.
- Strict mypy and ruff (line length 100). Keep both clean.
- Never commit `.env` or anything in `data/`.

## Status

See `docs/roadmap.md`. Open: IMAP intake, Xero export, evaluation set, auth/roles/deploy.

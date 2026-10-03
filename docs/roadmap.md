# Roadmap

| # | Milestone | Status |
|---|---|---|
| 0 | Foundations: uv, ruff, strict mypy, pytest, CI, settings, structured logs, health probes | ✅ |
| 1 | Domain + database: vendors, POs, receipts, chart of accounts, invoices, decisions, review tasks, audit log; migrations; seed data | ✅ (SQLite local, PostgreSQL prod: ADR 002) |
| 2 | Intake: PDF, text, `.eml`, scans and photos (vision transcription); content-addressed storage; duplicate files processed once | ✅ · ⏳ IMAP polling |
| 3 | Extraction: Groq JSON mode → typed invoice; versioned prompt; retries, circuit breaker, daily budget | ✅ |
| 4 | Code checks: arithmetic incl. discount, dates, approval limit, vendor status, bank account vs file, currency, duplicates, PO 2-way + 3-way | ✅ |
| 5 | Jev decision layer: question set, policy with reason codes, fail closed, provenance | ✅ live-tested via BeatAPI |
| 6 | Review: inbox, review queue and invoice screens; approve or reject with audit trail; notes required to approve blocked invoices | ✅ |
| 7 | Accounting export (Xero) | ⏳ |
| 8 | Evaluation: ~100 labelled invoices, extraction accuracy, Jev calibration, tuned thresholds | ⏳ |
| 9 | Hardening: authentication and roles, rate limits, background queue, deploy | ⏳ |

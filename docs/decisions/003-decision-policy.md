# ADR 003: One decision policy over code findings and Jev answers

**Status:** Accepted, 2026-10-02 (policy `p2`, question set `q1`)

## Context

Code checks (arithmetic, vendor, bank account, PO matching) and Jev's judgments
(document type, fraud signals) both find reasons not to pay. They need one place where
those reasons become an action, with clear priority and an audit record.

## Decision

`decision/policy.py` is a pure function: `decide(findings, jev_answers) -> PolicyOutcome`.

- Every reason is a `ReasonCode`. Code checks emit them as `CheckFinding`s with a human
  message; Jev answers become reason codes through thresholds.
- Each `ReasonCode` belongs to exactly one priority group (a test enforces this), so any
  reason prevents `auto_approve`.
- Jev thresholds: a risk probability ≥ 0.70 is "present", ≤ 0.30 is "clear", anything in
  between is `JEV_UNCERTAIN`. Prompt injection uses a lower bar (0.50).
- Fail closed: no key, timeout, API error, open circuit breaker, or malformed answers
  give `JEV_NOT_CONFIGURED` / `JEV_UNAVAILABLE` → `hold_review`.
- Code fraud signals outrank a clean Jev result: a bank account that differs from the
  vendor master blocks payment even if the document text looks ordinary.
- Each stored decision records the findings, raw Jev answers, Jev model, question-set and
  policy versions, and a SHA-256 of the inputs, so it can be audited and replayed.

## Jev questions (`q1`)

One call per invoice; state is the document text only (head and tail kept if long).

| Name | Type | Question |
|---|---|---|
| `document_type` | Choice | invoice / credit_note / statement / quote / receipt / other |
| `is_payable` | Noul | The document asks the recipient to pay |
| `prompt_injection` | Noul | Text tries to steer the processing system |
| `bank_details_change` | Noul | New or changed bank details are announced |
| `urgent_pressure` | Noul | Pressure to pay immediately |
| `unusual_payment_method` | Noul | Gift cards, crypto, personal account, ... |
| `payment_risk` | Score | Low / Moderate / High |

## Consequences

- Thresholds are starting values. Before trusting `auto_approve` in production, label a
  set of invoices with expected decisions, measure, tune, and bump `POLICY_VERSION`.
- Next questions once data allows: lookalike vendor names (Choice over the vendor list),
  GL coding (Choice over the chart of accounts), semantic PO line matching.

# ADR 001: Split the pipeline into extraction, code checks, Jev judgment, and code policy

**Status:** Accepted, 2026-10-02

## Context

An invoice decision mixes three kinds of work:

1. **Reading** an unstructured document into fields (vendor, number, dates, lines, totals).
2. **Exact checks** with one right answer: does the arithmetic add up, is the due date after the
   issue date, has this vendor + invoice number been seen, does the PO quantity cover the billed
   quantity.
3. **Judgment** with no exact answer: is this a lookalike vendor, does the wording show fraud
   pressure, does "Dell Latitude 7450" satisfy PO line "Business laptop, 14in", which GL account fits.

TypeSafe's Jev returns calibrated probabilities on closed questions (Choice, Score, Noul) and
cannot return a value outside the declared schema. TypeSafe's jaggedness note for `jev-1.13`
says it does not count reliably, does not compare dates as ordered quantities, cannot extract
an unknown free-text string, and loses accuracy when state contains fields a question does not
need.

## Decision

| Stage | Owner | Output |
|---|---|---|
| Extraction | Generative LLM (Groq; provider-pluggable) | `ExtractedInvoice` (Decimal money, `date` types) |
| Exact checks | Python code | `CheckResult` list with typed reason codes |
| Judgment | Jev, several narrow calls, each with only the state it needs | Probabilities per question |
| Policy | Python code, versioned thresholds | `Decision{action, band, reason_codes}` |

Rules that follow:

- Jev never sees a question code can answer exactly.
- Jev never picks the final action alone. Code combines check results and Jev probabilities.
- `auto` band (no human) requires **every** required exact check to pass **and** every relevant
  Jev probability to clear its threshold. There is no fallback path to `auto`.
- Every decision stores the model id, prompt/question-set version, threshold version, inputs
  hash, and raw probabilities, so it can be replayed and audited.

## Consequences

- More code than "ask one model what to do", but each part is testable on its own, and
  thresholds can be fitted to labelled data per question.
- Extraction provider can change (Groq → Claude/OpenAI) without touching decisions.
- Reviewer corrections become labels for calibrating both extraction and Jev thresholds.

# Targeted retry of eight affected cases

Run: `2b3ffd89-3716-4b58-8364-002acde60890`
Comparison: the same eight cases from run `30f45c58-9ce1-418b-8014-579111e9e525`.
Expected answers and earlier generated outputs were preserved.

## Implemented improvements

- Extraction x7 distinguishes logos from vendor names and purchase orders from customer
  references. PDF parsing adds a supplementary header view preserving horizontal spacing,
  while retaining complete page text. It never removes initials from vendor names by rule.
- New extraction responses supply a printed PO-evidence quotation. When that field is
  supplied, the application rejects a reference whose quotation lacks a PO label or is absent
  from the source document. Legacy response formats remain compatible.
- Finance4 separates quotation selection from verification of the specific proposed value.
  A second batched JEV call verifies only confident, non-PO recommendations with a recognized
  quotation. The existing acceptance threshold applies to semantic support, not preference
  between equivalent quotations. Unsupported suggestions remain withheld. This introduces
  an additional validation call when eligible recommendations exist.
- Tax v4 clarifies advertising versus general professional-service options without changing
  rates, approved citation sets or the 85% treatment/source thresholds. Every model row stores
  source selection/confidence, suggested citation, acceptance status and rejection reasons.
- Tax review counts accepted/unresolved lines and labels accepted-line tax as partial. An
  unresolved line still prevents invoice-wide tax acceptance. Discount/shipping allocations
  are not assumed. Suggested citations are distinguished from accepted/model or configured
  sources in the reviewer screen.

## Results

| Case | Original affected-case assertions | Retry |
| --- | --- | --- |
| 026 — extraction, vendor logo | 28 / 29 | 29 / 29 |
| 077 — extraction, customer reference | 20 / 21 | 21 / 21 |
| 071 — classification, advertising line | 20 / 22 | 22 / 22 |
| 092 — classification, hosting line | 5 / 7 | 7 / 7 |
| 002 — tax, hosting | 2 / 5 | 2 / 5 |
| 008 — tax, advertising mismatch | 2 / 5 | 5 / 5 |
| 017 — tax, advertising | 2 / 5 | 5 / 5 |
| 052 — tax, freight | 2 / 5 | 5 / 5 |
| Total | 81 / 99 (81.8%) | 96 / 99 (97.0%) |

Invoice 002's hosting treatment was selected but the software/services citation had only
67% source confidence, below 85%. It correctly remains in Finance review, with the rejection
now explicit. This is a remaining unresolved check, not a provider execution failure.

All eight model executions completed. Estimated retry cost: $0.004821072. The source ledger
was opened read-only and live invoice count stayed zero. Ninety-one regression checks and
strict type checks passed. The 52 previously untested cases remain untested. The retry is
stage-isolated and does not establish end-to-end or scanned-image accuracy.

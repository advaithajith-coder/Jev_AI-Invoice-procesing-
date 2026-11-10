# Pilot comparison — extraction x6 and finance3

Original run: `65fa740b-932b-40b3-aa10-11d79834a12c`
Comparison run: `30f45c58-9ce1-418b-8014-579111e9e525`

Both runs use the same frozen dataset, expected assertions, eight invoices, providers and
confidence thresholds. The original outputs are unchanged.

| Segment | Original assertions passed | Revised assertions passed |
| --- | --- | --- |
| Extraction | 51 / 58 | 58 / 58 |
| Classification | 11 / 15 | 15 / 15 |
| Tax | 10 / 10 | 10 / 10 |
| Routing | 22 / 22 | 22 / 22 |
| Total | 94 / 105 (89.5%) | 105 / 105 (100%) |

All eight cases completed. The other 92 remain unrun. The live invoice count stayed zero.
Revised batch time: 9.83 seconds; estimated model cost: $0.003538302.
Original batch time: 9.66 seconds; estimated model cost: $0.003291582.
These small timing samples do not establish a reliable performance difference.

## Changes verified

- Extraction x6 explicitly requires null quantity and unit price when unprinted, preserves
  printed quantities on goods lines, and joins wrapped description continuation text.
- Invoice 015 now keeps null quantity and unit price.
- Invoice 016 now preserves all five full freight descriptions.
- Finance3 requests supporting quotations separately for each field, deduplicates identical
  quotation options, and records evidence selection/confidence and rejection reasons.
- Hosting without software access is described as a professional service in category options.
- Invoices 038 and 078 now accept professional-service categories with supporting line text.
- PO GL values still take precedence. Unsupported departments and projects remain empty.
- Reviewers can see withheld suggestions and rejection reasons.
- Evaluation summaries report GL, category and evidence-presence accuracy separately.

## Remaining review

The 100% result covers the frozen assertions, not all accounting recommendations. Both
classification cases still require review of accounting treatment and service/asset detail
requirements. The application retains those uncertain fields rather than lowering thresholds.
Evidence presence is measured mechanically; its relevance still requires review.

This is a stage-isolated text-PDF pilot. Scans, unseen cases and complete invoice processing
remain separate tests. Twenty-three automated checks passed, type checks passed, and the
frontend build succeeded. No expected answers were changed to obtain the improved score.

# Eight-invoice pilot — 6 October 2026

Run: `65fa740b-932b-40b3-aa10-11d79834a12c`

Eight cases completed without provider failures. The other 92 remain unrun.
The live invoice count remained zero. Model inputs excluded expected answers; the original
expected assertions and generated outputs remain immutable.

| Segment | Cases | Assertions passed | Critical cases passed |
| --- | --- | --- | --- |
| Extraction | 015, 016 | 51 / 58 (87.9%) | 1 / 2 |
| Classification | 038, 078 | 11 / 15 (73.3%) | 2 / 2 |
| Tax | 001, 006 | 10 / 10 (100%) | 2 / 2 |
| Routing | 019, 085 | 22 / 22 (100%) | 2 / 2 |

Overall, 94 / 105 assertions passed (89.5%). Seven of eight cases passed their critical
assertions. These are pilot results on two cases per stage, not overall production accuracy.
Classification critical checks currently cover GL coding, not category or evidence quality.
Nonempty evidence checks also require human review of the cited support.

## Findings checked against source documents

- **015, extraction:** the legal-service PDF prints only a description and amount. The model
  invented quantity `1` and unit price `2812.25`; both should remain null. These two critical
  assertions failed. The other extracted fields matched.
- **016, extraction:** all five freight descriptions lost their wrapped continuation text,
  including “carrier transport after sale, actual cost.” The PDF text contains those
  continuations. All critical numeric assertions passed, but the lost wording matters for
  downstream tax treatment.
- **038, classification:** approved PO GL `5000` was preserved. JEV suggested subscription
  with confidence `0.41`; the application withheld the category and supporting evidence.
- **078, classification:** JEV selected GL `6100` and suggested professional service with
  confidence `1.0`. The application's evidence requirement withheld the category because
  no supporting evidence was accepted. Both classification cases need Finance review.
- **001, tax:** the goods invoice's deliberate 5% tax mismatch was detected. Expected tax
  was `52.96` at 6.25%, compared with the printed `42.37`; the DOR citation matched.
- **006, tax:** the pure legal-service case matched the expected zero-tax treatment and source.
- **019, routing:** the two delivery findings retained Delivery team ownership.
- **085, routing:** the missing-PO and approval-limit referrals matched the allowed owners.

## Measurements

Nine model calls: two extraction, three classification, two tax and two routing.
Recorded token-rate cost estimate: **$0.003291582**. This uses configured rates and is not a
provider billing receipt. Batch elapsed time was **9.66 seconds**; summed per-case time was
**8.98 seconds**. All calls had usage telemetry and estimated costs.

The measured Groq model was `openai/gpt-oss-120b`; JEV reported
`typesafe/jev-1.13-20260917`. Configuration and code hashes are in the run manifest.

## Before expanding the test

Review extraction instructions for absent quantity/unit-price fields and wrapped line
descriptions. Inspect the classification evidence-choice prompt and accepted-confidence
behavior. Keep this baseline unchanged, then compare any fixes in a new run. Do not treat
the two classification critical passes as successful complete line classification.

This pilot uses text PDFs and supplied upstream inputs for later stages. Scanned-image
quality, PO matching, final approval, and full end-to-end processing still need separate tests.

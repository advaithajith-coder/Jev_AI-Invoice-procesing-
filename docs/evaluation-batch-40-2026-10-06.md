# Forty additional invoices — 6 October 2026

Run: `30f45c58-9ce1-418b-8014-579111e9e525`
Batch: `batch-40-29b338eda83f`

Ten previously unrun cases per segment were selected across each manifest. All 40
completed without provider errors; 439 / 457 assertions passed (96.1%). Thirty-four
cases passed every critical assertion. The earlier eight outputs were preserved.

| Segment | Cases | Assertions passed | Critical cases passed |
| --- | --- | --- | --- |
| Extraction | 10 | 244 / 246 (99.2%) | 8 / 10 |
| Classification | 10 | 87 / 91 (95.6%) | 10 / 10 |
| Tax | 10 | 38 / 50 (76.0%) | 6 / 10 |
| Routing | 10 | 70 / 70 (100%) | 10 / 10 |

Batch elapsed time: 139.79 seconds, including extraction capacity waits. Estimated
model cost: $0.019084308 from 42 calls. Provider token rates are configured estimates,
not billing receipts. One routing case had no exceptions and correctly made no model call;
the current telemetry completeness counter flags its empty call list.

## Differences

- Extraction 026 included the decorative `NW` logo in the vendor name. The generator
  draws `NW` separately, but plain PDF text places it beside Pixelforge Marketing LLC.
- Extraction 077 treated customer reference `NORTHWIND-TEST` as a PO reference.
- Classification 071 (line 3) and 092 withheld categories because evidence confidence
  was below the acceptance threshold. All 12 scored GL-account assertions passed.
- Tax 002, 008, 017 and 052 left one or more line treatments uncertain and referred
  them to Finance. This prevented calculation of a supported invoice-wide expected tax
  or match result, producing three failed assertions per invoice. These were review
  referrals, not provider failures or accepted incorrect tax rates. Invoice 008 is a
  deliberate tax mismatch; the application did not establish the exact expected tax.

The run now contains 48 completed cases, zero execution failures and 52 unrun cases.
Cumulative assertion accuracy is 96.8%; cumulative estimated cost is $0.022622610.
Live invoice count remained zero before and after the batch.

The original expected answers and outputs were unchanged. Further fixes and retries
should use a new comparison run. These remain isolated stage tests, not end-to-end
approval or scanned-image accuracy results.

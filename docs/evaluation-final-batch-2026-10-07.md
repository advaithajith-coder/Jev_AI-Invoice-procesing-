# Final evaluation batch and completed baseline

Run `30f45c58-9ce1-418b-8014-579111e9e525` now contains saved outputs for all 100 unique invoices: 25 each for extraction, classification, tax and exception routing. There are zero execution failures and zero untested cases. The website API confirmed these counts.

## Final 26 cases

| Segment | Cases | Passed / checked | Field accuracy |
| --- | ---: | ---: | ---: |
| Extraction | 6 | 218 / 218 | 100% |
| Classification | 6 | 68 / 74 | 91.9% |
| Tax | 7 | 17 / 35 | 48.6% |
| Routing | 7 | 62 / 62 | 100% |
| Total | 26 | 365 / 389 | 93.8% |

Elapsed time: 82.70 seconds, including provider waits and retries. Estimated model cost: $0.009775806 across 34 calls. Case 049 encountered a rate-limit response, retried successfully, and completed in approximately 27.7 seconds. No models or prompts were changed for this batch.

Twenty-four assertions differed across seven cases. Classification case 058 had three missing category/evidence pairs; all ten GL account checks passed. Tax cases 005, 014 and 023 (office premises rent) selected no recognized source section. Cases 011 and 029 (managed hosting) selected the software section with source confidence of 73% and 79%. Case 021 (IT consultancy) selected an advertising source with 58% confidence. These six cases withheld rate, amount and match decisions and require Finance review. Each contributes three failed expected-output assertions.

Outputs, frozen manifest, summary and differences are in `data/files/evaluation-pilots/30f45c58-9ce1-418b-8014-579111e9e525/batch-26-505e773fcdf1/`.

## Full 100-case run

| Segment | Cases | Passed / checked | Field accuracy |
| --- | ---: | ---: | ---: |
| Extraction | 25 | 711 / 713 | 99.7% |
| Classification | 25 | 258 / 276 | 93.5% |
| Tax | 25 | 83 / 125 | 66.4% |
| Routing | 25 | 205 / 205 | 100% |
| Total | 100 | 1257 / 1319 | 95.3% |

Cumulative estimated cost for this run is $0.043124874, excluding separate retry runs. Results include multiple recorded prompt/model versions; this is completed coverage, not a single-version benchmark. Field accuracy is not the percentage of invoices entirely correct. Evidence nonempty checks still require human verification of meaning. These are isolated stage evaluations, using text PDFs for extraction and supplied upstream data for other stages, not full end-to-end or scan/photo testing.

The live invoice ledger contained zero invoices before and after the final batch. No live payables were created or modified.

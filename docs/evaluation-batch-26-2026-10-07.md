# Evaluation batch: 26 additional invoices

Completed on October 7, 2026, using the configured live models and improved checks. Run: `30f45c58-9ce1-418b-8014-579111e9e525`.

All 26 cases were previously untested. This batch completed without execution failures, bringing coverage to 74 unique invoices; 26 remain untested.

| Segment | Cases | Passed / checked | Field accuracy |
| --- | ---: | ---: | ---: |
| Extraction | 7 | 191 / 191 | 100% |
| Classification | 7 | 88 / 96 | 91.7% |
| Tax | 6 | 18 / 30 | 60% |
| Exception routing | 6 | 51 / 51 | 100% |
| Total | 26 | 348 / 368 | 94.6% |

Batch elapsed time was 100.64 seconds. Estimated model cost was $0.010726458 across 34 recorded calls. Provider capacity waits and retries contributed to elapsed time. These are field-level checks, not a claim that 94.6% of invoices were entirely correct.

## Differences requiring investigation

Twenty assertions differed across six invoices:

- Classification case 062 withheld a purchase category and evidence on line 3. Case 070 withheld those fields on lines 3–5. All 13 GL account checks passed. Supporting evidence still requires human review for meaning, beyond the automated nonempty check.
- Tax cases 003, 012, 018 and 027 withheld a final rate, tax amount and rate-match decision because citation confidence was respectively 51%, 55%, 79% and 82%, below the configured 85% threshold. These are unresolved decisions referred to Finance rather than incorrectly accepted rates. Each unresolved case accounts for three failed expected-output assertions.
- Extraction and routing matched every expected field in this batch.

Routing case 041 had no exception and correctly made no model call. Its empty call list appears in the aggregate telemetry counter; it is not an execution failure.

## Saved outputs and isolation

The batch folder is `data/files/evaluation-pilots/30f45c58-9ce1-418b-8014-579111e9e525/batch-26-de599fc08935/`. It contains the frozen manifest, individual generated outputs, summary and differences. Expected outputs remain in the evaluation dataset and are not supplied to the models.

The live invoice ledger contained zero invoices before and after evaluation. Evaluation results did not create live payables. No additional cases were run after these 26.

The cumulative visible run has 74 completed cases, zero execution failures, 26 untested cases, 95.9% field accuracy and $0.033349068 estimated model cost. It contains results from multiple recorded model/prompt versions; use the batch manifest when comparing improvements.

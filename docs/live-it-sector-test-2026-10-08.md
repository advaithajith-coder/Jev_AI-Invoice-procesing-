# Live IT sector test — 8 October 2026

Prepared 20 synthetic invoice copies for Northwind Analytics Inc. Processed the 16
development invoices through the live application's ordinary upload pipeline.
Four reserved invoices remain unprocessed and their POs/deliveries are not seeded.
The original 200-invoice industry benchmark is unchanged.

## Where to view

- [Live invoices](http://localhost:8000/inbox)
- [Human review queue](http://localhost:8000/review)
- [Purchase orders](http://localhost:8000/purchase-orders)
- [Delivery Management](http://localhost:8000/receiving)
- [Expected versus actual, costs and timing](http://localhost:8000/evaluations?run=8ea96536-d057-4fec-8498-eabe12e36728)

All 16 invoices finished processing without pipeline failures, and all 16 currently
await review. Completion means processing finished; it does not mean approval.
No reviewer decisions or payments were performed.

## Live master data added

| Synthetic vendor | Default GL | PO required |
| --- | --- | --- |
| Synthetic IT Office Supply | 6000 Office Supplies | Yes |
| Synthetic IT Equipment Supply | 1500 Computer Equipment | Yes |
| Synthetic IT Software Services | 6010 Software Subscriptions | No |
| Synthetic IT Consulting | 6020 IT Services and Support | No |
| Synthetic IT Project Services | 6020 IT Services and Support | No |
| Synthetic IT Freight Services | 6800 Courier and Postage | No |
| Synthetic IT Telecom | 6500 Telecommunications | No |

Each vendor has a stable synthetic tax ID shared across its invoices. No bank
accounts or payment instructions were added. Existing vendors, chart of accounts,
company policies and historical POs were retained.

Added 11 POs: seven services with Needs receiving off, and four goods with it on.
Recorded three deliveries: two partial and one complete. A fourth goods PO has no
receipt deliberately. Five development invoices have no PO; some are services
permitted without one, while the goods cases should raise missing-order reviews.

## Deliberate matching cases

| Invoice | PO/delivery state | Expected control and owner | Observed |
| --- | --- | --- | --- |
| NW-LIVE-IT-4-2 | Goods, no PO | NO_PURCHASE_ORDER / Purchasing | Raised and routed correctly |
| NW-LIVE-IT-4-3 | Goods, half received | QUANTITY_VARIANCE / Delivery team | Raised and routed correctly |
| NW-LIVE-IT-4-4 | Goods, no delivery | QUANTITY_VARIANCE / Delivery team | Raised and routed correctly |
| NW-LIVE-IT-4-5 | Goods, full delivery, invoice price higher than PO | PRICE_VARIANCE / Purchasing | Raised and routed correctly |

NW-LIVE-IT-3-5 also combines an unsupported exemption claim with partial delivery.
NW-LIVE-IT-2-5 purchases two $1,200 laptops without a PO, so capitalization and
missing-PO reviews are appropriate under the existing company policy.

## Evaluation results

Each invoice has one primary evaluation segment but went through the full live
pipeline. Other controls can therefore hold an invoice whose primary segment passed.

| Primary segment | Invoices tested | Field checks passed |
| --- | --- | --- |
| Extraction | 4 | 70/72 (97.2%) |
| Classification | 4 | 18/20 (90.0%) |
| Tax | 4 | 10/16 (62.5%) |
| Exception routing | 4 | 16/16 (100%) |
| Total | 16 | 114/124 (91.9%) |

Classification GL values were correct on 4/4 lines: three AI selections and one
PO-inherited value. Purchase category and nonempty supporting evidence passed on
3/4. Evidence presence does not establish that its meaning is correct.

Recorded 90 model calls, estimated/reported total cost **$0.022952964**, and summed
invoice execution time **232.686 seconds**. This excludes upload queue time and may
omit SDK-internal retry charges; it is not a guarantee of complete billed cost.
Digital PDFs use native text extraction, so this batch does not evaluate camera
or scan vision quality.

## Findings to resolve before scaling

1. **Payment-risk review: 16 invoices.** JEV risk scores were 1.52–1.85 on the
   application's 0–2 scale, so the existing high-risk policy held every invoice.
   Synthetic labels and absent payment instructions may influence this, but this
   batch cannot prove the cause. Compare neutral synthetic documents in a separate
   controlled test; do not lower risk thresholds just to increase approvals.
2. **Tax review: 13 invoices.** Source/treatment confidence often missed the 85%
   threshold. The software tax case selected a source with 67% confidence; consulting
   had treatment confidence 54% and source confidence 80%; phone source confidence
   was 84%. The phone's deliberately incorrect 5% rate therefore did not produce an
   accepted numerical expected tax: review triggered on uncertainty first. The
   unsupported exemption correctly needed Finance, but also had source uncertainty.
3. **Vendor extraction: two invoices.** Layout 5 contains a decorative NW mark.
   Extraction returned "NW Synthetic IT Freight Services" and "NW Synthetic IT
   Office Supply", which did not match the registered vendors. Do not register those
   accidental names as aliases merely to make this test pass.
4. **Equipment category: one classification case.** The GL selected 1500, but the
   purchase category/evidence remained unresolved. Finance review is safer than
   silently accepting the incomplete classification.
5. **Duplicate warnings: eight invoices.** Some stage copies share vendor, invoice
   date and amount. Their invoice numbers/files are distinct; these are possible-
   duplicate warnings rather than duplicate-file uploads. Vary ordinary-case dates
   and amounts in the next suite and reserve repeated values for explicit duplicate
   scenarios so this control does not confound other measurements.

These flags overlap; their counts cannot be added to count reviewed invoices.

## Saved records and reproducibility

Fixtures, PDFs, expected assertions, API outputs and per-step/model measurements:
`invoice generator/sector runs/technology-live-v1/`.

`plan.json` records supplier identities, source-case lineage, PO lines, receiving
scenarios and split. `seeded.json` records the live vendor/PO IDs. `actual/` retains
the live responses and invoice links. `rescored-comparisons/` and
`rescored-summary.json` contain the current comparisons. A local SQLite backup was
made before seeding.

Two evaluation records reference **one live processing batch**. The first kept raw
display labels; the current one normalizes "Delivery team" to the expected
"delivery" key. No invoices were uploaded twice and no extra model calls were
made to correct that label comparison. Raw decisions and the original evaluation
record remain available.

The reusable runner is `python -m jev_invoice.evaluation.live_sector` with actions
`prepare`, `seed`, `run`. Preparation refuses an existing nonempty folder. Seeding
checks existing test records and records only missing receipt quantities. Running
skips reserved cases and saved results. `run --rescore` requires saved outputs and
does not invoke models. This first runner is IT-specific; each next industry needs
its own coherent live purchase fixtures and expected answers before processing.

Validation: all 20 PDFs checked for supplier/buyer identity and arithmetic; goods
and service layouts visually reviewed; 9 focused regression tests passed; changed
files passed Ruff and strict typing. The reserved set is too small to establish
production accuracy, and the current results are diagnostic demo measurements.

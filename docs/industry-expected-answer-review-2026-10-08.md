# Expected-answer review: 200 industry invoices

Reviewed on October 8, 2026. Run: `71186ac3-71c6-4167-b1d9-43016eb5dfc0`.

**The arithmetic and transcription expectations reconcile. The benchmark needs
several adjustments before its scores can be presented as broad model accuracy.**
No expected answers, invoice files, application records or model outputs were changed.
The run still contains zero generated model results.

## What was checked

All 200 invoice PDFs were opened and checked for supplier, buyer and invoice identifiers,
line descriptions and printed amounts. Line extensions, subtotals, printed tax, totals
and balances were independently recalculated with Decimal arithmetic. Assertions were
compared with source records, the configured synthetic chart of accounts and documented
scenario assumptions rather than regenerated from the same expected-answer function.
The frozen database inputs and expected answers match the current files. PDF hashes
match their manifests. There were **zero mechanical discrepancies** in those checks.

| Segment | Cases | Finding |
| --- | ---: | --- |
| Extraction | 50 | Checked values match source records and readable PDFs. This is text-PDF extraction, not vision. |
| Classification | 50 | Codes and categories agree with the stated demo fixtures, subject to company-policy confirmation. Twenty cases inherit PO coding; thirty require JEV coding. |
| Tax | 50 | Thirty expected matches, ten deliberate rate mismatches and ten unresolved exemption claims have consistent expected amounts/review outcomes under their assumptions. Citation alternatives and source version need attention. |
| Routing | 50 | Expected responsible teams are reasonable for the supplied findings. Forty findings need reconciliation with their attached invoice evidence before end-to-end reuse. |

## Adjust before the baseline run

### 1. Citation assertions can reject supported alternatives

**Ten software cases** require only the guide's Software & Digital Goods link, even
though the current application also accepts the computer-software regulation. That
regulation directly supports taxable paid software access. **One technology consulting
case**, `NW-IND-01-3-3`, requires only the professional-services guide section; the
computer-services regulation is also directly relevant to its separately billed IT
service with no software or goods.

Accept these item-relevant alternative citations in a new dataset version. Do not accept
every source indiscriminately: a computer-services section is not automatically an
appropriate citation for legal, construction or clinical consulting.

Official support: [830 CMR 64H.1.3](https://www.mass.gov/regulations/830-CMR-64h13-computer-industry-services-and-products),
particularly (3) for software and (14) for computer services.

### 2. Routing findings and PDFs describe different scenarios

| Finding | Cases | Issue | Example |
| --- | ---: | --- | --- |
| Missing PO | 10 | The attached invoices contain PO references. A reference can be invalid, but that invalidity is not evidenced in these fixtures. | `NW-IND-01-4-2` has `PO-IND-01-4-2`. |
| Incorrect 5% tax on office goods | 10 | The attached PDFs are professional-service invoices with 0% printed tax. | `NW-IND-01-4-3`: server setup consulting, tax 0.00. |
| Above approval limit | 10 | All are below the earlier demo limit of 5,000; no lower applicable limit is supplied to the routing input. | `NW-IND-01-4-4`: total 589.55. |
| PO price variance | 10 | The attached invoices are freight invoices with no PO reference, and no PO price is supplied for comparison. | `NW-IND-01-4-5`. |

These can still test routing of **supplied standalone findings**. They cannot demonstrate
that the application correctly detected the exception from the associated invoice.
Either explicitly label them as standalone routing scenarios, or align the PDF, PO,
delivery and limit fixtures with each finding before full-workflow testing.

The remaining ten goods-delivery findings also depend on supplied findings, not actual
receipt records in this suite. Absence of delivery is an authored premise rather than a
verified result of Delivery Management.

### 3. Classification coverage overstates independent goods/software AI testing

All ten goods cases and all ten subscription cases have approved PO GL and category
values. Their expected answers are valid inheritance checks. They do **not** establish
JEV's independent ability to classify inventory, equipment or software subscriptions.

The thirty non-PO cases test professional services, milestones and freight. Include
some goods and subscriptions without approved PO coding within the fifty-case allocation.
Report PO inheritance separately from model suggestions. No department/project positive
examples or service-date/asset-detail assertions exist in the current set.

The technology monitors case, `NW-IND-01-2-1`, also illustrates a company-policy assumption:
it expects equipment GL 1500 at unit prices below 1,000. The suite says monitors are
equipment; the current application policy references capitalization review at 1,000 per
item. Inheriting an explicit PO code is what this case tests. Whether those inexpensive
monitors should actually be capitalized needs a consistent approved company policy;
this review does not treat either policy as a universal accounting rule.

### 4. The tax-source snapshot has changed

All fifty tax expected files record a source hash different from the current
`ma_guide.json`. The current source includes additional computer regulation sections,
and the application uses tax rules version v5. This does not itself mean the recorded
tax rates are wrong, but it prevents treating the old source hash as the exact evidence
used by a new run.

Freeze the current source passages and their retrieval/version metadata with revised
expected answers before the baseline. Preserve v1 and prior runs; use a new frozen dataset
for corrections rather than silently changing the expectations of an existing run.

## Other interpretation and data-quality limits

- **Evidence:** all fifty classification cases use nonempty evidence checks. Tax cases
  likewise check the presence of an evidence container. Neither establishes that the
  selected quotation or citation supports the item. Use a semantic review checklist
  covering relevant passage, correct item and absence of unsupported claims.
- **Routing actions:** all fifty cases only require a nonempty next action. An action
  that addresses the wrong control could pass. Score allowed resolution actions per
  finding while retaining appropriate supplier-clarification alternatives.
- **Code-enforced routing:** ten tax routes are forced to Finance by application code.
  Those results measure a guardrail, not JEV ownership selection, even if a model was called.
  Routing evidence is copied from the supplied finding rather than independently sourced.
- **Vendor identities:** fifty recurring supplier names each have four different tax IDs
  across their extraction, classification, tax and routing PDFs. Per-document extraction
  expectations are faithful, but this is inconsistent vendor master data. Standardize
  identities before uploading the suite into a real business workspace or evaluating vendor checks.
- **Policy reproducibility:** the runner freezes synthetic accounts/vendors but obtains
  accounting policy settings from the live ledger for each batch. `suite.json`'s free-text
  policy is not automatically the structured policy supplied to classification. Freeze
  applicable policies once for the baseline, and align evaluation/live industry context.
- **Exemptions:** the ten exemption cases correctly expect Finance review and no resolved
  tax amount. Certificate absence does not, by itself, legally decide whether a purchase
  qualifies. The data do not establish eligibility, so review is the safe expected outcome.

## Tax assumptions verified against official material

The determinate scenarios explicitly describe Massachusetts business use: ordinary
office goods without a supported exemption, paid software access, pure advisory
services without goods/software, and business telephone service excluding Internet
access. The expected 6.25% or zero-rate treatment is supported under those narrow
assumptions by the [DOR guide](https://www.mass.gov/guides/sales-and-use-tax),
[computer regulation](https://www.mass.gov/regulations/830-CMR-64h13-computer-industry-services-and-products)
and [telecommunications guidance](https://www.mass.gov/info-details/sales-tax-on-telecommunications).
Exempt-use eligibility and certificate requirements are described in
[830 CMR 64H.8.1](https://www.mass.gov/regulations/830-CMR-64h81-resale-and-exempt-use-certificates).
Industry membership is not enough to determine tax treatment.

The direct guide fetch returned HTTP 403 during this review; its indexed official
content and the project's source snapshot were available. No claim was made that every
rendered guide anchor was independently navigated successfully.

Detailed case flags and exact counts are in
`industry-expected-answer-review-2026-10-08.json`. This is a technical fixture review
against official sources and stated assumptions, not company Finance sign-off.

# Parser.AI: detailed invoice workflow

Code reviewed: 11 October 2026. This document describes the implemented workflow, including its limits. Configuration values below are code defaults; environment settings and saved company settings may override them.

## 1. What the application does

Parser.AI helps Northwind Analytics Inc process supplier invoices. It reads documents, checks them against company records, proposes accounting classifications, checks configured tax treatments and sends exceptions to a reviewer. Approved invoices can be exported for import into accounting software.

The application combines three types of work:

| Handler | Responsibility |
| --- | --- |
| Application code | File handling, arithmetic, dates, vendor records, duplicate checks, PO/delivery comparisons, policy thresholds, tax calculations, records and status changes |
| Extraction and vision models | Read document content and convert it into structured invoice fields |
| JEV | Assess document type and risk, suggest missing classifications and tax treatments, and recommend exception ownership |
| Human reviewer | Investigate exceptions, correct information, confirm accounting treatment, validate reusable corrections and approve or reject |

JEV recommendations do not override failed application controls. Approval is a stored application status, not a payment or an automatically posted journal entry.

## 2. Prepare the business records

### Vendor records

The vendor list is the reference for supplier identity, status, usual currency, tax ID, bank fingerprint, email domain, default GL account and whether a PO is required. A model reading a supplier name does not establish that the supplier is approved: code compares that name with the vendor records.

Unknown vendors, vendors on hold and blocked vendors produce different findings. Printed bank details are compared with the vendor's saved fingerprint when relevant information is available. The application stores a fingerprint and last four digits rather than the full bank account number.

### Purchase orders

Create the PO before the invoice where possible. Enter the vendor, reference, dates and line descriptions, quantities and prices. PO lines can also carry accounting information such as an account, purchase category, department/project, service dates and asset information.

Tax rates on PO lines help show the intended subtotal, tax and total when creating an order. Invoice tax checking is a separate process using the configured Massachusetts treatments and invoice evidence; a PO rate alone is not proof of correct treatment.

**Needs receiving is an order-level setting.** Goods orders use it to require delivery comparisons. Service orders leave it unticked and use an invoice-to-PO comparison. This setting controls whether an order needs receiving; the system does not require receiving for every purchase.

### Delivery Management

For goods, record accepted quantities against the correct PO lines and delivery date. Missing or damaged goods can be recorded as delivery issues and followed up separately. Matching uses the recorded received quantities: a delivery issue report by itself does not establish that all goods were accepted.

Goods invoices can arrive before delivery. They can be read and checked, but billed quantities exceeding recorded deliveries produce a review finding.

## 3. Receive and store the invoice

The screen upload first validates and stores the file. It calculates a SHA-256 fingerprint of the original bytes and checks whether the same file already exists.

An exact repeat upload returns the existing invoice rather than creating another processing job. This differs from business duplicate detection: two different files can still represent the same supplier invoice and will be compared later.

The original is stored locally under the configured file storage directory, and an invoice record captures the filename, content type, source and available sender information. Local defaults are a SQLite database under `data/` and original files under `data/files/`.

Screen uploads normally return promptly and run the remaining steps in the background. The JSON upload API processes before responding. Background jobs use their own database sessions. Invoices left in an in-progress status can be picked up after restart; interrupted processing steps are cleared and rerun, so restart recovery is not a promise of zero repeated model calls.

## 4. Read the file and establish coverage

Code parses the input and determines which pages already contain usable text and which need image transcription. It records information about pages read, image pages and omitted pages.

Text-based files proceed to extraction. Scans, photographs and pages requiring transcription go through the vision path. Reading the full document matters: an incomplete document is held for review and cannot be approved through the normal reviewer approval path. The reviewer is directed to reject the incomplete copy and upload a complete readable version.

Ordinary processing failures are recorded as failures with an explanation. They are distinct from a readable invoice that has a business exception.

## 5. Transcribe scans and photographs

The configured vision model runs through local Ollama. Its job is to turn page images into text, preserving the information needed for extraction. The code default model is `glm-ocr:q8_0`; actual configuration may differ.

Page and image-size limits bound this work. Code defaults are five image pages and a longest image dimension of 1,200 pixels. Coverage controls prevent omitted reading from silently becoming an approval.

Images on this configured local vision path stay on the machine. The resulting text is subsequently supplied to external extraction and JEV providers as needed. Local image processing therefore does not mean every later processing step is entirely local.

Files with usable text skip vision. This is one reason invoice processing times differ.

## 6. Extract structured fields

The extraction service sends document text to the configured Groq model and requests structured invoice data. The code default model is `openai/gpt-oss-120b`.

The output can include vendor and buyer information, invoice number, invoice/due dates, currency, PO reference, descriptions, line quantities, unit prices, line amounts, subtotal, discounts, shipping, tax, total, amount paid and balance due. Fields depend on what the document provides and what extraction successfully reads.

The application stores the raw extraction, structured invoice and lines, provider/model information, prompt version and reported or estimated cost. Successful extraction changes the invoice to the extracted status; it does not mean the invoice passed validation.

Rate-limit waits, retries and output validation can affect the duration. Extraction reads what the invoice says; later checks establish whether those values agree with records and rules.

## 7. Run document checks in code

These checks are deterministic and do not ask JEV to perform arithmetic.

| Check | Implemented purpose |
| --- | --- |
| Required fields | Vendor name, invoice number, invoice date and total must be present |
| Line arithmetic | Where quantity, unit price and amount exist, compare quantity × unit price with the line amount |
| Invoice arithmetic | Compare available line totals, subtotal, discount, shipping, tax, total, paid amount and balance |
| Dates | Flag future dates beyond the one-day allowance, inconsistent due dates and stale invoices |
| Currency | Use the base currency or obtain an exchange rate for the invoice date; missing conversion information requires review |
| Bill-to company | Compare the invoice's buyer with the configured company name |
| Approval limit | Compare the total in the base currency with the approval threshold |

Code defaults include a $0.02 arithmetic tolerance, a 180-day stale-invoice threshold and a $5,000 approval limit. The actual approval limit can come from saved settings. Foreign-currency conversion supports checking amounts against base-currency limits; the Massachusetts tax checker still flags non-USD invoices for review.

Checks collect findings rather than stopping at the first discrepancy. A reviewer can see multiple problems on one invoice.

## 8. Match vendor records and detect duplicates

Vendor names are normalized to reduce differences in punctuation, capitalization and company suffixes. Matching then checks vendor status and available currency, bank, tax-ID and email information.

Tax IDs are compared when both the vendor record and invoice supply one. Email-domain controls apply to available sender information and can flag suspicious lookalike domains. Missing data does not create evidence that a comparison was performed.

Duplicate controls distinguish:

- **Exact file duplicate:** the original bytes were already received; detected during upload.
- **Business duplicate:** the same vendor and invoice number appeared before.
- **Possible duplicate:** suspiciously similar invoice numbers or another invoice from the vendor for the same amount within the configured date window. The code default window is seven days.

A possible duplicate is a review signal, not proof of fraud. Recurring legitimate invoices and synthetic test fixtures can trigger it.

## 9. Compare invoice, PO and deliveries

The application looks for the quoted PO and checks that it belongs to the vendor, is usable, has a compatible currency and is not dated after the invoice. A missing reference matters when the vendor requires a PO; a supplied reference that cannot be found is a separate exception.

Invoice lines are paired with PO lines using description similarity or a unique matching price. This is an application matching heuristic, not an infallible semantic match.

### Price comparison

For paired lines with enough numerical information, code compares the billed price with the PO price. The implemented price control flags **overcharges** exceeding the permitted tolerance; it does not flag every lower price as a variance.

The allowed difference is the smaller of the configured percentage and absolute cap. Defaults are 2% and $5. The line calculation considers the billed quantity when comparing the overcharge.

### Quantity comparison

Code adds the current billed quantity to quantities already billed on relevant earlier invoices against the same PO line.

- For goods orders requiring receiving, cumulative billed quantities must not exceed recorded received quantities or the ordered quantities.
- For service orders, cumulative billed quantities are checked against the order without requiring a delivery receipt.

This prevents treating each partial invoice as if it had access to the full PO quantity again.

### When full line matching is unavailable

If lines cannot be paired or enough quantity/price information is missing, the system falls back to cumulative pre-tax amount comparisons against PO value and, for receiving orders, recorded delivery value. Unmatched-line findings remain relevant: a total comparison is not evidence that every line matched.

The reviewer should therefore distinguish a complete line-level match from an amount-level fallback.

## 10. Ask JEV for document judgment

JEV receives document text and answers typed questions about document type, whether the document requests payment, attempted instructions to the software, bank-detail changes, urgent payment pressure, unusual payment methods and payment risk.

These answers contain probabilities, confidence or a risk score. They are not a direct command to approve the invoice. Application policy converts them into findings using fixed thresholds.

Current policy defaults include:

| Signal | Threshold behavior |
| --- | --- |
| Document type | At least 85% confidence is needed to act confidently on the selected type |
| Payable document | Automatic approval needs at least 85%; at or below 30% indicates not payable, except for the credit-note handling |
| Ordinary risk signals | At or below 30% is clear; intermediate values create uncertainty; 70% or above establishes the relevant signal |
| Prompt injection | 50% or above creates the prompt-injection finding |
| Payment risk score | At least 0.75 is moderate; at least 1.5 is high, on the 0–2 scale |

Credit notes are sent for human handling rather than treated as ordinary invoices asking for payment. If JEV is missing or unavailable, the policy requires review.

The document-judgment call does not perform the vendor/database checks. Other JEV calls later receive the context required for their separate classification and routing tasks.

## 11. Classify and code each line

### GL account selection

The implemented precedence is:

1. The account on the matched PO line.
2. An applicable validated correction for the recurring purchase.
3. A sufficiently confident JEV account choice.
4. The vendor's default GL account.
5. The configured suspense account when no account is available.

JEV chooses from eligible active accounts provided by the application, rather than inventing an account code. The GL suggestion threshold is configurable; its code default is 60%, separate from the tax treatment threshold of 85%. This determines which suggestion is stored, not whether the invoice can be automatically approved. Even a high-confidence JEV GL choice requires Finance review.

The account's source and available confidence are stored with the line. A fallback account is not proof that the model classified the purchase correctly.

### Finance classification

The line classification records purchase category, accounting treatment, department/project where supported, whether service dates or asset details are required, and supporting evidence.

Purchase categories currently include goods, subscription, rent, utilities, professional service, milestone and other, with an unknown option when evidence is insufficient. Freight is classified according to the freight service, not the goods being transported.

Available matched PO finance fields take precedence. Validated category corrections can fill missing category information. JEV is asked for missing judgments. Department/project suggestions are constrained to explicitly labelled candidate values in the supplied text; the model is not given unrestricted permission to invent organizational assignments.

Insufficient confidence or evidence creates classification review reasons. Unknown does not silently become a successful classification.

## 12. Apply company accounting policies

Application rules evaluate the classified lines using the saved company policy.

**Subscriptions:** subscriptions spanning more than one month, or identified as annual, are flagged for prepayment/allocation review. Service start and end dates help determine the period. An annual subscription without dates can require those dates before an allocation proposal is meaningful.

**Assets:** lines coded to an asset account or marked as requiring asset details are candidates for the configured per-item capitalization review. The default threshold is $1,000 in USD. Missing comparable cost can also require review. The system does not classify every expensive purchase as an asset purely because of price.

Finance can record a treatment resolution. A fingerprint connects that resolution with the policy, amounts, description, account, currency and relevant dates/details. If those inputs change, the previous confirmation no longer automatically satisfies the changed review.

The application keeps invoice date, recorded delivery dates, service dates and proposed posting period separately. A posting period can be proposed from the invoice date or chosen by a person. **The removed “last closed month” setting is not an active closed-period control.**

These checks do not generate amortization schedules or post capitalization journals automatically. They produce treatment proposals and review requirements.

## 13. Cross-check Massachusetts tax

This step runs when Massachusetts tax checks are enabled. The Tax rules page shows ten broad purchase categories and the conditions for their configured rates; it is a compact explanation, not an editable list of every exact description recognized internally.

The implemented lookup is:

1. Look for an exact configured description treatment.
2. Reuse a valid scoped tax correction when its version and treatment are compatible with the configured rule.
3. For an unresolved description, ask JEV to choose from supported treatments.
4. Accept a supported JEV treatment only at 85% confidence or above; otherwise keep it unresolved for Finance.

Supported treatments include 6.25% for configured taxable goods/software/telephone purchases and 0% for qualifying configured service/rent/freight scenarios. Their conditions matter. Unknown treatments and exemption claims require Finance evidence. The program does not infer exemption status from an organization's name.

**Tax calculation:** use accepted taxable line amounts, multiply the taxable basis by 0.0625 and round with Decimal half-up rounding to cents. Line amounts already include quantity; quantity is not multiplied a second time. Compare with invoice tax using a $0.01 tolerance.

Example: three units at $100 produce a $300 line amount and $18.75 expected tax. A separately billed qualifying untaxed $200 service line adds no tax. For the combined $500 subtotal, an overall displayed percentage of 3.75% can still be correct because only $300 is taxable.

The checker requires an established basis. Missing or negative line amounts, unexplained subtotal differences, missing tax, discounts/shipping needing allocation, non-USD invoices and exemption claims can require review. Zero supplier tax on potentially taxable goods raises possible exemption/use-tax follow-up rather than assuming the purchase is exempt.

The code also sends missing dates or invoice dates before 7 May 2026 for historical-policy review. This is an implemented configuration boundary, not a general statement that Massachusetts tax began on that date.

When some lines are accepted and others unresolved, the output can show tax for the accepted portion without declaring the entire invoice valid. The stored tax review contains per-line treatment, expected rates, confidence/source where available, expected amounts and review messages.

**Citations are no longer an approval requirement.** The current runtime check does not ask JEV to select a legal citation. This is a configured purchase-tax cross-check, not a comprehensive determination of every jurisdiction or exemption.

## 14. Make the application decision

The fixed policy combines deterministic findings with JEV judgment. It records every applicable reason and chooses the proposed action from the highest-priority reason group.

| Priority | Proposed action | Examples |
| --- | --- | --- |
| 1 | Fraud review | Bank mismatch, blocked vendor, unusual payment method, high payment risk |
| 2 | Hold for review | Prompt injection or missing/unavailable JEV |
| 3 | Reject document, subject to confirmation | Confident non-invoice, not payable or business duplicate |
| 4 | Hold for review | Untrusted/missing/suspense GL coding, tax/classification/accounting issues, totals, dates, vendor, PO, quantity/price or uncertainty findings |
| 5 | Route for approval | Non-PO spend, approval limit exceeded or required PO missing |
| No reasons | Auto-approve | A matched PO, trusted GL coding on every line and no outstanding reason codes |

Policy p11 adds two mandatory gates to both initial processing and record rechecks. Every invoice without a matched PO needs Budget owner approval, including vendors allowed to bill without a PO and invoices below $5,000. Every line must have a non-suspense GL account from its matched PO line or a validated correction to qualify for automatic approval. JEV coding at any confidence, vendor defaults, manually coded sources, missing accounts and suspense cannot authorize automatic approval. They create a Finance coding review; a reviewer can still approve through the human review process after investigation. A correction does not waive the Budget owner requirement for non-PO spend.

**All non-automatic outcomes create a pending review task.** Even a proposed document rejection is not an immediate final rejection. A person confirms the final outcome.

Decisions preserve action, band, reasons, findings, rationale, JEV answers/status/model, policy/question versions and input fingerprint. Decisions are added to the history rather than silently overwriting previous decisions.

## 15. Route and resolve exceptions

JEV recommends a route, an accountable internal team and a next action for each exception. If its answer is missing, invalid or insufficiently confident, application fallback rules assign ownership.

| Team or route | Typical responsibility |
| --- | --- |
| Purchasing | PO scope, prices, extra charges and supplier agreement |
| Delivery team | Accepted quantities, shortages, damage and delivery confirmation |
| Budget owner | Business purpose, service completion and project allocation |
| Finance | Accounting, tax, credit notes, duplicates and payment-security concerns |
| Supplier clarification needed | Request corrected or missing supplier information, with an internal team still accountable |

Security findings retain Finance ownership regardless of a model recommendation. Non-PO approval findings retain Budget owner ownership, and GL coding review findings retain Finance ownership. A model recommendation cannot redirect these mandatory controls. Routing does not change the approval decision, send supplier messages or execute payment.

The review screen brings together the invoice line, matched PO line, recorded accepted delivery, failed checks and available supporting evidence. Exceptions track owner, status and resolution reason. Current tax review uses treatment evidence and cross-check results rather than requiring citation selection.

## 16. Recheck corrected or waiting invoices

Eligible invoices waiting for review can be rechecked after updated records, including new deliveries. Rechecks reuse the extraction and saved JEV judgment rather than repeating normal model processing.

The process reruns document, vendor/PO, classification-findings and accounting/tax controls relevant to the recheck, records a new decision and refreshes exception evidence. It does not rerun full line classification in the ordinary recheck path.

Tax model assessments can be reused only when the saved version and evidence fingerprint still match. If changed evidence needs a new treatment assessment and the no-model recheck cannot supply it, review can remain necessary.

If no reasons remain, the invoice can become approved and the open review closes. Otherwise the review stays open. A manually imposed hold prevents the ordinary automatic recheck path from deciding it without human involvement.

For human approval, incomplete-document coverage and accounting treatment requirements are checked again. A fraud-blocked approval requires a note explaining verification. Demo warnings about role limits, second approvers or separation of duties are not equivalent to fully enforced enterprise authorization controls.

## 17. Reuse validated corrections

An ordinary reviewer decision does not retrain JEV. The separate corrections feature supports controlled reuse for recurring purchases, including GL account, purchase category and tax treatment corrections.

A correction records the source purchase, selected value, scope, evidence and validity period. It must be validated before reuse. Matching requires the vendor, buyer, currency, description scope and relevant date validity. Both the invoice date and present date must fall within validity. The code bounds validity to at most one year and prevents overlapping active corrections in the same scope.

Validated values must still be eligible. Tax corrections are tied to the tax policy version, and conflicting configured rates are not silently replaced. Expired, withdrawn or incompatible corrections stop applying. Reuse provenance is recorded so a reviewer can identify the source of an answer.

This improves repeat processing through explicit saved knowledge, without changing model weights or claiming automatic learning from every approval.

## 18. Dashboard, export and accounting

Approved invoices become available for the appropriate Dashboard/payables views and supported export operations. A requested export creates accounting import files for supported formats such as Xero, QuickBooks or CSV.

Export behavior depends on accounting configuration. The default US sales-tax handling treats supplier sales tax as purchase cost rather than reclaimable VAT. Account codes and target-system tax settings still need to suit the destination accounting setup.

Importing the file happens in the external accounting system. Approval/export does not automatically import a bill, create a payment, transmit funds or prove that the external ledger accepted the record. The flowchart's accounting node explains that external next step.

## 19. Measurements and evaluations

Pipeline records capture recorded steps, duration, summaries and details. Processing measurements also preserve job execution time and model calls. The evaluation system compares generated outputs with expected answers for extraction, classification, tax treatment and exception routing separately.

Cost/time comparisons need context:

- Text invoices skip vision; photographs and scans need additional work.
- PO data and validated corrections can avoid some classification questions.
- Exact tax descriptions can avoid the JEV tax-treatment call.
- Unresolved fields or exceptions add model questions and routing work.
- Provider pacing, retry waits and local vision speed affect execution time.
- Job execution time excludes upload queue time; recorded step timing and total job time are different measurements.
- Cost reports can be estimates from configured pricing, and SDK-internal retry charges may be unreported.

A correct final route does not establish that every intermediate field was correct. Evaluate both intermediate answers and final outcomes. Synthetic dataset accuracy is evidence about that dataset, not a guaranteed real-world rate across all industries.

## 20. How to read the flowchart

The visual sequence is upload → read → optional vision → extraction → document checks → vendor/PO checks → JEV judgment → line classification → company policy → tax cross-check → decision.

It then branches into approval/export or exception routing and reviewer resolution. Failed reading has its own path. Moving slips illustrate sample routes; numbered live markers identify invoices actually processing. Policy and tax appear as explanatory stages after coding even though the current step recorder does not assign each its own independent live timing step.

Use the information button on a node to read its handler and explanation. The flowchart illustrates what happens to an invoice; it does not mean every illustrated downstream external action is automatically executed.

## Code reference map

Paths are relative to the project root.

| Area | Main source |
| --- | --- |
| Processing sequence, persistence, rechecks | `src/jev_invoice/pipeline/service.py` |
| Matched-PO and trusted-coding auto-approval gates | `src/jev_invoice/pipeline/approval.py` |
| Background execution and restart recovery | `src/jev_invoice/pipeline/runner.py` |
| Coverage approval guard | `src/jev_invoice/ingest/coverage.py` |
| Arithmetic/date/company checks | `src/jev_invoice/domain/checks.py` |
| Vendor, duplicates and PO/delivery matching | `src/jev_invoice/matching/service.py` |
| GL coding and evidence-based classification | `src/jev_invoice/coding/service.py`, `coding/classification.py` |
| Validated correction reuse | `src/jev_invoice/coding/corrections.py` |
| Subscription and asset policy checks | `src/jev_invoice/accounting/controls.py` |
| Configured tax treatments and calculations | `src/jev_invoice/tax/massachusetts.py` |
| Decision priorities and thresholds | `src/jev_invoice/decision/policy.py` |
| Exception ownership | `src/jev_invoice/decision/routing.py` |
| Human approval/rejection | `src/jev_invoice/pipeline/review.py` |
| Evaluation scoring and telemetry | `src/jev_invoice/evaluation/` |
| Workflow map and explanations | `src/jev_invoice/web/templates/workflow.html`, `web/rules.py` |
| Default configuration | `src/jev_invoice/config.py` |

# Parser.AI: detailed behaviour

The finer rules behind the checks summarised in the [README](../README.md): how lines are
classified, who owns each exception, how Massachusetts tax is verified, and how purchase-order
tax and accounting periods are reviewed.

## Line finance classification

Matched PO lines supply approved GL coding and optional accounting details: purchase category,
department/project, service coverage dates, asset details, and whether these details are required.
Enter them under **Accounting details** when creating a PO. The API accepts `finance_details` on
individual PO lines. Existing POs remain valid with no additional details.

Jev supplies missing classifications using closed choices and explicit evidence. Departments and
projects are suggested only from explicitly labelled document values, not invented names.
Low-confidence or unsupported answers remain undetermined. PO values are never overwritten.
Conflicts and missing required dates/asset details produce an accounting review reason;
undetermined optional classifications are displayed as advisory recommendations.
Results, confidence, evidence, and source are saved per invoice line and in the coding step.
New processing uses these rules; old invoices are not automatically reprocessed.

## Exception ownership

Invoices requiring review retain a recommendation for each exception: Purchasing, Delivery team,
Budget owner, Finance, or Supplier clarification needed. Supplier clarification always includes an
internal accountable team. Jev chooses from closed routes/actions; the explanation combines the
actual failed-check evidence with the responsible team's remit. Low-confidence or unavailable
answers use labelled rule fallbacks. Security exceptions retain Finance ownership. Recommendations
are visible in the review queue and invoice details; they do not send messages, assign named people,
or override failed checks. Delivery rechecks reuse surviving recommendations without model calls.

## Massachusetts tax verification

Tax rules are available at `/tax-rules`, with the official DOR guide section text and anchors.
`massachusetts_tax_checks_enabled` defaults to true for Northwind's Massachusetts purchase workflow.
Exact whole-description rules currently cover selected office goods and pure legal/accounting services.
All other descriptions require Finance review; JEV can select a relevant section from the saved
DOR guide but cannot grant exemptions or invent rates. Source selection failure keeps review open.
Each invoice stores versioned results in `additional_info.tax_review` and displays them separately.
Tax exceptions route to Finance. Rechecks reuse citations without model calls.
The guide snapshot was retrieved October 5, 2026 (guide updated May 7, 2026); sources older than
90 days and invoices preceding this guide version require review. This is a scoped rule check,
not a determination of buyer eligibility, multi-state software allocation, or every possible exemption.
Massachusetts delivery/use and supporting exemption certificates must be confirmed by Finance.
Unknown tax, discounts, shipping, ambiguous bundles, and uncertain tax bases require review.
Zero supplier tax on potentially taxable goods triggers exemption/use-tax review.

## JEV tax treatment assessment

With Massachusetts checks enabled, JEV now assesses every invoice line against the supplied
DOR sections. It selects a bounded treatment and supporting section; both answers must
reach 85% confidence and the section must support the selected treatment. Conditional
exemptions, uncertain purchases, inconsistent citations and conflicts with an exact-description
rule require Finance review. Code alone calculates and compares tax amounts. Invoice results
store the selected treatment, confidence, invoice-line evidence, source, model and call metadata.
Rechecks reuse assessments only when the policy version and invoice-evidence fingerprint match;
changed evidence or an unavailable model require review. No live model calls occur on recheck.
Offline fixture checks explicitly use `rule_only=True` and are expected results, not JEV evaluations.

## Reviewer evidence and exception resolution

Invoice details now show matched invoice/PO lines, recorded receipts and related delivery
issues together, with failed checks and the stored DOR source passage. Ambiguous findings
remain invoice-wide rather than being assigned to guessed lines. Exception records have an
internal owner, optional assigned person, status and required update/resolution reason.
Updates are audited; Save and recheck reruns existing controls without calling models.
A cleared control, a still-open finding and a reviewer-accepted exception have distinct statuses.
The latest recheck shows previous/current findings; earlier decisions and audit events remain.
Tax and payment-security exceptions retain Finance ownership. Existing recheck behavior may
automatically approve when all controls clear; saving ownership/status alone cannot approve.

## PO tax and accounting period review

Create PO accepts prices before tax and an optional tax rate on each line. Totals show subtotal, tax and total including tax. Enter 0 for untaxed lines; a blank rate leaves tax and the total unknown. Tax is rounded to cents after grouping net amounts by rate. The existing API `total_amount` remains the net ordered value used by matching; `subtotal_amount`, `tax_amount` and `grand_total` expose the separate totals. PO rates record purchasing expectations and do not override Massachusetts invoice tax verification.

Settings includes a company-approved per-item asset threshold. Subscription allocation review always covers subscriptions spanning multiple months. Invoice review keeps invoice date, service dates, matched delivery dates and posting period separate. Potential prepayments/assets require Finance review, with an explanation and recorded treatment confirmation. Changed policy, amount or service dates invalidate an earlier treatment confirmation. Approval and export recheck these controls. Standard CSV carries a separate posting period; formats that cannot represent an adjusted period are blocked. The app does not create amortization, depreciation or other journal entries.

Demo company policies default to multi-month subscription review and a $1,000 per-item equipment review threshold. The existing configured approval limit defaults to $5,000. These are testing defaults, not universal company policies. Clearing the equipment threshold disables only its price-triggered check. Settings retains current values without recording change history; invoice decision and review records remain available.

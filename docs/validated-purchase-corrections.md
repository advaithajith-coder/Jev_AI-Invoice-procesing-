# Validated correction reuse

This feature reuses explicitly validated reviewer knowledge. It does not retrain JEV and does not treat invoice approval as a correction.

## Use it

1. Open an invoice and expand **Save a recurring purchase correction**.
2. Choose its line and one corrected field: purchase category, GL account, or Massachusetts tax treatment.
3. Record the supporting evidence, name and validity dates (maximum one year). Tax corrections require a compatible official source and confirmation of the actual purchase location.
4. Save the pending correction. On **Saved corrections**, examine the exact scope and evidence, including the official passage for tax.
5. Enter the validator’s name and reason, then choose **Validate for future matches**.

Future processing reuses the correction only for the same matched vendor, buyer, currency and exact description, ignoring case and whitespace only. Dates, shipment numbers and other text within descriptions are not stripped. Both the invoice date and the current date must fall within the validity period. Missing scope fields disable reuse. This intentionally does not generalize October rent to November rent or one shipment to another.

Approved PO GL/category values take priority. GL accounts must still be active eligible purchase accounts. Tax corrections are restricted to supported Massachusetts treatments, require a compatible source, stop applying on a source-version change and cannot override a conflicting exact description rule. Tax arithmetic, source freshness, jurisdiction warnings and all other invoice checks still run. A correction never grants invoice approval.

## Inspect or stop reuse

The invoice shows validated-correction provenance, validator and expiry, and an audit event records each applied field. **Withdraw correction** disables future reuse; previous invoices and saved evaluation outputs are preserved. Rechecking tax does not reuse a cached withdrawn correction. Competing active corrections are rejected during validation, and ambiguous overlapping matches are not applied.

The page displays the latest 200 corrections. Pending, active and withdrawn records remain available. Expired records do not apply, even if their stored status is active.

## Boundaries

The first version covers purchase category, GL account and supported Massachusetts tax treatment. It does not learn extraction, tax exemptions, department/project assignments, asset policy, or exception routing. Corrections are not included automatically in the frozen isolated evaluation pilot; its existing baseline remains unchanged.

Like the existing reviewer workflow, this local demo records typed names; it does not authenticate validators or enforce Finance permissions. Before production use, add role checks and separation of duties. The backend checks scope, account validity and tax source compatibility; a named reviewer must verify that the evidence is substantively correct.

Migration: `a91cc2d847e1`. Database backup before the local upgrade: `tmp/before-purchase-corrections.sqlite3`.

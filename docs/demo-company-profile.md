# Demo focus: Massachusetts light manufacturing

Parser.AI's selected demo audience is a small Massachusetts manufacturer assembling electronic products. This is an invoice-processing demonstration, not an inventory or production-planning system.

## Company and purchases

The demo company buys components, assembles products and purchases supporting business services. Keep the existing Northwind Analytics Inc identity until the company name and repeated invoice details are deliberately migrated together; the present name, vendors and 100-invoice dataset still belong to the earlier technology-company baseline.

Representative purchases for a new manufacturing dataset:

- Electronic components: circuit boards, connectors, cables, sensors and power supplies.
- Assembly consumables: solder, fasteners, adhesives and protective packaging.
- Equipment: testing instruments, assembly tools and workstations.
- Operational services: equipment maintenance, freight, premises rent, utilities and software.

Goods POs require receiving. Service POs use two-way matching and do not appear in Delivery Management. The existing application records receipts and delivery issues; it does not establish perpetual inventory valuation, bills of materials or production consumption.

## Main demonstration

Create a PO for several components, record a partial delivery and upload the supplier invoice. Show extraction beside the document, match PO lines to accepted receipts, and distinguish a quantity issue from a price issue. Identify the exception owner and record the review outcome.

Additional examples:

1. Complete delivery and invoice quantities/prices agree.
2. Invoice bills more than accepted delivery; Delivery team investigates.
3. Invoice price differs from the PO; Purchasing investigates.
4. Damaged goods require a delivery issue and supplier credit follow-up.
5. Service invoice skips receiving but still matches the PO.
6. A recurring purchase uses an explicitly validated correction without bypassing other controls.

## Accounting and tax boundaries

Purchases intended for inventory, production materials, operating expense and equipment may require different accounts. Finance must approve the chart of accounts and policies; do not treat all manufacturing purchases as expenses or claim that this demo supplies inventory accounting.

Do not assume that all goods have the same tax treatment or that every manufacturer qualifies for an exemption. A future manufacturing tax dataset must use verified official guidance, transaction/use evidence and approved company rules. Existing Massachusetts rules and rates have not been revalidated for this industry choice.

## Migration sequence

1. Establish manufacturing vendors, purchase descriptions and Finance-approved account mappings.
2. Prepare a separate manufacturing invoice/PO/delivery fixture set and independently checked expectations.
3. Validate tax source coverage and conditional exemption handling.
4. Test the complete demonstration journeys, then isolated model stages on the new dataset.

Preserve the existing 100 outputs and their frozen expectations as historical results. They measure the earlier dataset and must not be relabeled as manufacturing validation. This profile records the selected direction; it does not modify live vendors, POs, policies or invoices.

"""Fictional master data for "Northwind Analytics Inc", a US company that pays in USD.

Scenarios built in for later milestones:
- PO-1001 fully received          → clean invoice can auto-pay
- PO-1002, PO-1008 part received  → invoice for the full amount should be held or short-paid
- PO-1004 hosting, PO-1005 campaign and PO-1007 IT retainer are services: matched to the
  order only (two-way), nothing is received
- PO-1009 cancelled               → any invoice against it is suspicious
- Old Mill Stationers on hold, Vortex Trading blocked → vendor-status checks
- Acme Supplies has known bank details → a changed account on an invoice is a fraud signal
- Meridian Legal, Stratus Telecom and Harbor Point Properties bill without POs (legal fees,
  phone lines, rent): their invoices need no PO
- Acme (2/10 net 30), Halcyon (1/15 net 45) and Northline (2/10 net 30) offer a discount
  for paying early

Bank account numbers here are fake and only exist so seeding can compute fingerprints.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from jev_invoice.domain.enums import GLAccountType, PurchaseOrderStatus, VendorStatus


@dataclass(frozen=True)
class GLAccountSeed:
    code: str
    name: str
    description: str
    account_type: GLAccountType = GLAccountType.EXPENSE


@dataclass(frozen=True)
class VendorSeed:
    key: str
    name: str
    currency: str
    default_gl: str
    email_domain: str | None = None
    tax_id: str | None = None
    payment_terms_days: int = 30
    bank_account: str | None = None
    status: VendorStatus = VendorStatus.ACTIVE
    po_required: bool = True
    # Early-payment discount: (percent, days), e.g. (2, 10) for "2/10 net 30".
    early_pay: tuple[Decimal, int] | None = None

    @property
    def early_pay_discount_pct(self) -> Decimal | None:
        return self.early_pay[0] if self.early_pay else None

    @property
    def early_pay_days(self) -> int | None:
        return self.early_pay[1] if self.early_pay else None


@dataclass(frozen=True)
class POLineSeed:
    description: str
    quantity: Decimal
    unit_price: Decimal
    gl: str

    @property
    def amount(self) -> Decimal:
        return (self.quantity * self.unit_price).quantize(Decimal("0.01"))


@dataclass(frozen=True)
class ReceiptSeed:
    receipt_number: str
    received_date: date
    # line_no (1-based) -> quantity received
    quantities: dict[int, Decimal]


@dataclass(frozen=True)
class PurchaseOrderSeed:
    po_number: str
    vendor_key: str
    issued_date: date
    status: PurchaseOrderStatus
    lines: tuple[POLineSeed, ...]
    receipts: tuple[ReceiptSeed, ...] = field(default_factory=tuple)
    # False for services: invoices are matched to the order only (two-way).
    needs_receiving: bool = True

    @property
    def total(self) -> Decimal:
        return sum((line.amount for line in self.lines), Decimal("0"))


D = Decimal

GL_ACCOUNTS: tuple[GLAccountSeed, ...] = (
    GLAccountSeed("1500", "Computer Equipment", "Laptops, desktops, servers and other IT hardware with a useful life over one year (capitalized).", GLAccountType.ASSET),
    GLAccountSeed("5000", "Cloud Hosting", "Cloud infrastructure, compute, storage and data transfer used to deliver client services.", GLAccountType.COST_OF_SALES),
    GLAccountSeed("6000", "Office Supplies", "Paper, toner, stationery and small consumables for the office."),
    GLAccountSeed("6010", "Software Subscriptions", "SaaS licences and software subscriptions for internal use."),
    GLAccountSeed("6020", "IT Services and Support", "Managed IT support, helpdesk retainers and IT consultancy."),
    GLAccountSeed("6100", "Legal Fees", "Attorney and legal advisory fees, contract review."),
    GLAccountSeed("6110", "Accountancy Fees", "External accounting, audit and tax advisory fees."),
    GLAccountSeed("6200", "Marketing and Advertising", "Campaigns, agency fees, paid media and promotional material."),
    GLAccountSeed("6300", "Travel and Accommodation", "Business travel, hotels and subsistence."),
    GLAccountSeed("6400", "Rent and Facilities", "Office rent, service charges and facilities management."),
    GLAccountSeed("6500", "Telecommunications", "Phone lines, mobile contracts and internet connectivity."),
    GLAccountSeed("6600", "Training", "Courses, certifications and staff training."),
    GLAccountSeed("6700", "Repairs and Maintenance", "Repairs to equipment and premises; small parts and accessories under the capitalization threshold."),
    GLAccountSeed("6800", "Courier and Postage", "Couriers, freight and postage."),
    GLAccountSeed("9999", "Suspense - needs coding", "Temporary account for approved costs whose expense account is not known yet. Recode before closing the month."),
)  # fmt: skip

VENDORS: tuple[VendorSeed, ...] = (
    VendorSeed("acme", "Acme Supplies Inc", "USD", "6000", "acmesupplies.com", "84-1234567", 30, "Routing 021000021 Account 4417926819", early_pay=(D("2"), 10)),
    VendorSeed("brightline", "Brightline Cloud Services Inc", "USD", "5000", "brightlinecloud.com", "84-2219034", 30, "Routing 021000021 Account 4417 2209"),
    VendorSeed("halcyon", "Halcyon Computing LLC", "USD", "1500", "halcyoncomputing.com", "47-9876543", 45, "Routing 121000358 Account 5698765432", early_pay=(D("1"), 15)),
    VendorSeed("meridian", "Meridian Legal LLP", "USD", "6100", "meridianlegal.com", "13-5551239", 14, "Routing 026009593 Account 1555555555", po_required=False),
    VendorSeed("pixelforge", "Pixelforge Marketing LLC", "USD", "6200", "pixelforge.com", "27-4448882", 30, "Routing 011000138 Account 1530093459"),
    VendorSeed("northline", "Northline Freight Inc", "USD", "6800", "northlinefreight.com", "91-8119079", 30, "Routing 125000024 Account 4405320130", early_pay=(D("2"), 10)),
    VendorSeed("clearpath", "Clearpath IT Support LLC", "USD", "6020", "clearpathit.com", "36-2223334", 30, "Routing 322271627 Account 7612345678"),
    VendorSeed("stratus", "Stratus Telecom Inc", "USD", "6500", "stratustelecom.com", "52-7776665", 30, "Routing 071000013 Account 1234567890", po_required=False),
    VendorSeed("harborpoint", "Harbor Point Properties LLC", "USD", "6400", "harborpointproperties.com", "26-5550142", 5, "Routing 111000025 Account 8800145521", po_required=False),
    VendorSeed("oldmill", "Old Mill Stationers", "USD", "6000", "oldmillstationers.com", None, 30, None, VendorStatus.ON_HOLD),
    VendorSeed("vortex", "Vortex Trading LLC", "USD", "6700", None, None, 7, None, VendorStatus.BLOCKED),
)  # fmt: skip

PURCHASE_ORDERS: tuple[PurchaseOrderSeed, ...] = (
    PurchaseOrderSeed(
        "PO-1001", "halcyon", date(2026, 8, 3), PurchaseOrderStatus.RECEIVED,
        (
            POLineSeed("Business laptop, 14-inch, 32GB RAM, 1TB SSD", D("10"), D("1150.00"), "1500"),
            POLineSeed("USB-C docking station", D("10"), D("145.00"), "6700"),
        ),
        (ReceiptSeed("GRN-5001", date(2026, 8, 18), {1: D("10"), 2: D("10")}),),
    ),
    PurchaseOrderSeed(
        "PO-1002", "halcyon", date(2026, 8, 20), PurchaseOrderStatus.PARTIALLY_RECEIVED,
        (POLineSeed("Rack server, 2U, dual CPU, 256GB RAM", D("2"), D("4800.00"), "1500"),),
        (ReceiptSeed("GRN-5002", date(2026, 9, 9), {1: D("1")}),),
    ),
    PurchaseOrderSeed(
        "PO-1003", "acme", date(2026, 9, 1), PurchaseOrderStatus.RECEIVED,
        (
            POLineSeed("Letter copier paper, 20 lb, case of 10 reams", D("50"), D("24.99"), "6000"),
            POLineSeed("Toner cartridge, black, high yield", D("12"), D("89.50"), "6000"),
        ),
        (ReceiptSeed("GRN-5003", date(2026, 9, 4), {1: D("50"), 2: D("12")}),),
    ),
    PurchaseOrderSeed(
        "PO-1004", "brightline", date(2026, 9, 1), PurchaseOrderStatus.OPEN,
        (POLineSeed("Managed cloud hosting, September 2026", D("1"), D("3200.00"), "5000"),),
        needs_receiving=False,
    ),
    PurchaseOrderSeed(
        "PO-1005", "pixelforge", date(2026, 9, 10), PurchaseOrderStatus.OPEN,
        (POLineSeed("Q4 product launch campaign: strategy, creative and paid social", D("1"), D("7500.00"), "6200"),),
        needs_receiving=False,
    ),
    PurchaseOrderSeed(
        "PO-1006", "northline", date(2026, 9, 2), PurchaseOrderStatus.RECEIVED,
        (POLineSeed("Express courier shipment, Seattle to Chicago, up to 10 lb", D("40"), D("18.50"), "6800"),),
        (ReceiptSeed("GRN-5006", date(2026, 9, 28), {1: D("40")}),),
    ),
    PurchaseOrderSeed(
        "PO-1007", "clearpath", date(2026, 9, 15), PurchaseOrderStatus.OPEN,
        (POLineSeed("IT support retainer, Q4 2026 (October to December)", D("1"), D("2400.00"), "6020"),),
        needs_receiving=False,
    ),
    PurchaseOrderSeed(
        "PO-1008", "meridian", date(2026, 9, 5), PurchaseOrderStatus.PARTIALLY_RECEIVED,
        (POLineSeed("Commercial contract review, hourly", D("12"), D("275.00"), "6100"),),
        (ReceiptSeed("GRN-5008", date(2026, 9, 25), {1: D("8")}),),
    ),
    PurchaseOrderSeed(
        "PO-1009", "acme", date(2026, 7, 14), PurchaseOrderStatus.CANCELLED,
        (POLineSeed("Ergonomic office chair", D("6"), D("310.00"), "6700"),),
    ),
)  # fmt: skip

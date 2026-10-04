"""Idempotent seeding of master data. Run with `uv run poe seed`.

IDs are deterministic (uuid5 of a natural key), so re-running inserts nothing new and
never duplicates rows. Existing rows are left untouched.
"""

import asyncio
import uuid
from collections.abc import Sequence
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from jev_invoice.config import Settings, get_settings
from jev_invoice.core.logging import configure_logging
from jev_invoice.db.base import Base
from jev_invoice.db.models import (
    GLAccount,
    GoodsReceipt,
    GoodsReceiptLine,
    PurchaseOrder,
    PurchaseOrderLine,
    Vendor,
)
from jev_invoice.db.seed_data import GL_ACCOUNTS, PURCHASE_ORDERS, VENDORS
from jev_invoice.db.session import create_engine, create_session_factory
from jev_invoice.domain.vendors import (
    bank_account_fingerprint,
    bank_account_last4,
    normalize_vendor_name,
)

_NAMESPACE = uuid.UUID("6f1c2b8e-6a8e-4d0a-9a51-3b5c1e0c7a10")
log = structlog.get_logger(__name__)


def seed_id(kind: str, key: str) -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, f"{kind}:{key}")


async def _insert(session: AsyncSession, model: type[Base], rows: Sequence[dict[str, Any]]) -> None:
    """INSERT ... ON CONFLICT DO NOTHING, for PostgreSQL or SQLite."""
    if not rows:
        return
    dialect = session.get_bind().dialect.name
    insert = postgresql.insert if dialect == "postgresql" else sqlite.insert
    await session.execute(insert(model).values(list(rows)).on_conflict_do_nothing())


async def seed(session: AsyncSession, settings: Settings) -> dict[str, int]:
    """Insert master data. Returns row counts per table after seeding."""
    key = settings.bank_fingerprint_key.get_secret_value().encode()

    await _insert(
        session,
        GLAccount,
        [
            {
                "code": a.code,
                "name": a.name,
                "description": a.description,
                "account_type": a.account_type,
            }
            for a in GL_ACCOUNTS
        ],
    )
    await _insert(
        session,
        Vendor,
        [
            {
                "id": seed_id("vendor", v.key),
                "name": v.name,
                "normalized_name": normalize_vendor_name(v.name),
                "tax_id": v.tax_id,
                "email_domain": v.email_domain,
                "default_currency": v.currency,
                "payment_terms_days": v.payment_terms_days,
                "early_pay_discount_pct": v.early_pay_discount_pct,
                "early_pay_days": v.early_pay_days,
                "po_required": v.po_required,
                "bank_account_last4": bank_account_last4(v.bank_account)
                if v.bank_account
                else None,
                "bank_account_fingerprint": (
                    bank_account_fingerprint(v.bank_account, key) if v.bank_account else None
                ),
                "default_gl_account_code": v.default_gl,
                "status": v.status,
            }
            for v in VENDORS
        ],
    )

    currency_by_vendor = {v.key: v.currency for v in VENDORS}
    po_rows, po_line_rows, receipt_rows, receipt_line_rows = [], [], [], []
    for po in PURCHASE_ORDERS:
        po_id = seed_id("po", po.po_number)
        po_rows.append(
            {
                "id": po_id,
                "po_number": po.po_number,
                "vendor_id": seed_id("vendor", po.vendor_key),
                "currency": currency_by_vendor[po.vendor_key],
                "status": po.status,
                "issued_date": po.issued_date,
                "total_amount": po.total,
            }
        )
        for line_no, line in enumerate(po.lines, start=1):
            po_line_rows.append(
                {
                    "id": seed_id("po_line", f"{po.po_number}:{line_no}"),
                    "purchase_order_id": po_id,
                    "line_no": line_no,
                    "description": line.description,
                    "quantity": line.quantity,
                    "unit_price": line.unit_price,
                    "gl_account_code": line.gl,
                }
            )
        for receipt in po.receipts:
            receipt_id = seed_id("receipt", receipt.receipt_number)
            receipt_rows.append(
                {
                    "id": receipt_id,
                    "receipt_number": receipt.receipt_number,
                    "purchase_order_id": po_id,
                    "received_date": receipt.received_date,
                }
            )
            for line_no, quantity in receipt.quantities.items():
                receipt_line_rows.append(
                    {
                        "id": seed_id("receipt_line", f"{receipt.receipt_number}:{line_no}"),
                        "goods_receipt_id": receipt_id,
                        "purchase_order_line_id": seed_id("po_line", f"{po.po_number}:{line_no}"),
                        "quantity_received": quantity,
                    }
                )

    await _insert(session, PurchaseOrder, po_rows)
    await _insert(session, PurchaseOrderLine, po_line_rows)
    await _insert(session, GoodsReceipt, receipt_rows)
    await _insert(session, GoodsReceiptLine, receipt_line_rows)
    await session.flush()

    counts = {}
    for model in (
        GLAccount,
        Vendor,
        PurchaseOrder,
        PurchaseOrderLine,
        GoodsReceipt,
        GoodsReceiptLine,
    ):
        counts[model.__tablename__] = (
            await session.scalar(select(func.count()).select_from(model)) or 0
        )
    return counts


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json)
    engine = create_engine(settings)
    try:
        async with create_session_factory(engine)() as session, session.begin():
            counts = await seed(session, settings)
        log.info("seed_complete", **counts)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

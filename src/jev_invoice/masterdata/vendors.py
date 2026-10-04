"""Vendors: create, edit, block, and change bank details.

Changing a vendor's bank account is the classic way invoice fraud succeeds (a fraudster
asks AP to "update our details"). So it is a separate operation that needs the name of
the person making the change and a note saying how it was verified, and the old and new
account endings go into the audit trail. The full account number is never stored.
"""

import uuid
from decimal import Decimal
from typing import Annotated, Self

from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jev_invoice.core import audit
from jev_invoice.core.errors import AppError, ConflictError, NotFoundError
from jev_invoice.db.models import GLAccount, PurchaseOrder, Vendor
from jev_invoice.domain.enums import VendorStatus
from jev_invoice.domain.vendors import (
    bank_account_fingerprint,
    bank_account_last4,
    normalize_bank_account,
    normalize_vendor_name,
)

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Person = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Optional = Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=255)]


class InvalidInputError(AppError):
    status_code = 422
    code = "invalid_input"


def _currency(value: str) -> str:
    code = value.strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise ValueError("currency must be a 3-letter ISO code such as USD")
    return code


class VendorFields(BaseModel):
    name: Name
    default_currency: str = "USD"
    payment_terms_days: int = Field(default=30, ge=0, le=365)
    # Early-payment discount: e.g. 2 (%) if paid within 10 days. Both or neither.
    early_pay_discount_pct: Decimal | None = Field(default=None, gt=0, lt=100)
    early_pay_days: int | None = Field(default=None, ge=1, le=365)
    # False for vendors billed without a PO (rent, utilities, legal fees, subscriptions).
    po_required: bool = True
    default_gl_account_code: str
    email_domain: Optional = None
    tax_id: Optional = None
    status: VendorStatus = VendorStatus.ACTIVE

    _currency = field_validator("default_currency")(_currency)

    @model_validator(mode="after")
    def _discount_needs_both(self) -> Self:
        if (self.early_pay_discount_pct is None) != (self.early_pay_days is None):
            raise ValueError(
                "an early-payment discount needs both a percentage and a number of days"
            )
        if self.early_pay_days is not None and self.early_pay_days >= self.payment_terms_days:
            raise ValueError("the discount period must be shorter than the payment terms")
        return self

    @field_validator("email_domain", "tax_id")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        return value or None


class VendorCreate(VendorFields):
    bank_account: Optional = None


class BankAccountChange(BaseModel):
    bank_account: Annotated[str, StringConstraints(strip_whitespace=True, min_length=4)]
    changed_by: Person
    note: Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=2000)]


class VendorService:
    def __init__(self, session: AsyncSession, *, fingerprint_key: bytes) -> None:
        self._session = session
        self._key = fingerprint_key

    async def list_all(self) -> list[Vendor]:
        return list(await self._session.scalars(select(Vendor).order_by(Vendor.name)))

    async def get(self, vendor_id: uuid.UUID) -> Vendor:
        vendor = await self._session.get(Vendor, vendor_id)
        if vendor is None:
            raise NotFoundError("Vendor not found", details={"vendor_id": str(vendor_id)})
        return vendor

    async def gl_accounts(self) -> list[GLAccount]:
        return list(
            await self._session.scalars(
                select(GLAccount).where(GLAccount.is_active).order_by(GLAccount.code)
            )
        )

    async def create(self, data: VendorCreate, *, actor: str, commit: bool = True) -> Vendor:
        await self._check_gl(data.default_gl_account_code)
        normalized = normalize_vendor_name(data.name)
        await self._check_name_free(normalized)
        vendor = Vendor(
            id=uuid.uuid4(),
            name=data.name,
            normalized_name=normalized,
            default_currency=data.default_currency,
            payment_terms_days=data.payment_terms_days,
            early_pay_discount_pct=data.early_pay_discount_pct,
            early_pay_days=data.early_pay_days,
            po_required=data.po_required,
            default_gl_account_code=data.default_gl_account_code,
            email_domain=data.email_domain,
            tax_id=data.tax_id,
            status=data.status,
        )
        if data.bank_account:
            self._set_bank(vendor, data.bank_account)
        self._session.add(vendor)
        audit.record(
            self._session,
            action="vendor.created",
            entity_type="vendor",
            entity_id=vendor.id,
            actor=actor,
            data={"name": vendor.name, "bank_account_last4": vendor.bank_account_last4},
        )
        if commit:
            await self._session.commit()
        return vendor

    async def update(self, vendor_id: uuid.UUID, data: VendorFields, *, actor: str) -> Vendor:
        vendor = await self.get(vendor_id)
        await self._check_gl(data.default_gl_account_code)
        normalized = normalize_vendor_name(data.name)
        if normalized != vendor.normalized_name:
            await self._check_name_free(normalized)
        changes = {
            field: [str(getattr(vendor, field)), str(value)]
            for field, value in data.model_dump().items()
            if getattr(vendor, field) != value
        }
        for field, value in data.model_dump().items():
            setattr(vendor, field, value)
        vendor.normalized_name = normalized
        if changes:
            audit.record(
                self._session,
                action="vendor.updated",
                entity_type="vendor",
                entity_id=vendor.id,
                actor=actor,
                data={"changes": changes},
            )
        await self._session.commit()
        return vendor

    async def change_bank_account(self, vendor_id: uuid.UUID, change: BankAccountChange) -> Vendor:
        vendor = await self.get(vendor_id)
        old_last4 = vendor.bank_account_last4
        self._set_bank(vendor, change.bank_account)
        audit.record(
            self._session,
            action="vendor.bank_account_changed",
            entity_type="vendor",
            entity_id=vendor.id,
            actor=change.changed_by,
            data={
                "old_last4": old_last4,
                "new_last4": vendor.bank_account_last4,
                "note": change.note,
            },
        )
        await self._session.commit()
        return vendor

    async def open_po_counts(self) -> dict[uuid.UUID, int]:
        rows = await self._session.execute(
            select(PurchaseOrder.vendor_id, func.count())
            .where(PurchaseOrder.status.in_(("open", "partially_received")))
            .group_by(PurchaseOrder.vendor_id)
        )
        return {row[0]: row[1] for row in rows}

    # ── helpers ───────────────────────────────────────────────────────────────

    def _set_bank(self, vendor: Vendor, account: str) -> None:
        if len(normalize_bank_account(account)) < 6:
            raise InvalidInputError(
                "That does not look like a bank account number (routing and account, or IBAN)."
            )
        vendor.bank_account_last4 = bank_account_last4(account)
        vendor.bank_account_fingerprint = bank_account_fingerprint(account, self._key)

    async def _check_gl(self, code: str) -> None:
        if await self._session.get(GLAccount, code) is None:
            raise InvalidInputError(f"GL account {code} does not exist.")

    async def _check_name_free(self, normalized: str) -> None:
        clash = await self._session.scalar(
            select(Vendor.name).where(Vendor.normalized_name == normalized)
        )
        if clash is not None:
            raise ConflictError(f"A vendor with this name already exists: {clash}.")

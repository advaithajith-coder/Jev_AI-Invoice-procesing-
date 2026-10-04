"""The chart of accounts: the accounts invoice lines are coded to.

Each account's description is what Jev reads when it codes a line, so a good
description ("Paper, toner, stationery and small consumables for the office") makes
good coding. Accounts are never deleted (past invoices refer to them); an account that
is no longer used is deactivated, and Jev stops offering it.
"""

import re
from dataclasses import dataclass
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from jev_invoice.core import audit
from jev_invoice.core.errors import ConflictError, NotFoundError
from jev_invoice.db.models import GLAccount, Invoice, InvoiceLine
from jev_invoice.domain.enums import GLAccountType, InvoiceStatus

_CODE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,19}$")
SPENT_STATUSES = (InvoiceStatus.APPROVED, InvoiceStatus.EXPORTED)


class AccountFields(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=10, max_length=500)
    account_type: GLAccountType = GLAccountType.EXPENSE
    is_active: bool = True

    @field_validator("name", "description", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class AccountCreate(AccountFields):
    code: str

    @field_validator("code", mode="before")
    @classmethod
    def _code(cls, value: object) -> str:
        code = str(value or "").strip().upper()
        if not _CODE.match(code):
            raise ValueError("use 1-20 letters, digits, dots or dashes, e.g. 6250")
        return code


@dataclass(frozen=True)
class AccountUsage:
    account: GLAccount
    lines: int
    amount: Decimal


class AccountService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_with_usage(self) -> list[AccountUsage]:
        """Every account with how many approved invoice lines, and how much, were coded to it."""
        rows = await self._session.execute(
            select(
                InvoiceLine.gl_account_code,
                func.count(InvoiceLine.id),
                func.coalesce(func.sum(InvoiceLine.amount), 0),
            )
            .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
            .where(Invoice.status.in_(SPENT_STATUSES), InvoiceLine.gl_account_code.is_not(None))
            .group_by(InvoiceLine.gl_account_code)
        )
        usage = {code: (count, Decimal(str(amount))) for code, count, amount in rows}
        accounts = await self._session.scalars(select(GLAccount).order_by(GLAccount.code))
        return [
            AccountUsage(account, *usage.get(account.code, (0, Decimal(0)))) for account in accounts
        ]

    async def get(self, code: str) -> GLAccount:
        account = await self._session.get(GLAccount, code)
        if account is None:
            raise NotFoundError("Account not found", details={"code": code})
        return account

    async def recent_lines(self, code: str, limit: int = 20) -> list[InvoiceLine]:
        return list(
            await self._session.scalars(
                select(InvoiceLine)
                .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
                .where(InvoiceLine.gl_account_code == code)
                .options(selectinload(InvoiceLine.invoice))
                .order_by(Invoice.received_at.desc())
                .limit(limit)
            )
        )

    async def create(self, data: AccountCreate, *, actor: str) -> GLAccount:
        if await self._session.get(GLAccount, data.code) is not None:
            raise ConflictError(f"Account {data.code} already exists.")
        account = GLAccount(
            code=data.code,
            name=data.name,
            description=data.description,
            account_type=data.account_type,
            is_active=data.is_active,
        )
        self._session.add(account)
        audit.record(
            self._session,
            action="gl_account.created",
            entity_type="gl_account",
            entity_id=data.code,
            actor=actor,
            data={"name": data.name},
        )
        await self._session.commit()
        return account

    async def update(self, code: str, data: AccountFields, *, actor: str) -> GLAccount:
        account = await self.get(code)
        changes = {
            name: [str(getattr(account, name)), str(value)]
            for name, value in data.model_dump().items()
            if getattr(account, name) != value
        }
        for name, value in data.model_dump().items():
            setattr(account, name, value)
        if changes:
            audit.record(
                self._session,
                action="gl_account.updated",
                entity_type="gl_account",
                entity_id=code,
                actor=actor,
                data={"changes": changes},
            )
        await self._session.commit()
        return account

"""Accounting dates and treatment proposals; never creates journal entries."""

import hashlib
import json
import re
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jev_invoice.core.errors import AppError
from jev_invoice.db.models import AppSetting, GLAccount, GoodsReceipt, GoodsReceiptLine, Invoice
from jev_invoice.domain.enums import ReasonCode
from jev_invoice.domain.invoice import CheckFinding


class AccountingInputError(AppError):
    status_code = 422
    code = "accounting_input"


def period(value: str) -> str:
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", value) or value.startswith("0000"):
        raise ValueError("Enter a period in YYYY-MM format.")
    return value


class AccountingPolicy(BaseModel):
    asset_threshold: Decimal | None = Field(
        default=Decimal("1000.00"), gt=0, allow_inf_nan=False, decimal_places=2
    )
    asset_currency: str = "USD"
    prepayment_rule: Literal["multi_period"] = "multi_period"
    policy_reference: str = Field(
        default=(
            "Demo policies: review subscriptions across multiple months; "
            "review equipment at $1,000 per item for capitalization. "
            "Finance confirms treatment and eligibility."
        ),
        max_length=200,
    )


async def load_policy(session: AsyncSession) -> AccountingPolicy:
    rows = await session.scalars(select(AppSetting).where(AppSetting.key.like("accounting_%")))
    return AccountingPolicy.model_validate(
        {
            **{
                row.key.removeprefix("accounting_"): (
                    None if row.key == "accounting_asset_threshold" and not row.value else row.value
                )
                for row in rows
            },
            "prepayment_rule": "multi_period",
        }
    )


async def save_policy(session: AsyncSession, policy: AccountingPolicy, actor: str) -> None:
    for name, value in policy.model_dump(mode="json").items():
        key = "accounting_" + name
        stored = await session.get(AppSetting, key)
        saved_value = "" if value is None else str(value)
        if stored:
            stored.value, stored.updated_by = saved_value, actor
        else:
            session.add(AppSetting(key=key, value=saved_value, updated_by=actor))
    await session.commit()


def posting_period(invoice: Invoice) -> str | None:
    chosen = (invoice.additional_info or {}).get("accounting", {}).get("posting_period")
    return chosen or (invoice.invoice_date.strftime("%Y-%m") if invoice.invoice_date else None)


async def check_accounting(session: AsyncSession, invoice: Invoice) -> list[CheckFinding]:
    policy = await load_policy(session)
    findings = []
    state = dict((invoice.additional_info or {}).get("accounting", {}))
    chosen = posting_period(invoice)
    state.update(
        posting_period=chosen,
        posting_period_source="person"
        if state.get("posting_reason")
        else "proposed from invoice date",
        invoice_date=str(invoice.invoice_date) if invoice.invoice_date else None,
        policy=policy.model_dump(mode="json"),
    )
    matched_ids = [
        line.purchase_order_line_id for line in invoice.lines if line.purchase_order_line_id
    ]
    delivery_dates = (
        list(
            await session.scalars(
                select(GoodsReceipt.received_date)
                .join(GoodsReceiptLine)
                .where(GoodsReceiptLine.purchase_order_line_id.in_(matched_ids))
            )
        )
        if matched_ids
        else []
    )
    state["delivery_dates"] = sorted({d.isoformat() for d in delivery_dates})
    accounts = {a.code: a.account_type for a in await session.scalars(select(GLAccount))}
    rows = []
    for line in invoice.lines:
        fields = (line.finance_classification or {}).get("fields", {})

        def value(key: str, current: dict[str, Any] = fields) -> Any:
            return current.get(key, {}).get("value")

        start, end = value("service_start"), value("service_end")
        months = None
        if start and end:
            first, last = date.fromisoformat(start), date.fromisoformat(end)
            months = (last.year - first.year) * 12 + last.month - first.month + 1
        category = value("purchase_category")
        suggestion = value("accounting_treatment")
        reasons = []
        annual = bool(months and months >= 12) or bool(
            re.search(r"\bannual(?:ly)?\b", line.description, re.I)
        )
        if category == "subscription" and (annual or (months is not None and months > 1)):
            suggestion = "prepayment_review"
            reasons.append(
                "Subscription spans accounting periods; review prepaid expense allocation."
            )
            if not start or not end:
                reasons.append("Service start and end dates are needed for an allocation proposal.")
        asset_candidate = (
            accounts.get(line.gl_account_code or "") == "asset"
            or value("asset_details_required") is True
        )
        unit = (
            line.unit_price
            if line.unit_price is not None
            else (
                line.amount / line.quantity if line.amount is not None and line.quantity else None
            )
        )
        if asset_candidate and policy.asset_threshold is not None:
            cost = (
                unit
                if invoice.currency == policy.asset_currency
                else (unit * invoice.fx_rate if unit is not None and invoice.fx_rate else None)
            )
            if cost is None or cost >= policy.asset_threshold:
                suggestion = "asset_review"
                reasons.append(
                    "Potential asset: review against the per-item capitalization threshold; "
                    "price alone does not establish useful life or eligibility."
                )
        if suggestion in ("prepayment_review", "asset_review") and not reasons:
            reasons.append(
                "JEV or the approved PO proposes treatment requiring Finance confirmation."
            )
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "policy": policy.model_dump(mode="json"),
                    "line": line.description,
                    "amount": str(line.amount),
                    "quantity": str(line.quantity),
                    "gl_account": line.gl_account_code,
                    "asset_details": value("asset_details"),
                    "currency": invoice.currency,
                    "fx_rate": str(invoice.fx_rate),
                    "unit_price": str(line.unit_price),
                    "start": start,
                    "end": end,
                    "suggestion": suggestion,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        resolution = state.get("resolutions", {}).get(str(line.line_no), {})
        resolved = bool(resolution.get("reason") and resolution.get("fingerprint") == fingerprint)
        if reasons and not resolved:
            findings.append(
                CheckFinding(
                    code=ReasonCode.ACCOUNTING_TREATMENT_REVIEW,
                    message=f"Line {line.line_no}: {' '.join(reasons)}",
                )
            )
        rows.append(
            {
                "line_no": line.line_no,
                "description": line.description,
                "service_start": start,
                "service_end": end,
                "months": months,
                "suggestion": suggestion or "No treatment proposed",
                "reasons": reasons,
                "fingerprint": fingerprint,
                "resolution": resolution if resolved else None,
                "status": "Finance confirmed"
                if resolved
                else ("Needs Finance review" if reasons else "No treatment exception"),
            }
        )
    state["lines"] = rows
    invoice.additional_info = {**(invoice.additional_info or {}), "accounting": state}
    return findings

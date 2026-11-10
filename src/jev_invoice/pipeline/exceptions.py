"""Exception state is workflow evidence, never an override of a failed control."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jev_invoice.core import audit
from jev_invoice.db.models import Decision, Invoice, InvoiceException

OWNERS = ("Purchasing", "Delivery team", "Budget owner", "Finance")
EDITABLE_STATUSES = ("open", "in_progress", "waiting_for_information", "ready_for_recheck")


async def sync_exceptions(
    session: AsyncSession, invoice: Invoice, decision: Decision, *, actor: str = "system"
) -> list[InvoiceException]:
    rows = list(
        await session.scalars(
            select(InvoiceException).where(InvoiceException.invoice_id == invoice.id)
        )
    )
    existing = {row.code: row for row in rows}
    routes = {r["code"]: r for r in decision.exception_routing or []}
    codes = set(decision.reason_codes)
    for code in codes:
        route = routes.get(code, {})
        evidence: dict[str, Any] = {
            "decision_id": str(decision.id),
            "findings": [f["message"] for f in decision.findings if f.get("code") == code],
            "next_action": route.get("next_action", "Finance must investigate this finding."),
        }
        row = existing.get(code)
        if row is None:
            row = InvoiceException(
                invoice_id=invoice.id,
                code=code,
                owner=route.get("owner", "Finance"),
                status="open",
                evidence=evidence,
            )
            session.add(row)
            rows.append(row)
            audit.record(
                session,
                action="exception.opened",
                entity_type="invoice",
                entity_id=invoice.id,
                actor=actor,
                data={"code": code, "owner": row.owner},
            )
        else:
            previous = row.evidence
            row.evidence = {
                **evidence,
                "previous_findings": previous.get("findings", []),
                "changed": previous.get("findings", []) != evidence["findings"],
            }
            if row.status in {"cleared", "accepted", "rejected", "ready_for_recheck"}:
                old_status = row.status
                row.status = "open"
                audit.record(
                    session,
                    action="exception.reopened",
                    entity_type="invoice",
                    entity_id=invoice.id,
                    actor=actor,
                    data={
                        "code": code,
                        "from": old_status,
                        "why": "Control still fails on the latest check.",
                    },
                )
    for row in rows:
        if row.code not in codes and row.status not in {"cleared", "accepted", "rejected"}:
            row.status = "cleared"
            row.evidence = {
                **row.evidence,
                "previous_findings": row.evidence.get("findings", []),
                "findings": [],
                "changed": True,
                "decision_id": str(decision.id),
            }
            row.resolution_reason = (
                row.resolution_reason
                or "The latest control check no longer reports this exception."
            )
            audit.record(
                session,
                action="exception.cleared",
                entity_type="invoice",
                entity_id=invoice.id,
                actor=actor,
                data={"code": row.code, "reason": row.resolution_reason},
            )
    return rows


async def record_review_disposition(
    session: AsyncSession, invoice: Invoice, *, approve: bool, actor: str, note: str | None
) -> None:
    rows = await session.scalars(
        select(InvoiceException).where(InvoiceException.invoice_id == invoice.id)
    )
    for row in rows:
        if row.status != "cleared":
            row.status = "accepted" if approve else "rejected"
            row.resolution_reason = (
                note or "Reviewer decided the invoice; the failed control was not cleared."
            )
            audit.record(
                session,
                action="exception.disposition",
                entity_type="invoice",
                entity_id=invoice.id,
                actor=actor,
                data={"code": row.code, "status": row.status, "reason": row.resolution_reason},
            )

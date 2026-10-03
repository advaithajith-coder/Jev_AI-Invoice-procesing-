"""Append-only audit trail. Every state change on an invoice is recorded here."""

from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from jev_invoice.db.models import AuditEvent

SYSTEM_ACTOR = "system"


def record(
    session: AsyncSession,
    *,
    action: str,
    entity_type: str,
    entity_id: object,
    actor: str = SYSTEM_ACTOR,
    data: dict[str, Any] | None = None,
) -> AuditEvent:
    """Add an audit event to the current unit of work (committed with it)."""
    event = AuditEvent(
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        request_id=structlog.contextvars.get_contextvars().get("request_id"),
        data=data or {},
    )
    session.add(event)
    return event

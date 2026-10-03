"""Business rules people change from the screens, such as the approval limit.

The configured value (APPROVAL_LIMIT, default 5,000) is the starting point; once someone
saves a new value on the Settings page it is stored in the database and used for every
invoice processed after that. Each change is recorded with who made it, old and new.
Invoices already decided keep the decision they got.
"""

from decimal import Decimal, InvalidOperation

from sqlalchemy.ext.asyncio import AsyncSession

from jev_invoice.config import Settings
from jev_invoice.core import audit
from jev_invoice.core.errors import AppError
from jev_invoice.db.models import AppSetting

APPROVAL_LIMIT = "approval_limit"
MAX_APPROVAL_LIMIT = Decimal("100000000")
_CENT = Decimal("0.01")


class InvalidRuleError(AppError):
    status_code = 422
    code = "invalid_rule"


class RulesService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def approval_limit(self) -> Decimal:
        """Invoices above this total need a budget owner's approval."""
        stored = await self._session.get(AppSetting, APPROVAL_LIMIT)
        return Decimal(stored.value) if stored else self._settings.approval_limit

    async def approval_limit_setting(self) -> AppSetting | None:
        """The saved value with who saved it, or None while the configured default applies."""
        return await self._session.get(AppSetting, APPROVAL_LIMIT)

    async def set_approval_limit(self, value: str | Decimal, *, actor: str) -> Decimal:
        try:
            limit = Decimal(str(value).replace(",", "").replace("$", "").strip())
        except InvalidOperation as exc:
            raise InvalidRuleError(f"'{value}' is not an amount.") from exc
        if not limit.is_finite() or limit <= 0:
            raise InvalidRuleError("The approval limit must be more than zero.")
        if limit > MAX_APPROVAL_LIMIT:
            raise InvalidRuleError(f"The approval limit can be at most {MAX_APPROVAL_LIMIT:,}.")
        limit = limit.quantize(_CENT)

        old = await self.approval_limit()
        stored = await self._session.get(AppSetting, APPROVAL_LIMIT)
        if stored is None:
            self._session.add(AppSetting(key=APPROVAL_LIMIT, value=str(limit), updated_by=actor))
        else:
            stored.value = str(limit)
            stored.updated_by = actor
        if limit != old:
            audit.record(
                self._session,
                action="settings.approval_limit_changed",
                entity_type="setting",
                entity_id=APPROVAL_LIMIT,
                actor=actor,
                data={"changes": {"approval limit": [f"{old:,.2f}", f"{limit:,.2f}"]}},
            )
        await self._session.commit()
        return limit

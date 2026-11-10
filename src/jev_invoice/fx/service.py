"""Exchange rates for invoices in a currency other than the company's.

An INR invoice is converted to USD at the rate for its invoice date, then checked like any
other: the approval limit, the PO tolerance and the payables totals all work in USD. The
original amounts are kept; the rate, its date and its source are stored on the invoice.

Rates are the European Central Bank's daily reference rates, served by Frankfurter
(https://frankfurter.dev): free, no key, one rate per business day. On a weekend or
holiday the last published rate applies, and the stored rate date says which day that
was. Only the currency and a date are sent. Each rate is fetched once and cached.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Protocol

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jev_invoice.db.models import ExchangeRate

log = structlog.get_logger(__name__)

RATE_PLACES = Decimal("0.00000001")


@dataclass(frozen=True)
class Rate:
    """1 unit of `currency` is worth `rate` units of `base`, on `rate_date`."""

    currency: str
    base: str
    rate: Decimal
    rate_date: date
    source: str


class RateSource(Protocol):
    name: str

    async def fetch(self, currency: str, base: str, on: date) -> tuple[Decimal, date] | None:
        """The rate and the date it was published for, or None when there is none."""
        ...


class FrankfurterSource:
    """ECB reference rates through the Frankfurter API."""

    name = "ECB (frankfurter.dev)"

    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")

    async def fetch(self, currency: str, base: str, on: date) -> tuple[Decimal, date] | None:
        try:
            response = await self._client.get(
                f"{self._base_url}/{on.isoformat()}", params={"base": currency, "symbols": base}
            )
            response.raise_for_status()
            body = response.json()
            rate = Decimal(str(body["rates"][base]))
            published = date.fromisoformat(body["date"])
        except (httpx.HTTPError, KeyError, ValueError, TypeError, InvalidOperation) as exc:
            log.warning("exchange_rate_unavailable", currency=currency, on=str(on), error=str(exc))
            return None
        if rate <= 0:
            return None
        return rate.quantize(RATE_PLACES), published


class ExchangeRates:
    """Rates for the company's currency, fetched once per currency and date."""

    def __init__(self, session: AsyncSession, base: str, source: RateSource | None) -> None:
        self._session = session
        self._base = base
        self._source = source

    async def rate(self, currency: str, on: date) -> Rate | None:
        """The rate to convert *currency* on *on*, or None when no rate can be found."""
        if currency == self._base:
            return Rate(currency, self._base, Decimal(1), on, "same currency")
        cached = await self._session.scalar(
            select(ExchangeRate).where(
                ExchangeRate.currency == currency,
                ExchangeRate.base_currency == self._base,
                ExchangeRate.requested_date == on,
            )
        )
        if cached is not None:
            return Rate(currency, self._base, cached.rate, cached.rate_date, cached.source)
        if self._source is None:
            return None
        fetched = await self._source.fetch(currency, self._base, on)
        if fetched is None:
            return None
        rate, published = fetched
        self._session.add(
            ExchangeRate(
                currency=currency,
                base_currency=self._base,
                requested_date=on,
                rate_date=published,
                rate=rate,
                source=self._source.name,
            )
        )
        await self._session.flush()
        return Rate(currency, self._base, rate, published, self._source.name)

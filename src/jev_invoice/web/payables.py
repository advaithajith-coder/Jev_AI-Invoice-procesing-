"""The payables page: approved invoices and what is still owed on each, in tabs.
The old dashboard and report addresses redirect here.
"""

import csv
import io
from collections.abc import Iterable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from jev_invoice.api.dependencies import SessionDep
from jev_invoice.export.formats import safe_text
from jev_invoice.payables.overview import TABS, overview
from jev_invoice.web.routes import templates

router = APIRouter(include_in_schema=False)


def _csv(filename: str, header: list[str], rows: Iterable[Iterable[Any]]) -> Response:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    for row in rows:
        writer.writerow([safe_text(cell) if isinstance(cell, str) else cell for cell in row])
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/payables", response_class=HTMLResponse)
async def payables_page(request: Request, session: SessionDep, tab: str = "open") -> HTMLResponse:
    tab = tab if tab in TABS else "open"
    view = await overview(session)
    return templates.TemplateResponse(
        request,
        "payables.html",
        {
            "o": view,
            "tab": tab,
            "tabs": TABS,
            "counts": view.counts(),
            "rows": view.tab(tab),
            "cur": request.app.state.settings.base_currency,
            "nav": "payables",
        },
    )


@router.get("/payables/invoices.csv")
async def invoices_csv(request: Request, session: SessionDep, tab: str = "open") -> Response:
    base = request.app.state.settings.base_currency
    view = await overview(session)
    rows = view.tab(tab if tab in TABS else "open")
    return _csv(
        f"payables-{tab}-{view.today}.csv",
        [
            "Vendor",
            "Invoice",
            "Invoice date",
            "Pay by",
            "Days late",
            "Currency",
            "Billed",
            "Owed",
            "Plan to pay on",
            "Discount kept",
            "To pay",
            f"To pay ({base})",
            "Status",
        ],
        (
            [
                item.vendor,
                item.invoice.invoice_number or item.invoice.original_filename,
                item.invoice.invoice_date or "",
                item.terms.pay_by if item.terms else "",
                item.days_late,
                item.invoice.currency or base,
                item.balance.billed,
                item.owed,
                "" if item.held or not item.owed else item.pay_on,
                item.discount,
                item.to_pay,
                item.to_pay_base,
                _status(item),
            ]
            for item in rows
        ),
    )


def _status(item: Any) -> str:
    invoice = item.invoice
    if invoice.hold_kind:
        return "disputed" if invoice.hold_kind == "dispute" else "on hold"
    if item.balance.state == "paid":
        return "paid"
    state = "exported" if invoice.status == "exported" else "ready to export"
    return f"{state}, part paid" if item.balance.state == "part_paid" else state


# Old addresses of the dashboard and the reports.
_MOVED = {
    "/approved": "/payables",
    "/reports/aging": "/payables",
    "/reports/cash-forecast": "/payables",
    "/reports/accruals": "/payables",
}


def _redirect(target: str) -> Any:
    async def moved() -> RedirectResponse:
        return RedirectResponse(target, status_code=308)

    return moved


for _old, _new in _MOVED.items():
    router.add_api_route(_old, _redirect(_new), methods=["GET"], include_in_schema=False)

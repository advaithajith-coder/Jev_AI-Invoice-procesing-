"""Per-job model measurements; ContextVar keeps concurrent invoices isolated."""

from contextvars import ContextVar
from typing import Any

current_calls: ContextVar[list[dict[str, Any]] | None] = ContextVar("model_calls", default=None)


def record_call(**values: Any) -> None:
    calls = current_calls.get()
    if calls is not None:
        calls.append(values)

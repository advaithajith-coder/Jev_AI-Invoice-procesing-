"""Mechanical checks against frozen assertions, with missing values kept distinct."""

from contextlib import suppress
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any

MISSING = object()


def lookup(value: Any, path: str) -> Any:
    for part in path.split("."):
        if part == "length" and isinstance(value, list):
            value = len(value)
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        elif isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return MISSING
    return value


def equal(actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    if isinstance(actual, str) and isinstance(expected, str):
        return " ".join(actual.split()).casefold() == " ".join(expected.split()).casefold()
    return bool(actual == expected)


def passes(actual: Any, item: dict[str, Any]) -> bool:
    if actual is MISSING:
        return False
    mode, expected = item["mode"], item["expected"]
    if mode == "nonempty":
        return bool(actual.strip() if isinstance(actual, str) else actual)
    if mode == "one_of":
        return any(equal(actual, alternative) for alternative in expected)
    if mode == "decimal":
        if expected is None:
            return actual is None
        try:
            got, want = Decimal(str(actual)), Decimal(str(expected))
            return got.is_finite() and want.is_finite() and got == want
        except (InvalidOperation, ValueError):
            return False
    if mode not in {"exact", "equal"}:
        raise ValueError(f"Unsupported assertion mode: {mode}")
    return equal(actual, expected)


def score(case: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    value = deepcopy(actual)
    stage = case["stage"]
    if (
        stage == "classification"
        and isinstance(value.get("lines"), list)
        and all(isinstance(row, dict) for row in value["lines"])
    ):
        value["lines"] = sorted(value["lines"], key=lambda row: str(row.get("line_no", "")))
        # Numeric line order, including invoices with more than nine items.
        with suppress(ValueError, TypeError):
            value["lines"].sort(key=lambda row: int(row.get("line_no", 0)))
    if (
        stage == "routing"
        and isinstance(value.get("routes"), list)
        and all(isinstance(row, dict) for row in value["routes"])
    ):
        codes = [row["code"] for row in case["input"].get("exceptions", [])]
        value["routes"].sort(
            key=lambda row: codes.index(row.get("code")) if row.get("code") in codes else len(codes)
        )
    details = []
    for item in case["expected"]["assertions"]:
        got = lookup(value, item["path"])
        details.append(
            {
                **item,
                "actual": None if got is MISSING else got,
                "missing": got is MISSING,
                "passed": passes(got, item),
                "manual_review_required": item["mode"] == "nonempty",
            }
        )
    critical = [row for row in details if row.get("critical")]
    return {
        "fields_checked": len(details),
        "fields_passed": sum(row["passed"] for row in details),
        "critical_pass": all(row["passed"] for row in critical) if critical else None,
        "manual_review_required": any(row["manual_review_required"] for row in details),
        "details": details,
        "note": "Mechanical checks only. Nonempty evidence does not prove its correctness.",
    }

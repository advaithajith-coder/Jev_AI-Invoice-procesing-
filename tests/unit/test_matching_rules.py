"""Pure helpers behind the matching rules."""

import pytest

from jev_invoice.matching.service import _similar_numbers


@pytest.mark.parametrize(
    ("a", "b", "similar"),
    [
        ("AS20431", "AS2O431", True),  # O read for 0
        ("AS20431", "AS2043L", True),  # l read for 1
        ("AS20431", "AS20431R", True),  # re-sent with a suffix
        ("AS20431", "AS20432", False),  # the vendor's next invoice
        ("AS88001", "AS88102", False),
        ("101", "1O1", False),  # too short to judge
    ],
)
def test_similar_invoice_numbers(a: str, b: str, similar: bool) -> None:
    assert _similar_numbers(a, b) is similar

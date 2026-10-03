import pytest

from jev_invoice.domain.vendors import (
    bank_account_fingerprint,
    bank_account_last4,
    normalize_bank_account,
    normalize_vendor_name,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Acme Supplies Inc", "acme supplies"),
        ("ACME SUPPLIES LIMITED.", "acme supplies"),
        ("  Acme   Supplies, Ltd  ", "acme supplies"),
        ("Nordwind Logistik GmbH", "nordwind logistik"),
        ("Brightline Cloud Services Inc.", "brightline cloud services"),
        ("Café Société SARL", "cafe societe"),
        ("Meridian Legal LLP", "meridian legal"),
        ("Company Ltd", "company"),  # strips trailing suffixes, keeps the first word
    ],
)
def test_normalize_vendor_name(raw: str, expected: str) -> None:
    assert normalize_vendor_name(raw) == expected


def test_normalize_keeps_lookalikes_distinct() -> None:
    # Lookalike detection is Jev's job; normalisation must not hide the difference.
    assert normalize_vendor_name("Acme Suppl1es Ltd") != normalize_vendor_name("Acme Supplies Inc")


def test_fingerprint_ignores_formatting() -> None:
    key = b"k"
    assert bank_account_fingerprint("GB29 NWBK 6016 1331 9268 19", key) == bank_account_fingerprint(
        "gb29-nwbk-6016-1331-9268-19", key
    )


def test_fingerprint_depends_on_key_and_account() -> None:
    account = "GB29NWBK60161331926819"
    assert bank_account_fingerprint(account, b"a") != bank_account_fingerprint(account, b"b")
    assert bank_account_fingerprint(account, b"a") != bank_account_fingerprint(
        "GB29NWBK60161331926818", b"a"
    )


def test_fingerprint_does_not_contain_account() -> None:
    fingerprint = bank_account_fingerprint("GB29NWBK60161331926819", b"k")
    assert "60161331926819" not in fingerprint
    assert len(fingerprint) == 64


def test_fingerprint_rejects_empty() -> None:
    with pytest.raises(ValueError, match="empty"):
        bank_account_fingerprint(" - ", b"k")


def test_last4() -> None:
    assert bank_account_last4("Routing 021000021 Account 4417926819") == "6819"


@pytest.mark.parametrize(
    ("printed", "expected"),
    [
        ("GB29 NWBK 6016 1331 9268 19", "GB29NWBK60161331926819"),
        ("gb29-nwbk-6016-1331-9268-19", "GB29NWBK60161331926819"),
        ("IBAN: GB29NWBK60161331926819", "GB29NWBK60161331926819"),
        ("GB29 NWBK 6016 1331 9268 19 Barclays Bank", "GB29NWBK60161331926819"),
        ("DE89 3704 0044 0532 0130 00", "DE89370400440532013000"),
        ("Sort code 04-00-75, Account 19283746 (A. Smith)", "04007519283746"),
        ("Routing 121140399 / Account 9876543210", "1211403999876543210"),
    ],
)
def test_normalize_bank_account_formats(printed: str, expected: str) -> None:
    assert normalize_bank_account(printed) == expected


def test_last4_ignores_account_holder_name() -> None:
    assert bank_account_last4("Sort code 04-00-75, Account 19283746 (A. Smith)") == "3746"

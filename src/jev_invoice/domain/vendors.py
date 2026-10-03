"""Vendor identity helpers: name normalisation and bank-account fingerprints."""

import hashlib
import hmac
import re
import unicodedata

# Legal-form suffixes that do not distinguish one vendor from another.
_LEGAL_SUFFIXES = {
    "ltd", "limited", "plc", "llp", "lp", "inc", "incorporated", "corp", "corporation",
    "co", "company", "llc", "gmbh", "ag", "sa", "sarl", "bv", "nv", "srl", "pty", "pvt",
}  # fmt: skip
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_vendor_name(name: str) -> str:
    """Canonical form for exact matching: 'ACME Supplies Ltd.' -> 'acme supplies'.

    Deliberately does NOT fold lookalike characters ('Acme Suppl1es' stays different).
    Spotting lookalikes is a judgment call that goes to Jev, not a normalisation rule.
    """
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    words = _NON_ALNUM.sub(" ", ascii_name.lower()).split()
    while len(words) > 1 and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


_IBAN_START = re.compile(r"[A-Z]{2}\d{2}[A-Z0-9]*")
_NOT_ALNUM = re.compile(r"[^A-Z0-9]")


def normalize_bank_account(account: str) -> str:
    """Canonical account identifier from text as printed on an invoice.

    - An IBAN, printed compact or in groups ("GB29 NWBK 6016 1331 9268 19"), becomes
      "GB29NWBK60161331926819". Groups are at most 4 characters, which stops the IBAN
      before a following word such as a bank name.
    - Anything else keeps only its digits, so "Sort code 04-00-75, Account 19283746
      (A. Smith)" becomes "04007519283746": labels and the holder's name are dropped.
    """
    tokens = [_NOT_ALNUM.sub("", token) for token in re.split(r"[\s\-]+", account.upper())]
    for index, token in enumerate(tokens):
        if not _IBAN_START.fullmatch(token):
            continue
        parts = [token]
        for following in tokens[index + 1 :]:
            if not 1 <= len(following) <= 4:
                break
            parts.append(following)
        iban = "".join(parts)
        if 15 <= len(iban) <= 34:
            return iban
    return "".join(re.findall(r"\d", account))


def bank_account_fingerprint(account: str, key: bytes) -> str:
    """Keyed HMAC-SHA256 of a normalised account number.

    Account numbers have low entropy, so a plain hash could be reversed by brute force.
    A keyed HMAC lets us compare accounts without ever storing them.
    """
    normalized = normalize_bank_account(account)
    if not normalized:
        raise ValueError("bank account is empty after normalisation")
    return hmac.new(key, normalized.encode(), hashlib.sha256).hexdigest()


def bank_account_last4(account: str) -> str:
    return normalize_bank_account(account)[-4:]


_ACCOUNT_LABELLED = re.compile(
    r"(?P<label>\b(?:account|acct|a/c|iban)\b(?:\s*(?:no\.?|number|#))?\s*[:#]?\s*)"
    # Groups of letters and digits, each containing a digit: "4417 2209", "DE89 3704 ...".
    # Stops at a plain word, so "Account 4417926819 at First Bank" keeps "at First Bank".
    r"(?P<number>[A-Z]{0,2}\d[A-Z0-9]*(?:[ \-][A-Z]*\d[A-Z0-9]*)*)",
    re.IGNORECASE,
)
_LONG_DIGITS = re.compile(r"\d{10,}")


def mask_account_numbers(text: str, *, long_digits: bool = True) -> str:
    """Keep only the last 4 characters of bank account numbers in free text.

    Remittance details and notes are shown to people and stored, but full account
    numbers are never kept. Masks what follows "Account", "Acct", "A/C" or "IBAN", and
    any run of 10 or more digits. Routing numbers (9 digits) and phone numbers printed
    with separators stay readable.
    """

    def labelled(match: re.Match[str]) -> str:
        number = match.group("number")
        compact = re.sub(r"[ \-]", "", number)
        if sum(ch.isdigit() for ch in compact) < 4:
            return match.group(0)
        return f"{match.group('label')}••••{compact[-4:]}"

    masked = _ACCOUNT_LABELLED.sub(labelled, text)
    if not long_digits:  # e.g. a tracking or order number, which is not a bank account
        return masked
    return _LONG_DIGITS.sub(lambda m: f"••••{m.group(0)[-4:]}", masked)

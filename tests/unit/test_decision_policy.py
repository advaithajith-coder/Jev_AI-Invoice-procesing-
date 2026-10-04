"""The decision policy: pure function from code findings + Jev answers to an outcome.

Key property: AUTO_APPROVE only when there are no findings, Jev answered, and every
Jev probability is clearly on the safe side.
"""

import random
from typing import Any

import pytest

from jev_invoice.decision.policy import (
    ACTION_PRIORITY,
    DEFAULT_THRESHOLDS,
    PolicyThresholds,
    decide,
)
from jev_invoice.domain.decision import JevAnswers, PolicyOutcome
from jev_invoice.domain.enums import ActionBand, DecisionAction, ReasonCode
from jev_invoice.domain.invoice import CheckFinding
from tests.fakes import clean_jev_answers

R = ReasonCode


def _decide(
    *codes: ReasonCode,
    jev: JevAnswers | None = None,
    jev_status: str = "ok",
    thresholds: PolicyThresholds = DEFAULT_THRESHOLDS,
) -> PolicyOutcome:
    if jev is None and jev_status == "ok":
        jev = clean_jev_answers()
    return decide(
        findings=[CheckFinding(code=code, message=str(code)) for code in codes],
        jev=jev,
        jev_status=jev_status,  # type: ignore[arg-type]
        thresholds=thresholds,
    )


def test_clean_invoice_is_auto_approved_with_no_reasons() -> None:
    outcome = _decide()

    assert (outcome.action, outcome.band) == (DecisionAction.AUTO_APPROVE, ActionBand.AUTO)
    assert outcome.reason_codes == []
    assert outcome.rationale.startswith("auto_approve: all checks passed")


# ── Code findings ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("code", "action", "band"),
    [
        (R.BANK_ACCOUNT_MISMATCH, "fraud_review", "block"),
        (R.VENDOR_BLOCKED, "fraud_review", "block"),
        (R.DUPLICATE_INVOICE, "reject_document", "confirm"),
        (R.MISSING_REQUIRED_FIELD, "hold_review", "human"),
        (R.TOTALS_MISMATCH, "hold_review", "human"),
        (R.INVALID_DATES, "hold_review", "human"),
        (R.UNKNOWN_VENDOR, "hold_review", "human"),
        (R.VENDOR_ON_HOLD, "hold_review", "human"),
        (R.CURRENCY_MISMATCH, "hold_review", "human"),
        (R.PO_NOT_FOUND, "hold_review", "human"),
        (R.PO_VENDOR_MISMATCH, "hold_review", "human"),
        (R.PO_CLOSED, "hold_review", "human"),
        (R.PO_AMOUNT_EXCEEDED, "hold_review", "human"),
        (R.GOODS_NOT_RECEIVED, "hold_review", "human"),
        (R.OVER_APPROVAL_LIMIT, "route_approval", "confirm"),
        (R.NO_PURCHASE_ORDER, "route_approval", "confirm"),
    ],
)
def test_single_code_finding(code: ReasonCode, action: str, band: str) -> None:
    outcome = _decide(code)

    assert (outcome.action, outcome.band) == (action, band)
    assert outcome.reason_codes == [code]


# ── Jev signals ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("jev_overrides", "action", "band", "reason"),
    [
        ({"bank_details_change": 0.8}, "fraud_review", "block", "BANK_DETAILS_CHANGED"),
        ({"unusual_payment_method": 0.9}, "fraud_review", "block", "UNUSUAL_PAYMENT_METHOD"),
        ({"payment_risk": 1.6}, "fraud_review", "block", "HIGH_PAYMENT_RISK"),
        ({"prompt_injection": 0.6}, "hold_review", "human", "PROMPT_INJECTION"),
        ({"document_type": "quote"}, "reject_document", "confirm", "NOT_AN_INVOICE"),
        ({"is_payable": 0.1}, "reject_document", "confirm", "NOT_PAYABLE"),
        ({"document_type": "credit_note"}, "hold_review", "human", "CREDIT_NOTE"),
        ({"urgent_pressure": 0.75}, "hold_review", "human", "URGENT_PAYMENT_PRESSURE"),
        ({"payment_risk": 0.9}, "hold_review", "human", "MODERATE_PAYMENT_RISK"),
        ({"bank_details_change": 0.5}, "hold_review", "human", "JEV_UNCERTAIN"),
        ({"document_type_confidence": 0.6}, "hold_review", "human", "JEV_UNCERTAIN"),
        ({"is_payable": 0.7}, "hold_review", "human", "JEV_UNCERTAIN"),
    ],
)
def test_single_jev_signal(
    jev_overrides: dict[str, Any], action: str, band: str, reason: str
) -> None:
    outcome = _decide(jev=clean_jev_answers(**jev_overrides))

    assert (outcome.action, outcome.band) == (action, band)
    assert ReasonCode(reason) in outcome.reason_codes


def test_credit_note_is_held_not_rejected_although_it_asks_for_no_payment() -> None:
    outcome = _decide(jev=clean_jev_answers(document_type="credit_note", is_payable=0.02))

    assert (outcome.action, outcome.band) == ("hold_review", "human")
    assert outcome.reason_codes == [ReasonCode.CREDIT_NOTE]


@pytest.mark.parametrize(
    ("status", "reason"),
    [("not_configured", R.JEV_NOT_CONFIGURED), ("unavailable", R.JEV_UNAVAILABLE)],
)
def test_missing_jev_fails_closed(status: str, reason: ReasonCode) -> None:
    outcome = _decide(jev_status=status)

    assert (outcome.action, outcome.band) == (DecisionAction.HOLD_REVIEW, ActionBand.HUMAN)
    assert outcome.reason_codes == [reason]


@pytest.mark.parametrize(
    ("field", "value", "action"),
    [
        ("bank_details_change", 0.70, "fraud_review"),
        ("bank_details_change", 0.6999, "hold_review"),
        ("bank_details_change", 0.3001, "hold_review"),
        ("bank_details_change", 0.30, "auto_approve"),
        ("prompt_injection", 0.50, "hold_review"),
        ("prompt_injection", 0.30, "auto_approve"),
        ("is_payable", 0.30, "reject_document"),
        ("is_payable", 0.8499, "hold_review"),
        ("is_payable", 0.85, "auto_approve"),
        ("document_type_confidence", 0.8499, "hold_review"),
        ("document_type_confidence", 0.85, "auto_approve"),
        ("payment_risk", 1.5, "fraud_review"),
        ("payment_risk", 1.4999, "hold_review"),
        ("payment_risk", 0.75, "hold_review"),
        ("payment_risk", 0.7499, "auto_approve"),
    ],
)
def test_threshold_boundaries(field: str, value: float, action: str) -> None:
    assert _decide(jev=clean_jev_answers(**{field: value})).action == action


def test_low_confidence_non_invoice_label_is_uncertain_not_rejected() -> None:
    outcome = _decide(jev=clean_jev_answers(document_type="quote", document_type_confidence=0.6))

    assert outcome.action == DecisionAction.HOLD_REVIEW
    assert R.NOT_AN_INVOICE not in outcome.reason_codes
    assert R.JEV_UNCERTAIN in outcome.reason_codes


# ── Priority ──────────────────────────────────────────────────────────────────


def test_code_fraud_signal_outranks_clean_jev() -> None:
    # Jev sees nothing wrong in the text, but the account differs from the one on file.
    outcome = _decide(R.BANK_ACCOUNT_MISMATCH, jev=clean_jev_answers())

    assert outcome.action == DecisionAction.FRAUD_REVIEW


def test_fraud_outranks_everything_and_all_reasons_are_recorded() -> None:
    outcome = _decide(
        R.TOTALS_MISMATCH,
        R.OVER_APPROVAL_LIMIT,
        jev=clean_jev_answers(bank_details_change=0.9, prompt_injection=0.9, document_type="quote"),
    )

    assert (outcome.action, outcome.band) == (DecisionAction.FRAUD_REVIEW, ActionBand.BLOCK)
    assert set(outcome.reason_codes) >= {
        R.BANK_DETAILS_CHANGED,
        R.PROMPT_INJECTION,
        R.NOT_AN_INVOICE,
        R.TOTALS_MISMATCH,
        R.OVER_APPROVAL_LIMIT,
    }


def test_injection_outranks_document_type_rejection() -> None:
    assert _decide(jev=clean_jev_answers(prompt_injection=0.9, document_type="quote")).action == (
        DecisionAction.HOLD_REVIEW
    )


def test_missing_jev_outranks_approval_routing() -> None:
    outcome = _decide(R.OVER_APPROVAL_LIMIT, jev_status="unavailable")

    assert outcome.action == DecisionAction.HOLD_REVIEW
    assert R.OVER_APPROVAL_LIMIT in outcome.reason_codes


def test_duplicate_reasons_collapsed() -> None:
    outcome = _decide(
        R.TOTALS_MISMATCH,
        R.TOTALS_MISMATCH,
        jev=clean_jev_answers(document_type_confidence=0.5, is_payable=0.6),
    )

    assert outcome.reason_codes.count(R.TOTALS_MISMATCH) == 1
    assert outcome.reason_codes.count(R.JEV_UNCERTAIN) == 1


def test_every_reason_code_belongs_to_exactly_one_group() -> None:
    grouped = [code for _, _, codes in ACTION_PRIORITY for code in codes]

    assert sorted(grouped) == sorted(ReasonCode)


# ── Invariant sweep ───────────────────────────────────────────────────────────


def test_auto_approve_only_when_every_gate_passes() -> None:
    """Randomised sweep: whenever the policy says AUTO, re-check every gate independently."""
    rng = random.Random(20261002)
    t = DEFAULT_THRESHOLDS
    all_codes = list(ReasonCode)
    auto_seen = 0

    for _ in range(5000):
        jev = None
        if rng.random() > 0.05:
            jev = clean_jev_answers(
                document_type=rng.choice(["invoice"] * 6 + ["credit_note", "quote", "other"]),
                document_type_confidence=rng.random(),
                is_payable=rng.random(),
                prompt_injection=rng.random() ** 3,
                bank_details_change=rng.random() ** 3,
                urgent_pressure=rng.random() ** 3,
                unusual_payment_method=rng.random() ** 3,
                payment_risk=2 * rng.random() ** 2,
            )
        findings = rng.sample(all_codes, k=rng.choice([0, 0, 0, 1, 2]))

        outcome = _decide(*findings, jev=jev, jev_status="ok" if jev else "unavailable")
        if outcome.action != DecisionAction.AUTO_APPROVE:
            assert outcome.band != ActionBand.AUTO
            assert outcome.reason_codes, "every non-auto decision must say why"
            continue

        auto_seen += 1
        assert outcome.band == ActionBand.AUTO and outcome.reason_codes == []
        assert not findings
        assert jev is not None
        assert jev.document_type == "invoice"
        assert jev.document_type_confidence >= t.auto_choice_confidence
        assert jev.is_payable >= t.auto_is_payable
        assert (
            max(
                jev.prompt_injection,
                jev.bank_details_change,
                jev.urgent_pressure,
                jev.unusual_payment_method,
            )
            <= t.risk_clear
        )
        assert jev.payment_risk < t.moderate_risk

    assert auto_seen > 0, "the sweep should exercise the AUTO path"

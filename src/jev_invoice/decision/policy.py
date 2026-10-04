"""Invoice decision policy: code findings + Jev probabilities -> one typed outcome.

Pure function, no I/O. Every applicable reason code is recorded; the action comes
from the highest-priority group that contains a reason. The invariant:

    AUTO_APPROVE only when there are NO reason codes at all.

That means every exact check passed (vendor known and active, bank account on file,
PO matched and received, arithmetic and dates fine, under the approval limit), Jev
answered, Jev is confident the document is a payable invoice, and every risk
probability is clearly low. A probability in the grey zone is a reason
(JEV_UNCERTAIN), not a pass.

Bump POLICY_VERSION whenever a threshold or rule changes.
"""

from dataclasses import dataclass

from jev_invoice.domain.decision import JevAnswers, JevStatus, PolicyOutcome
from jev_invoice.domain.enums import ActionBand, DecisionAction, ReasonCode
from jev_invoice.domain.invoice import CheckFinding

POLICY_VERSION = "p7"  # p7: a credit note is held for a person, never proposed for rejection

R = ReasonCode


@dataclass(frozen=True)
class PolicyThresholds:
    """Starting values; tune on labelled invoices before trusting AUTO."""

    risk_clear: float = 0.30
    """Every risk Noul must be at or below this for AUTO."""
    fraud_signal: float = 0.70
    """A fraud-signal Noul at or above this is treated as present."""
    injection_signal: float = 0.50
    """Lower bar than fraud: a false alarm only costs one review."""
    not_payable: float = 0.30
    """is_payable at or below this means the document does not ask for payment."""
    auto_is_payable: float = 0.85
    auto_choice_confidence: float = 0.85
    """document_type confidence needed to act on it (AUTO or reject)."""
    high_risk: float = 1.5
    """payment_risk (0 Low .. 2 High) at or above this is high."""
    moderate_risk: float = 0.75


DEFAULT_THRESHOLDS = PolicyThresholds()

# Highest priority first. Every ReasonCode belongs to exactly one group,
# so any reason at all prevents AUTO_APPROVE.
ACTION_PRIORITY: tuple[tuple[DecisionAction, ActionBand, frozenset[ReasonCode]], ...] = (
    (
        DecisionAction.FRAUD_REVIEW,
        ActionBand.BLOCK,
        frozenset(
            {
                R.BANK_ACCOUNT_MISMATCH,
                R.VENDOR_BLOCKED,
                R.BANK_DETAILS_CHANGED,
                R.UNUSUAL_PAYMENT_METHOD,
                R.HIGH_PAYMENT_RISK,
            }
        ),
    ),
    (
        # Nothing else can be trusted: the model is missing or the text may be steering it.
        DecisionAction.HOLD_REVIEW,
        ActionBand.HUMAN,
        frozenset({R.PROMPT_INJECTION, R.JEV_NOT_CONFIGURED, R.JEV_UNAVAILABLE}),
    ),
    (
        DecisionAction.REJECT_DOCUMENT,
        ActionBand.CONFIRM,
        frozenset({R.NOT_AN_INVOICE, R.NOT_PAYABLE, R.DUPLICATE_INVOICE}),
    ),
    (
        DecisionAction.HOLD_REVIEW,
        ActionBand.HUMAN,
        frozenset(
            {
                R.CREDIT_NOTE,
                R.MISSING_REQUIRED_FIELD,
                R.TOTALS_MISMATCH,
                R.INVALID_DATES,
                R.FOREIGN_CURRENCY,
                R.STALE_INVOICE,
                R.WRONG_BILL_TO,
                R.SENDER_DOMAIN_MISMATCH,
                R.TAX_ID_MISMATCH,
                R.POSSIBLE_DUPLICATE,
                R.INVOICE_BEFORE_PO,
                R.LINE_NOT_ON_PO,
                R.PRICE_VARIANCE,
                R.QUANTITY_VARIANCE,
                R.UNKNOWN_VENDOR,
                R.VENDOR_ON_HOLD,
                R.CURRENCY_MISMATCH,
                R.PO_NOT_FOUND,
                R.PO_VENDOR_MISMATCH,
                R.PO_CLOSED,
                R.PO_AMOUNT_EXCEEDED,
                R.GOODS_NOT_RECEIVED,
                R.URGENT_PAYMENT_PRESSURE,
                R.MODERATE_PAYMENT_RISK,
                R.JEV_UNCERTAIN,
            }
        ),
    ),
    (
        DecisionAction.ROUTE_APPROVAL,
        ActionBand.CONFIRM,
        frozenset({R.OVER_APPROVAL_LIMIT, R.NO_PURCHASE_ORDER}),
    ),
)


def decide(
    *,
    findings: list[CheckFinding],
    jev: JevAnswers | None,
    jev_status: JevStatus,
    thresholds: PolicyThresholds = DEFAULT_THRESHOLDS,
) -> PolicyOutcome:
    """Decide what happens to one invoice.

    Args:
        findings: problems found by exact checks (document and master data).
        jev: Jev's answers, or None if Jev was not configured or failed.
        jev_status: why jev is None, or "ok".
        thresholds: probability thresholds for Jev answers.
    """
    reasons = [finding.code for finding in findings]
    if jev is None:
        reasons.append(
            R.JEV_NOT_CONFIGURED if jev_status == "not_configured" else R.JEV_UNAVAILABLE
        )
    else:
        reasons.extend(_jev_reasons(jev, thresholds))

    reasons = list(dict.fromkeys(reasons))  # dedupe, keep order
    action, band = _choose_action(reasons)
    return PolicyOutcome(
        action=action,
        band=band,
        reason_codes=reasons,
        rationale=_rationale(action, reasons, jev),
    )


def _jev_reasons(jev: JevAnswers, t: PolicyThresholds) -> list[ReasonCode]:
    reasons: list[ReasonCode] = []
    uncertain = False

    # Act on a non-invoice label only when Jev is confident about it.
    credit_note = False
    if jev.document_type_confidence < t.auto_choice_confidence:
        uncertain = True
    elif jev.document_type == "credit_note":
        reasons.append(R.CREDIT_NOTE)
        credit_note = True
    elif jev.document_type != "invoice":
        reasons.append(R.NOT_AN_INVOICE)

    # A credit note never asks for payment: that is what makes it one, and it is money
    # owed to us, so it goes to a person to apply instead of being proposed for rejection.
    if credit_note:
        pass
    elif jev.is_payable <= t.not_payable:
        reasons.append(R.NOT_PAYABLE)
    elif jev.is_payable < t.auto_is_payable:
        uncertain = True

    signals = (
        (jev.prompt_injection, t.injection_signal, R.PROMPT_INJECTION),
        (jev.bank_details_change, t.fraud_signal, R.BANK_DETAILS_CHANGED),
        (jev.urgent_pressure, t.fraud_signal, R.URGENT_PAYMENT_PRESSURE),
        (jev.unusual_payment_method, t.fraud_signal, R.UNUSUAL_PAYMENT_METHOD),
    )
    for probability, present_at, reason in signals:
        if probability >= present_at:
            reasons.append(reason)
        elif probability > t.risk_clear:
            uncertain = True

    if jev.payment_risk >= t.high_risk:
        reasons.append(R.HIGH_PAYMENT_RISK)
    elif jev.payment_risk >= t.moderate_risk:
        reasons.append(R.MODERATE_PAYMENT_RISK)

    if uncertain:
        reasons.append(R.JEV_UNCERTAIN)
    return reasons


def _choose_action(reasons: list[ReasonCode]) -> tuple[DecisionAction, ActionBand]:
    present = set(reasons)
    for action, band, triggers in ACTION_PRIORITY:
        if present & triggers:
            return action, band
    return DecisionAction.AUTO_APPROVE, ActionBand.AUTO


def _rationale(action: DecisionAction, reasons: list[ReasonCode], jev: JevAnswers | None) -> str:
    because = ", ".join(reasons) if reasons else "all checks passed"
    if jev is None:
        return f"{action}: {because}"
    return (
        f"{action}: {because}. Jev: type={jev.document_type}"
        f"({jev.document_type_confidence:.2f}) payable={jev.is_payable:.2f}"
        f" injection={jev.prompt_injection:.2f} bank_change={jev.bank_details_change:.2f}"
        f" urgent={jev.urgent_pressure:.2f} unusual_method={jev.unusual_payment_method:.2f}"
        f" risk={jev.payment_risk:.2f}/2"
    )

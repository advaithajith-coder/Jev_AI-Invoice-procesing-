"""Typed results of the decision layer.

JevAnswers      calibrated answers from one Jev call
PolicyOutcome   what the policy decided (action, band, reasons), before provenance
DecisionOutcome the outcome plus everything needed to audit or replay it
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

from jev_invoice.domain.enums import ActionBand, DecisionAction, ReasonCode
from jev_invoice.domain.invoice import CheckFinding

JevDocumentType = Literal["invoice", "credit_note", "statement", "quote", "receipt", "other"]
JevStatus = Literal["ok", "not_configured", "unavailable", "not_run"]


class JevAnswers(BaseModel):
    """Calibrated answers from one Jev call. Probabilities are in [0, 1]."""

    document_type: JevDocumentType
    document_type_confidence: float = Field(ge=0.0, le=1.0)
    document_type_probabilities: dict[str, float] = Field(default_factory=dict)
    is_payable: float = Field(ge=0.0, le=1.0, description="P(document requests payment)")
    prompt_injection: float = Field(ge=0.0, le=1.0)
    bank_details_change: float = Field(ge=0.0, le=1.0)
    urgent_pressure: float = Field(ge=0.0, le=1.0)
    unusual_payment_method: float = Field(ge=0.0, le=1.0)
    payment_risk: float = Field(ge=0.0, le=2.0, description="Expected level: 0 Low .. 2 High")
    payment_risk_confidence: float = Field(ge=0.0, le=1.0)


class PolicyOutcome(BaseModel):
    action: DecisionAction
    band: ActionBand
    reason_codes: list[ReasonCode] = Field(default_factory=list)
    rationale: str


class DecisionOutcome(PolicyOutcome):
    findings: list[CheckFinding] = Field(default_factory=list)
    exception_routing: list[dict[str, Any]] = Field(default_factory=list)
    jev_status: JevStatus
    jev: JevAnswers | None = None
    jev_model: str | None = None
    jev_latency_ms: float | None = None
    question_set_version: str
    policy_version: str
    inputs_sha256: str

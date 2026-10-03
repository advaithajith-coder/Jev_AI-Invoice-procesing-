"""Decision service: ask Jev, apply the policy, record provenance.

Never raises for Jev problems. No key, a timeout, an API error, an open circuit
breaker, or malformed answers all fail closed: HOLD_REVIEW for a human.
"""

import hashlib
import json
from dataclasses import dataclass

import structlog

from jev_invoice.config import Settings
from jev_invoice.core.errors import AppError
from jev_invoice.decision.jev_client import JevClient, JevResult
from jev_invoice.decision.policy import (
    DEFAULT_THRESHOLDS,
    POLICY_VERSION,
    PolicyThresholds,
    decide,
)
from jev_invoice.decision.questions import (
    QUESTION_SET_VERSION,
    build_questions,
    build_state,
    parse_answers,
)
from jev_invoice.domain.decision import DecisionOutcome, JevAnswers, JevStatus
from jev_invoice.domain.invoice import CheckFinding

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Judgment:
    """What Jev said about one document, or why it said nothing."""

    answers: JevAnswers | None
    status: JevStatus
    result: JevResult | None = None
    error: str | None = None


class DecisionService:
    def __init__(
        self,
        jev_client: JevClient | None,
        settings: Settings,
        *,
        thresholds: PolicyThresholds = DEFAULT_THRESHOLDS,
    ) -> None:
        self._jev = jev_client
        self._settings = settings
        self._thresholds = thresholds

    @property
    def jev_configured(self) -> bool:
        return self._jev is not None

    def expected_wait_seconds(self) -> float:
        return self._jev.expected_wait_seconds() if self._jev else 0.0

    async def decide(self, document_text: str, findings: list[CheckFinding]) -> DecisionOutcome:
        """Ask Jev, then apply the policy. Same as judge() followed by decide_from()."""
        return self.decide_from(findings, await self.judge(document_text))

    async def judge(self, document_text: str) -> Judgment:
        """Ask Jev the question set. Never raises: problems become a status."""
        if self._jev is None:
            return Judgment(None, "not_configured")
        try:
            result = await self._jev.ask(
                build_state(document_text, self._settings.jev_max_state_chars),
                build_questions(),
            )
            return Judgment(parse_answers(result.response), "ok", result)
        except AppError as exc:  # DecisionError, CircuitOpenError
            log.warning("jev_unavailable_fail_closed", error=exc.code)
            return Judgment(None, "unavailable", error=exc.message)

    def decide_from(self, findings: list[CheckFinding], judgment: Judgment) -> DecisionOutcome:
        """Apply the policy to code findings and Jev's judgment (pure, no I/O)."""
        jev_answers, jev_status, jev_result = judgment.answers, judgment.status, judgment.result
        outcome = decide(
            findings=findings,
            jev=jev_answers,
            jev_status=jev_status,
            thresholds=self._thresholds,
        )
        decision = DecisionOutcome(
            **outcome.model_dump(),
            findings=findings,
            jev_status=jev_status,
            jev=jev_answers,
            jev_model=jev_result.model if jev_result else None,
            jev_latency_ms=jev_result.latency_ms if jev_result else None,
            question_set_version=QUESTION_SET_VERSION,
            policy_version=POLICY_VERSION,
            inputs_sha256=_inputs_hash(findings, jev_answers, jev_status),
        )
        log.info(
            "decision",
            action=decision.action,
            band=decision.band,
            reasons=[str(code) for code in decision.reason_codes],
            jev_status=jev_status,
        )
        return decision


def _inputs_hash(
    findings: list[CheckFinding], jev: JevAnswers | None, jev_status: JevStatus
) -> str:
    payload = {
        "findings": [finding.model_dump(mode="json") for finding in findings],
        "jev": jev.model_dump(mode="json") if jev else None,
        "jev_status": jev_status,
        "policy_version": POLICY_VERSION,
        "question_set_version": QUESTION_SET_VERSION,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()

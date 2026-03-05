"""
Incident Router — orchestrates classification → ITSM platform assignment.

Platform-agnostic: works with any BaseITSMClient implementation.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from src.classifier import ClassificationResult, IncidentClassifier
from src.clients.base_client import BaseITSMClient, ITSMError

logger = logging.getLogger(__name__)


@dataclass
class RoutingDecision:
    """Immutable record of a single routing action."""

    incident_platform_id: str
    incident_number: str
    short_description: str
    assigned_group_key: str
    assigned_group_platform_id: str       # Platform-native group identifier
    assigned_group_name: str
    platform: str                         # "ServiceNow" | "Jira" | "PagerDuty" | etc.
    classification_method: str            # "keyword" | "ml" | "fallback"
    confidence: float
    matched_keywords: list[str]
    is_critical: bool
    routed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error: Optional[str] = None
    success: bool = True


class IncidentRouter:
    """
    Main routing engine — platform-agnostic.

    For each unassigned incident:
      1. Classify using IncidentClassifier (keyword → ML → fallback)
      2. Look up platform-specific group ID from routing_rules
      3. Call client.assign_incident()
      4. Return a RoutingDecision for audit logging

    Parameters
    ----------
    client : BaseITSMClient
        Any ITSM platform client (ServiceNow, Jira, PagerDuty, Ivanti, Freshservice).
    classifier : IncidentClassifier
    routing_rules : dict
        Parsed config/routing_rules.yaml.
    dry_run : bool
        When True, classification runs but the platform is NOT updated.
    """

    def __init__(
        self,
        client: BaseITSMClient,
        classifier: IncidentClassifier,
        routing_rules: dict,
        dry_run: bool = False,
    ) -> None:
        self._client = client
        self._clf = classifier
        self._groups: dict[str, dict] = routing_rules.get("assignment_groups", {})
        self._dry_run = dry_run
        self._platform = client.platform_name

        if dry_run:
            logger.warning(
                "Router running in DRY-RUN mode — %s will NOT be updated", self._platform
            )

    # ─── Public API ──────────────────────────────────────────────────────────

    def route_incident(self, incident: dict) -> RoutingDecision:
        """
        Classify and assign a single normalised incident dict.

        The incident dict must have keys: platform_id, number,
        short_description, description, priority.
        """
        platform_id = incident.get("platform_id", "")
        number = incident.get("number", "UNKNOWN")
        short_desc = incident.get("short_description", "")
        description = incident.get("description", "")

        logger.info("[%s] Routing %s: %r", self._platform, number, short_desc[:80])

        # ── Classify ─────────────────────────────────────────────────────────
        result: ClassificationResult = self._clf.classify(short_desc, description)
        is_critical = self._clf.is_critical(short_desc, description)

        # ── Look up platform-specific group ID ───────────────────────────────
        group_cfg = self._groups.get(result.group_key, {})
        platform_ids: dict = group_cfg.get("platform_ids", {})
        group_platform_id = str(
            platform_ids.get(self._platform.lower(), "")
        )
        group_name = group_cfg.get("display_name", result.group_key)

        work_notes = self._build_work_notes(result, is_critical)

        # ── Assign on platform (unless dry-run) ──────────────────────────────
        error: Optional[str] = None
        success = True

        if self._dry_run:
            logger.info(
                "[DRY-RUN][%s] Would assign %s → %s (method=%s, conf=%.3f)",
                self._platform, number, group_name, result.method, result.confidence,
            )
        else:
            try:
                self._client.assign_incident(
                    platform_id=platform_id,
                    group_id=group_platform_id,
                    work_notes=work_notes,
                )
                logger.info(
                    "[%s] Assigned %s → %s (method=%s, conf=%.3f)",
                    self._platform, number, group_name, result.method, result.confidence,
                )
            except ITSMError as exc:
                error = str(exc)
                success = False
                logger.error("[%s] Failed to assign %s: %s", self._platform, number, exc)

        return RoutingDecision(
            incident_platform_id=platform_id,
            incident_number=number,
            short_description=short_desc,
            assigned_group_key=result.group_key,
            assigned_group_platform_id=group_platform_id,
            assigned_group_name=group_name,
            platform=self._platform,
            classification_method=result.method,
            confidence=result.confidence,
            matched_keywords=result.matched_keywords,
            is_critical=is_critical,
            error=error,
            success=success,
        )

    def route_batch(self, incidents: list[dict]) -> list[RoutingDecision]:
        """Route a list of incidents; continues even if individual assignments fail."""
        decisions = [self.route_incident(i) for i in incidents]
        succeeded = sum(1 for d in decisions if d.success)
        logger.info(
            "[%s] Batch complete: %d/%d routed successfully",
            self._platform, succeeded, len(decisions),
        )
        return decisions

    # ─── Helpers ─────────────────────────────────────────────────────────────

    def _build_work_notes(self, result: ClassificationResult, is_critical: bool) -> str:
        lines = [
            "[Auto-Router] Incident automatically classified and assigned.",
            f"Method       : {result.method}",
            f"Confidence   : {result.confidence:.1%}",
        ]
        if result.matched_keywords:
            lines.append(f"Matched terms: {', '.join(result.matched_keywords[:5])}")
        if is_critical:
            lines.append("⚠ CRITICAL keywords detected — please review priority.")
        return "\n".join(lines)

    @property
    def is_dry_run(self) -> bool:
        return self._dry_run

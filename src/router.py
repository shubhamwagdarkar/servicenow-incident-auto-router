"""
Incident Router — orchestrates classification → ServiceNow assignment.

Ties together IncidentClassifier + ServiceNowClient and produces a
structured RoutingDecision for each incident processed.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from src.classifier import ClassificationResult, IncidentClassifier
from src.snow_client import ServiceNowClient, ServiceNowError

logger = logging.getLogger(__name__)


@dataclass
class RoutingDecision:
    """Immutable record of a single routing action."""

    incident_sys_id: str
    incident_number: str
    short_description: str
    assigned_group_key: str
    assigned_group_sys_id: str
    assigned_group_name: str
    classification_method: str      # "keyword" | "ml" | "fallback"
    confidence: float
    matched_keywords: list[str]
    is_critical: bool
    routed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error: Optional[str] = None
    success: bool = True


class IncidentRouter:
    """
    Main routing engine.

    For each unassigned incident:
      1. Classify using IncidentClassifier (keyword → ML → fallback)
      2. Resolve group metadata from routing_rules
      3. Call ServiceNowClient.assign_incident()
      4. Return a RoutingDecision for audit logging

    Parameters
    ----------
    snow_client : ServiceNowClient
    classifier : IncidentClassifier
    routing_rules : dict
        Parsed config/routing_rules.yaml (used to map group_key → group metadata)
    dry_run : bool
        When True, classification runs but ServiceNow is NOT updated.
    """

    def __init__(
        self,
        snow_client: ServiceNowClient,
        classifier: IncidentClassifier,
        routing_rules: dict,
        dry_run: bool = False,
    ) -> None:
        self._snow = snow_client
        self._clf = classifier
        self._groups: dict[str, dict] = routing_rules.get("assignment_groups", {})
        self._dry_run = dry_run

        if dry_run:
            logger.warning("Router running in DRY-RUN mode — ServiceNow will NOT be updated")

    # ─── Public API ──────────────────────────────────────────────────────────

    def route_incident(self, incident: dict) -> RoutingDecision:
        """
        Classify and assign a single incident record.

        Parameters
        ----------
        incident : dict
            A raw ServiceNow incident dict (from snow_client.get_new_incidents).

        Returns
        -------
        RoutingDecision
        """
        sys_id = incident.get("sys_id", "")
        number = incident.get("number", "UNKNOWN")
        short_desc = incident.get("short_description", "")
        description = incident.get("description", "")

        logger.info("Routing incident %s: %r", number, short_desc[:80])

        # ── Classify ──────────────────────────────────────────────────────────
        result: ClassificationResult = self._clf.classify(short_desc, description)
        is_critical = self._clf.is_critical(short_desc, description)

        # ── Resolve group metadata ────────────────────────────────────────────
        group_cfg = self._groups.get(result.group_key, {})
        group_sys_id = group_cfg.get("sys_id", "")
        group_name = group_cfg.get("display_name", result.group_key)

        work_notes = self._build_work_notes(result, is_critical)

        # ── Assign in ServiceNow (unless dry-run) ─────────────────────────────
        error: Optional[str] = None
        success = True

        if self._dry_run:
            logger.info(
                "[DRY-RUN] Would assign %s → %s (method=%s, conf=%.3f)",
                number,
                group_name,
                result.method,
                result.confidence,
            )
        else:
            try:
                self._snow.assign_incident(
                    sys_id=sys_id,
                    assignment_group_id=group_sys_id,
                    work_notes=work_notes,
                )
                logger.info(
                    "Assigned %s → %s (method=%s, conf=%.3f)",
                    number,
                    group_name,
                    result.method,
                    result.confidence,
                )
            except ServiceNowError as exc:
                error = str(exc)
                success = False
                logger.error("Failed to assign %s: %s", number, exc)

        return RoutingDecision(
            incident_sys_id=sys_id,
            incident_number=number,
            short_description=short_desc,
            assigned_group_key=result.group_key,
            assigned_group_sys_id=group_sys_id,
            assigned_group_name=group_name,
            classification_method=result.method,
            confidence=result.confidence,
            matched_keywords=result.matched_keywords,
            is_critical=is_critical,
            error=error,
            success=success,
        )

    def route_batch(self, incidents: list[dict]) -> list[RoutingDecision]:
        """
        Route a list of incidents and return all decisions.
        Continues processing even if individual assignments fail.
        """
        decisions: list[RoutingDecision] = []
        for incident in incidents:
            decision = self.route_incident(incident)
            decisions.append(decision)

        total = len(decisions)
        succeeded = sum(1 for d in decisions if d.success)
        logger.info("Batch complete: %d/%d routed successfully", succeeded, total)
        return decisions

    # ─── Helpers ─────────────────────────────────────────────────────────────

    def _build_work_notes(
        self, result: ClassificationResult, is_critical: bool
    ) -> str:
        """Compose the work_notes string written back to ServiceNow."""
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

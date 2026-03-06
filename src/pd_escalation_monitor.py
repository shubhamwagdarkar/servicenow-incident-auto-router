"""
PagerDuty Escalation Monitor
=============================
Heartbeat/pulse that escalates unacknowledged PagerDuty incidents.

Rather than re-routing incidents, this monitor watches for incidents that
remain in "triggered" (unacknowledged) state past a configurable SLA window
and pushes them up the escalation policy automatically.

Configuration (config/routing_rules.yaml):
    pagerduty:
      escalation:
        ack_timeout_minutes: 5
        max_escalation_level: 3
        check_interval_seconds: 30
"""

import logging
from datetime import datetime, timezone

from dateutil import parser as dt_parser

from src.clients.pagerduty_client import PagerDutyClient

logger = logging.getLogger(__name__)


class PDEscalationMonitor:
    """
    Lightweight escalation heartbeat for PagerDuty.

    Each pulse():
      1. Fetches triggered (unacknowledged) incidents — minimal fields only
      2. Calculates elapsed time since creation
      3. If elapsed > ack_timeout_minutes → escalates to next policy level
      4. Respects max_escalation_level ceiling (never escalates beyond it)

    Parameters
    ----------
    client : PagerDutyClient
        Authenticated PagerDuty client.
    rules : dict
        Parsed config/routing_rules.yaml. Reads pagerduty.escalation section.
    """

    def __init__(self, client: PagerDutyClient, rules: dict) -> None:
        pd_cfg = rules.get("pagerduty", {}).get("escalation", {})
        self._client = client
        self._ack_timeout_sec: int = int(pd_cfg.get("ack_timeout_minutes", 5)) * 60
        self._max_level: int = int(pd_cfg.get("max_escalation_level", 3))

    # ─── Public API ───────────────────────────────────────────────────────────

    def pulse(self) -> int:
        """
        Single heartbeat check. Returns the number of incidents escalated.

        Designed to be called repeatedly on a short interval (e.g. every 30s).
        Failures on individual incidents are logged but do not abort the loop.
        """
        incidents = self._client.get_triggered_incidents_with_metadata()
        now = datetime.now(timezone.utc)
        escalated = 0

        for inc in incidents:
            try:
                created_at = dt_parser.parse(inc["created_at"])
            except (KeyError, ValueError):
                logger.warning("[PD-Monitor] Skipping incident with unparseable created_at: %s", inc)
                continue

            elapsed_sec = (now - created_at).total_seconds()

            if elapsed_sec <= self._ack_timeout_sec:
                continue  # Still within SLA window — no action needed

            current_level = inc.get("escalation_level", 1)
            next_level = current_level + 1

            if next_level > self._max_level:
                logger.warning(
                    "[PD-Monitor] %s already at max escalation level %d (unacked %.0f min) — no further escalation",
                    inc.get("id", "?"), current_level, elapsed_sec / 60,
                )
                continue

            logger.warning(
                "[PD-Monitor] Escalating %s '%s' level %d → %d (unacked %.0f min)",
                inc.get("id", "?"),
                inc.get("title", "")[:60],
                current_level,
                next_level,
                elapsed_sec / 60,
            )

            if self._client.escalate_incident(inc["id"], next_level):
                escalated += 1

        logger.info(
            "[PD-Monitor] Pulse complete: %d incident(s) escalated out of %d triggered",
            escalated, len(incidents),
        )
        return escalated

    # ─── Properties ───────────────────────────────────────────────────────────

    @property
    def ack_timeout_minutes(self) -> float:
        return self._ack_timeout_sec / 60

    @property
    def max_escalation_level(self) -> int:
        return self._max_level

"""
Unit tests for PDEscalationMonitor — heartbeat escalation logic.
Run with: pytest tests/test_pd_escalation_monitor.py -v
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from src.pd_escalation_monitor import PDEscalationMonitor

RULES = {
    "pagerduty": {
        "escalation": {
            "ack_timeout_minutes": 5,
            "max_escalation_level": 3,
        }
    }
}


def _make_incident(id_: str, minutes_ago: float, level: int = 1) -> dict:
    """Helper — create a mock incident dict with a realistic created_at timestamp."""
    created = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    return {
        "id": id_,
        "created_at": created,
        "escalation_level": level,
        "title": f"Test incident {id_}",
    }


class TestPDEscalationMonitorInit:

    def test_reads_config_correctly(self):
        client = MagicMock()
        monitor = PDEscalationMonitor(client, RULES)
        assert monitor.ack_timeout_minutes == 5.0
        assert monitor.max_escalation_level == 3

    def test_defaults_when_config_missing(self):
        client = MagicMock()
        monitor = PDEscalationMonitor(client, {})
        assert monitor.ack_timeout_minutes == 5.0   # built-in default
        assert monitor.max_escalation_level == 3    # built-in default


class TestPulseEscalation:

    def test_escalates_overdue_incident(self):
        """Incident unacked for 10 min > 5 min threshold → should escalate."""
        client = MagicMock()
        client.get_triggered_incidents_with_metadata.return_value = [
            _make_incident("I1", minutes_ago=10, level=1)
        ]
        client.escalate_incident.return_value = True

        monitor = PDEscalationMonitor(client, RULES)
        count = monitor.pulse()

        assert count == 1
        client.escalate_incident.assert_called_once_with("I1", 2)

    def test_skips_recent_incident(self):
        """Incident unacked for 2 min < 5 min threshold → no escalation."""
        client = MagicMock()
        client.get_triggered_incidents_with_metadata.return_value = [
            _make_incident("I2", minutes_ago=2, level=1)
        ]

        monitor = PDEscalationMonitor(client, RULES)
        count = monitor.pulse()

        assert count == 0
        client.escalate_incident.assert_not_called()

    def test_respects_max_escalation_level(self):
        """Incident already at max level → no further escalation."""
        client = MagicMock()
        client.get_triggered_incidents_with_metadata.return_value = [
            _make_incident("I3", minutes_ago=20, level=3)   # already at max
        ]

        monitor = PDEscalationMonitor(client, RULES)
        count = monitor.pulse()

        assert count == 0
        client.escalate_incident.assert_not_called()

    def test_empty_incident_list_returns_zero(self):
        """No triggered incidents → pulse returns 0 with no API calls."""
        client = MagicMock()
        client.get_triggered_incidents_with_metadata.return_value = []

        monitor = PDEscalationMonitor(client, RULES)
        assert monitor.pulse() == 0
        client.escalate_incident.assert_not_called()

    def test_escalates_from_level_2_to_3(self):
        """Level 2 overdue incident should escalate to level 3 (still within max)."""
        client = MagicMock()
        client.get_triggered_incidents_with_metadata.return_value = [
            _make_incident("I4", minutes_ago=15, level=2)
        ]
        client.escalate_incident.return_value = True

        monitor = PDEscalationMonitor(client, RULES)
        count = monitor.pulse()

        assert count == 1
        client.escalate_incident.assert_called_once_with("I4", 3)

    def test_escalation_api_failure_not_counted(self):
        """If escalate_incident returns False, that incident is NOT counted as escalated."""
        client = MagicMock()
        client.get_triggered_incidents_with_metadata.return_value = [
            _make_incident("I5", minutes_ago=10, level=1)
        ]
        client.escalate_incident.return_value = False

        monitor = PDEscalationMonitor(client, RULES)
        count = monitor.pulse()

        assert count == 0

    def test_mixed_batch(self):
        """3 incidents: 1 recent, 1 overdue (escalated), 1 at max level → count=1."""
        client = MagicMock()
        client.get_triggered_incidents_with_metadata.return_value = [
            _make_incident("I6", minutes_ago=2, level=1),   # recent — skip
            _make_incident("I7", minutes_ago=8, level=1),   # overdue — escalate
            _make_incident("I8", minutes_ago=30, level=3),  # at max — skip
        ]
        client.escalate_incident.return_value = True

        monitor = PDEscalationMonitor(client, RULES)
        count = monitor.pulse()

        assert count == 1
        client.escalate_incident.assert_called_once_with("I7", 2)

    def test_skips_incident_with_bad_timestamp(self):
        """Incident with unparseable created_at is skipped gracefully."""
        client = MagicMock()
        client.get_triggered_incidents_with_metadata.return_value = [
            {"id": "I9", "created_at": "not-a-date", "escalation_level": 1, "title": "bad"}
        ]

        monitor = PDEscalationMonitor(client, RULES)
        count = monitor.pulse()

        assert count == 0
        client.escalate_incident.assert_not_called()

"""
Unit tests for IncidentRouter — routing decisions and dry-run mode.
Run with: pytest tests/test_router.py -v
"""

from unittest.mock import MagicMock, patch

import pytest
import yaml

from src.classifier import ClassificationResult, IncidentClassifier
from src.router import IncidentRouter, RoutingDecision


@pytest.fixture(scope="module")
def routing_rules():
    with open("config/routing_rules.yaml", "r") as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def classifier(routing_rules):
    return IncidentClassifier(routing_rules=routing_rules)


@pytest.fixture
def mock_snow_client():
    client = MagicMock()
    client.health_check.return_value = True
    client.assign_incident.return_value = {"sys_id": "abc123", "number": "INC0001234"}
    return client


@pytest.fixture
def sample_incident():
    return {
        "sys_id": "abc123def456",
        "number": "INC0001234",
        "short_description": "VPN users cannot connect to corporate network",
        "description": "Multiple users reporting VPN connectivity failure since 09:00 UTC",
        "priority": "2",
        "state": "1",
        "opened_at": "2026-03-05 09:05:00",
        "category": "network",
    }


class TestDryRunMode:

    def test_dry_run_does_not_call_assign(
        self, mock_snow_client, classifier, routing_rules, sample_incident
    ):
        router = IncidentRouter(
            snow_client=mock_snow_client,
            classifier=classifier,
            routing_rules=routing_rules,
            dry_run=True,
        )
        decision = router.route_incident(sample_incident)

        mock_snow_client.assign_incident.assert_not_called()
        assert decision.success is True  # dry-run always "succeeds"
        assert decision.incident_number == "INC0001234"

    def test_dry_run_flag_is_exposed(
        self, mock_snow_client, classifier, routing_rules
    ):
        router = IncidentRouter(
            snow_client=mock_snow_client,
            classifier=classifier,
            routing_rules=routing_rules,
            dry_run=True,
        )
        assert router.is_dry_run is True


class TestLiveRoutingMode:

    def test_assigns_network_incident_to_network_group(
        self, mock_snow_client, classifier, routing_rules, sample_incident
    ):
        router = IncidentRouter(
            snow_client=mock_snow_client,
            classifier=classifier,
            routing_rules=routing_rules,
            dry_run=False,
        )
        decision = router.route_incident(sample_incident)

        assert decision.assigned_group_key == "network"
        mock_snow_client.assign_incident.assert_called_once()

    def test_routing_decision_fields_populated(
        self, mock_snow_client, classifier, routing_rules, sample_incident
    ):
        router = IncidentRouter(
            snow_client=mock_snow_client,
            classifier=classifier,
            routing_rules=routing_rules,
            dry_run=False,
        )
        decision = router.route_incident(sample_incident)

        assert decision.incident_sys_id == "abc123def456"
        assert decision.incident_number == "INC0001234"
        assert decision.classification_method in ("keyword", "ml", "fallback")
        assert 0.0 <= decision.confidence <= 1.0
        assert decision.routed_at is not None

    def test_snow_error_sets_success_false(
        self, mock_snow_client, classifier, routing_rules, sample_incident
    ):
        from src.snow_client import ServiceNowError
        mock_snow_client.assign_incident.side_effect = ServiceNowError("403 Forbidden")

        router = IncidentRouter(
            snow_client=mock_snow_client,
            classifier=classifier,
            routing_rules=routing_rules,
            dry_run=False,
        )
        decision = router.route_incident(sample_incident)

        assert decision.success is False
        assert "403" in decision.error


class TestBatchRouting:

    def test_route_batch_returns_all_decisions(
        self, mock_snow_client, classifier, routing_rules
    ):
        incidents = [
            {
                "sys_id": f"sys{i}",
                "number": f"INC000{i}",
                "short_description": desc,
                "description": "",
                "priority": "3",
                "state": "1",
            }
            for i, desc in enumerate(
                [
                    "VPN connectivity issue",
                    "SQL database deadlock detected",
                    "Malware found on endpoint",
                ]
            )
        ]
        router = IncidentRouter(
            snow_client=mock_snow_client,
            classifier=classifier,
            routing_rules=routing_rules,
            dry_run=True,
        )
        decisions = router.route_batch(incidents)

        assert len(decisions) == 3
        groups_assigned = {d.assigned_group_key for d in decisions}
        assert "network" in groups_assigned
        assert "database" in groups_assigned
        assert "security" in groups_assigned

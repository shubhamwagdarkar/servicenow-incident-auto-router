"""
Unit tests for IncidentRouter — platform-agnostic routing and dry-run mode.
Run with: pytest tests/test_router.py -v
"""

from unittest.mock import MagicMock

import pytest
import yaml

from src.classifier import IncidentClassifier
from src.clients.base_client import ITSMError
from src.router import IncidentRouter, RoutingDecision


@pytest.fixture(scope="module")
def routing_rules():
    with open("config/routing_rules.yaml", "r") as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def classifier(routing_rules):
    return IncidentClassifier(routing_rules=routing_rules)


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.platform_name = "ServiceNow"
    client.health_check.return_value = True
    client.assign_incident.return_value = {"platform_id": "abc123"}
    return client


@pytest.fixture
def sample_incident():
    return {
        "platform_id": "abc123def456",
        "number": "INC0001234",
        "short_description": "VPN users cannot connect to corporate network",
        "description": "Multiple users reporting VPN connectivity failure since 09:00 UTC",
        "priority": "2",
    }


class TestDryRunMode:

    def test_dry_run_does_not_call_assign(
        self, mock_client, classifier, routing_rules, sample_incident
    ):
        router = IncidentRouter(
            client=mock_client, classifier=classifier,
            routing_rules=routing_rules, dry_run=True,
        )
        decision = router.route_incident(sample_incident)
        mock_client.assign_incident.assert_not_called()
        assert decision.success is True
        assert decision.incident_number == "INC0001234"

    def test_dry_run_flag_is_exposed(self, mock_client, classifier, routing_rules):
        router = IncidentRouter(
            client=mock_client, classifier=classifier,
            routing_rules=routing_rules, dry_run=True,
        )
        assert router.is_dry_run is True


class TestLiveRoutingMode:

    def test_assigns_network_incident_to_network_group(
        self, mock_client, classifier, routing_rules, sample_incident
    ):
        router = IncidentRouter(
            client=mock_client, classifier=classifier,
            routing_rules=routing_rules, dry_run=False,
        )
        decision = router.route_incident(sample_incident)
        assert decision.assigned_group_key == "network"
        mock_client.assign_incident.assert_called_once()

    def test_routing_decision_has_platform_field(
        self, mock_client, classifier, routing_rules, sample_incident
    ):
        router = IncidentRouter(
            client=mock_client, classifier=classifier,
            routing_rules=routing_rules, dry_run=False,
        )
        decision = router.route_incident(sample_incident)
        assert decision.platform == "ServiceNow"

    def test_routing_decision_fields_populated(
        self, mock_client, classifier, routing_rules, sample_incident
    ):
        router = IncidentRouter(
            client=mock_client, classifier=classifier,
            routing_rules=routing_rules, dry_run=False,
        )
        decision = router.route_incident(sample_incident)
        assert decision.incident_platform_id == "abc123def456"
        assert decision.incident_number == "INC0001234"
        assert decision.classification_method in ("keyword", "ml", "fallback")
        assert 0.0 <= decision.confidence <= 1.0
        assert decision.routed_at is not None

    def test_itsm_error_sets_success_false(
        self, mock_client, classifier, routing_rules, sample_incident
    ):
        mock_client.assign_incident.side_effect = ITSMError("403 Forbidden")
        router = IncidentRouter(
            client=mock_client, classifier=classifier,
            routing_rules=routing_rules, dry_run=False,
        )
        decision = router.route_incident(sample_incident)
        assert decision.success is False
        assert "403" in decision.error

    def test_platform_id_in_routing_rules(
        self, mock_client, classifier, routing_rules, sample_incident
    ):
        """ServiceNow platform_id for 'network' should be netops-group-001."""
        router = IncidentRouter(
            client=mock_client, classifier=classifier,
            routing_rules=routing_rules, dry_run=True,
        )
        decision = router.route_incident(sample_incident)
        assert decision.assigned_group_platform_id == "netops-group-001"


class TestBatchRouting:

    def test_route_batch_returns_all_decisions(
        self, mock_client, classifier, routing_rules
    ):
        incidents = [
            {"platform_id": f"id{i}", "number": f"INC000{i}",
             "short_description": desc, "description": "", "priority": "3"}
            for i, desc in enumerate([
                "VPN connectivity issue",
                "SQL database deadlock detected",
                "Malware found on endpoint",
            ])
        ]
        router = IncidentRouter(
            client=mock_client, classifier=classifier,
            routing_rules=routing_rules, dry_run=True,
        )
        decisions = router.route_batch(incidents)
        assert len(decisions) == 3
        groups = {d.assigned_group_key for d in decisions}
        assert "network" in groups
        assert "database" in groups
        assert "security" in groups


class TestMultiPlatform:

    def test_jira_client_uses_jira_platform_id(
        self, classifier, routing_rules, sample_incident
    ):
        """When platform is Jira, group platform_id should use jira mapping."""
        jira_client = MagicMock()
        jira_client.platform_name = "Jira"
        jira_client.assign_incident.return_value = {}

        router = IncidentRouter(
            client=jira_client, classifier=classifier,
            routing_rules=routing_rules, dry_run=True,
        )
        decision = router.route_incident(sample_incident)
        assert decision.platform == "Jira"
        assert decision.assigned_group_platform_id == "Network"  # Jira component name

    def test_pagerduty_client_uses_pagerduty_platform_id(
        self, classifier, routing_rules, sample_incident
    ):
        pd_client = MagicMock()
        pd_client.platform_name = "PagerDuty"
        pd_client.assign_incident.return_value = {}

        router = IncidentRouter(
            client=pd_client, classifier=classifier,
            routing_rules=routing_rules, dry_run=True,
        )
        # Use dry_run — just verify platform field
        router2 = IncidentRouter(
            client=pd_client, classifier=classifier,
            routing_rules=routing_rules, dry_run=True,
        )
        decision = router2.route_incident(sample_incident)
        assert decision.platform == "PagerDuty"
        assert decision.assigned_group_platform_id == "PD_NETOPS_EP_001"

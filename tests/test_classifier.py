"""
Unit tests for IncidentClassifier — keyword and ML routing.
Run with: pytest tests/test_classifier.py -v
"""

import pytest
import yaml

from src.classifier import IncidentClassifier


@pytest.fixture(scope="module")
def routing_rules():
    with open("config/routing_rules.yaml", "r") as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def classifier(routing_rules):
    return IncidentClassifier(routing_rules=routing_rules)


# ─── Keyword Classification Tests ─────────────────────────────────────────────

class TestKeywordClassification:

    def test_network_incident_routes_to_network(self, classifier):
        result = classifier.classify("VPN connectivity issue — users cannot connect")
        assert result.group_key == "network"
        assert result.method == "keyword"
        assert result.confidence >= 0.75

    def test_security_incident_routes_to_security(self, classifier):
        result = classifier.classify("Phishing email detected in mailbox")
        assert result.group_key == "security"
        assert result.method == "keyword"

    def test_database_incident_routes_to_database(self, classifier):
        result = classifier.classify("PostgreSQL deadlock causing transaction failures")
        assert result.group_key == "database"
        assert result.method == "keyword"

    def test_application_incident_routes_to_application(self, classifier):
        result = classifier.classify("Kubernetes pod OOMKilled — container crash loop")
        assert result.group_key == "application"
        assert result.method == "keyword"

    def test_infrastructure_incident_routes_to_infra(self, classifier):
        result = classifier.classify("VMware hypervisor CPU at 98% — server unresponsive")
        assert result.group_key == "infrastructure"
        assert result.method == "keyword"

    def test_matched_keywords_populated(self, classifier):
        result = classifier.classify("DNS resolution failure causing network outage")
        assert len(result.matched_keywords) >= 1
        assert any(kw in ["network", "dns", "outage"] for kw in result.matched_keywords)


# ─── Critical Keyword Detection ───────────────────────────────────────────────

class TestCriticalDetection:

    def test_production_down_is_critical(self, classifier):
        assert classifier.is_critical("Production database is down — all users affected") is True

    def test_outage_is_critical(self, classifier):
        assert classifier.is_critical("Network outage in primary DC") is True

    def test_normal_incident_not_critical(self, classifier):
        assert classifier.is_critical("Printer offline on 3rd floor") is False

    def test_ransomware_is_critical(self, classifier):
        assert classifier.is_critical("Ransomware detected on file server") is True


# ─── ML Fallback Tests ────────────────────────────────────────────────────────

class TestMLClassification:

    def test_ml_classifies_novel_text(self, classifier):
        # No exact keywords but semantically related to security
        result = classifier.classify(
            "Suspicious login attempts detected from unknown IP addresses"
        )
        # Should classify to security or application — not fail
        assert result.group_key in classifier._groups
        assert result.confidence > 0

    def test_classification_result_has_required_fields(self, classifier):
        result = classifier.classify("Firewall rule blocking internal traffic")
        assert result.group_key is not None
        assert result.method in ("keyword", "ml", "fallback")
        assert 0.0 <= result.confidence <= 1.0


# ─── Fallback Tests ───────────────────────────────────────────────────────────

class TestFallback:

    def test_empty_description_does_not_crash(self, classifier):
        result = classifier.classify("", "")
        assert result.group_key is not None

    def test_gibberish_returns_fallback_or_low_confidence(self, classifier):
        result = classifier.classify("xyzzy qwerty asdfgh 12345 !!!!")
        # Either routes to fallback or returns low confidence ML result
        assert result.group_key is not None

"""
Unit tests for JiraClient — normalisation and error handling.
Run with: pytest tests/clients/test_jira_client.py -v
"""

from unittest.mock import MagicMock, patch

import pytest

from src.clients.jira_client import JiraClient
from src.clients.base_client import ITSMError


@pytest.fixture
def client():
    return JiraClient(
        instance_url="https://testorg.atlassian.net",
        email="test@testorg.com",
        api_token="test_token",
    )


class TestNormalisation:

    def test_normalise_maps_fields_correctly(self, client):
        raw = {
            "key": "HELPDESK-142",
            "fields": {
                "summary": "VPN not working for remote users",
                "description": "text content here",
                "priority": {"name": "High"},
            },
        }
        result = client._normalise(raw)
        assert result["platform_id"] == "HELPDESK-142"
        assert result["number"] == "HELPDESK-142"
        assert result["short_description"] == "VPN not working for remote users"
        assert result["priority"] == "High"

    def test_normalise_handles_missing_fields(self, client):
        result = client._normalise({"key": "TEST-1", "fields": {}})
        assert result["platform_id"] == "TEST-1"
        assert result["short_description"] == ""
        assert result["priority"] == ""


class TestGetNewIncidents:

    def test_returns_normalised_incidents(self, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "issues": [
                {
                    "key": "IT-10",
                    "fields": {
                        "summary": "Network outage in building B",
                        "description": None,
                        "priority": {"name": "Critical"},
                    },
                }
            ]
        }
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client._session, "get", return_value=mock_resp):
            incidents = client.get_new_incidents(limit=10)

        assert len(incidents) == 1
        assert incidents[0]["number"] == "IT-10"
        assert incidents[0]["short_description"] == "Network outage in building B"

    def test_http_error_raises_itsm_error(self, client):
        import requests
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = requests.exceptions.HTTPError("401")

        with patch.object(client._session, "get", return_value=mock_resp):
            with pytest.raises(ITSMError, match="Jira GET issues failed"):
                client.get_new_incidents()


class TestAssignIncident:

    def test_assign_puts_component(self, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 204
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client._session, "put", return_value=mock_resp) as mock_put:
            result = client.assign_incident("IT-10", "Network")

        mock_put.assert_called_once()
        payload = mock_put.call_args[1]["json"]
        assert payload["fields"]["components"] == [{"name": "Network"}]

    def test_platform_name(self, client):
        assert client.platform_name == "Jira"

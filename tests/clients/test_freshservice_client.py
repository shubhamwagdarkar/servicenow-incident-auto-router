"""
Unit tests for FreshserviceClient — normalisation and error handling.
Run with: pytest tests/clients/test_freshservice_client.py -v
"""

from unittest.mock import MagicMock, patch

import pytest

from src.clients.freshservice_client import FreshserviceClient
from src.clients.base_client import ITSMError


@pytest.fixture
def client():
    return FreshserviceClient(
        instance_url="https://testorg.freshservice.com",
        api_key="test_api_key",
    )


class TestNormalisation:

    def test_normalise_maps_fields_correctly(self, client):
        raw = {
            "id": 42,
            "subject": "Printer offline on 3rd floor",
            "description_text": "HP LaserJet not responding since this morning",
            "priority": 2,
            "status": 2,
            "group_id": None,
        }
        result = client._normalise(raw)
        assert result["platform_id"] == "42"
        assert result["number"] == "FS-42"
        assert result["short_description"] == "Printer offline on 3rd floor"
        assert result["description"] == "HP LaserJet not responding since this morning"
        assert result["priority"] == "2"

    def test_normalise_handles_missing_fields(self, client):
        result = client._normalise({})
        assert result["platform_id"] == ""
        assert result["number"] == "FS-"
        assert result["short_description"] == ""


class TestGetNewIncidents:

    def test_filters_assigned_tickets(self, client):
        """Tickets with a group_id should be excluded from results."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "tickets": [
                {"id": 1, "subject": "Ticket A", "description_text": "", "priority": 2, "group_id": None},
                {"id": 2, "subject": "Ticket B", "description_text": "", "priority": 2, "group_id": 99},
            ]
        }
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client._session, "get", return_value=mock_resp):
            incidents = client.get_new_incidents(limit=10)

        assert len(incidents) == 1
        assert incidents[0]["number"] == "FS-1"

    def test_http_error_raises_itsm_error(self, client):
        import requests
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = requests.exceptions.HTTPError("404")

        with patch.object(client._session, "get", return_value=mock_resp):
            with pytest.raises(ITSMError, match="Freshservice GET tickets failed"):
                client.get_new_incidents()


class TestAssignIncident:

    def test_assign_calls_put_with_group_id(self, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ticket": {"id": 42, "group_id": 12001}}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client._session, "put", return_value=mock_resp) as mock_put:
            result = client.assign_incident("42", "12001")

        mock_put.assert_called_once()
        call_kwargs = mock_put.call_args
        assert call_kwargs[1]["json"]["group_id"] == 12001

    def test_platform_name(self, client):
        assert client.platform_name == "Freshservice"

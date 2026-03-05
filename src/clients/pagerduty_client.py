"""
PagerDuty REST API Client
Wraps PagerDuty v2 API for incident polling and escalation policy assignment.

Auth: API key in Authorization header
Docs: https://developer.pagerduty.com/api-reference/
"""

import logging
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.clients.base_client import BaseITSMClient, ITSMError

logger = logging.getLogger(__name__)

PD_API_BASE = "https://api.pagerduty.com"


class PagerDutyClient(BaseITSMClient):
    """
    PagerDuty incident client.

    Usage::

        client = PagerDutyClient(
            api_key="your_pd_api_key",
            from_email="oncall@yourorg.com",   # Required by PD API for write ops
        )
        incidents = client.get_new_incidents(limit=25)
    """

    def __init__(
        self,
        api_key: str,
        from_email: str,
        timeout: int = 30,
        max_retries: int = 3,
    ) -> None:
        self._from_email = from_email
        self._timeout = timeout
        self._session = self._build_session(api_key, max_retries)

    def _build_session(self, api_key: str, max_retries: int) -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                "Authorization": f"Token token={api_key}",
                "Accept": "application/vnd.pagerduty+json;version=2",
                "Content-Type": "application/json",
                "From": self._from_email,
            }
        )
        retry = Retry(
            total=max_retries,
            backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "PUT"],
        )
        session.mount("https://", HTTPAdapter(max_retries=retry))
        return session

    # ─── Normalisation ────────────────────────────────────────────────────────

    def _normalise(self, incident: dict) -> dict:
        """Convert a PagerDuty incident dict to the shared incident schema."""
        return {
            "platform_id": incident.get("id", ""),
            "number": incident.get("incident_number", ""),
            "short_description": incident.get("title", ""),
            "description": incident.get("description", ""),
            "priority": (incident.get("priority") or {}).get("name", ""),
        }

    # ─── Public Methods ───────────────────────────────────────────────────────

    def get_new_incidents(self, limit: int = 50) -> list[dict]:
        """
        Fetch triggered (unacknowledged) PagerDuty incidents with no assignments.
        """
        params = {
            "statuses[]": ["triggered"],
            "limit": limit,
            "sort_by": "created_at:asc",
        }
        try:
            resp = self._session.get(
                f"{PD_API_BASE}/incidents",
                params=params,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            incidents = resp.json().get("incidents", [])
            logger.info("Fetched %d incidents from PagerDuty", len(incidents))
            return [self._normalise(i) for i in incidents]
        except requests.exceptions.HTTPError as exc:
            raise ITSMError(f"PagerDuty GET incidents failed: {exc}") from exc

    def assign_incident(
        self,
        platform_id: str,
        group_id: str,
        work_notes: str = "",
    ) -> dict:
        """
        Reassign a PagerDuty incident to an escalation policy.
        group_id = PagerDuty escalation policy ID (e.g. "P1ABC23").
        """
        url = f"{PD_API_BASE}/incidents/{platform_id}"
        payload = {
            "incident": {
                "type": "incident_reference",
                "escalation_policy": {
                    "id": group_id,
                    "type": "escalation_policy_reference",
                },
            }
        }
        try:
            resp = self._session.put(url, json=payload, timeout=self._timeout)
            resp.raise_for_status()
            result = resp.json().get("incident", {})
            logger.info(
                "Assigned PagerDuty incident %s → escalation_policy=%s",
                platform_id,
                group_id,
            )
            return result
        except requests.exceptions.HTTPError as exc:
            raise ITSMError(
                f"PagerDuty assign incident {platform_id} failed: {exc}"
            ) from exc

    def health_check(self) -> bool:
        try:
            resp = self._session.get(
                f"{PD_API_BASE}/abilities",
                timeout=self._timeout,
            )
            return resp.status_code == 200
        except requests.exceptions.RequestException:
            return False

    @property
    def platform_name(self) -> str:
        return "PagerDuty"

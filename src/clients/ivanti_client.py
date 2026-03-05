"""
Ivanti Neurons for ITSM Client
Wraps Ivanti REST API (OData) for incident polling and team assignment.

Auth: API token in Authorization header
Docs: https://help.ivanti.com/ht/help/en_US/ISM/2023/Content/Configure/API/REST-API.htm
"""

import logging
from typing import Optional
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.clients.base_client import BaseITSMClient, ITSMError

logger = logging.getLogger(__name__)


class IvantiClient(BaseITSMClient):
    """
    Ivanti Neurons for ITSM incident client.

    Usage::

        client = IvantiClient(
            instance_url="https://yourorg.ivanticloud.com",
            api_token="your_api_token",
        )
        incidents = client.get_new_incidents(limit=25)
    """

    # OData endpoint for Incident business object
    INCIDENT_ENDPOINT = "/api/odata/businessobject/incidents"

    def __init__(
        self,
        instance_url: str,
        api_token: str,
        timeout: int = 30,
        max_retries: int = 3,
    ) -> None:
        self.base_url = instance_url.rstrip("/")
        self._timeout = timeout
        self._session = self._build_session(api_token, max_retries)

    def _build_session(self, api_token: str, max_retries: int) -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                "Authorization": f"rest_api_key={api_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )
        retry = Retry(
            total=max_retries,
            backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "PATCH"],
        )
        session.mount("https://", HTTPAdapter(max_retries=retry))
        return session

    # ─── Normalisation ────────────────────────────────────────────────────────

    def _normalise(self, record: dict) -> dict:
        """Convert an Ivanti incident record to the shared incident schema."""
        return {
            "platform_id": record.get("RecId", ""),
            "number": record.get("IncidentNumber", record.get("RecId", "")),
            "short_description": record.get("Subject", ""),
            "description": record.get("Symptom", ""),
            "priority": str(record.get("Priority", "")),
        }

    # ─── Public Methods ───────────────────────────────────────────────────────

    def get_new_incidents(self, limit: int = 50) -> list[dict]:
        """
        Fetch active Ivanti incidents with no assigned team (OData filter).
        """
        odata_filter = "Status eq 'Active' and Team eq ''"
        params = {
            "$filter": odata_filter,
            "$top": limit,
            "$select": "RecId,IncidentNumber,Subject,Symptom,Priority,Status,Team",
            "$orderby": "CreatedDateTime asc",
        }
        url = f"{self.base_url}{self.INCIDENT_ENDPOINT}"
        try:
            resp = self._session.get(url, params=params, timeout=self._timeout)
            resp.raise_for_status()
            records = resp.json().get("value", [])
            logger.info("Fetched %d incidents from Ivanti", len(records))
            return [self._normalise(r) for r in records]
        except requests.exceptions.HTTPError as exc:
            raise ITSMError(f"Ivanti GET incidents failed: {exc}") from exc

    def assign_incident(
        self,
        platform_id: str,
        group_id: str,
        work_notes: str = "",
    ) -> dict:
        """
        Assign an Ivanti incident to a team.
        group_id = Team name string (e.g. "Network Operations").
        """
        encoded_id = quote(platform_id, safe="")
        url = f"{self.base_url}{self.INCIDENT_ENDPOINT}('{encoded_id}')"
        payload: dict = {"Team": group_id}
        if work_notes:
            payload["Resolution"] = work_notes

        try:
            resp = self._session.patch(url, json=payload, timeout=self._timeout)
            resp.raise_for_status()
            logger.info(
                "Assigned Ivanti incident %s → team=%s", platform_id, group_id
            )
            return {"RecId": platform_id, "Team": group_id}
        except requests.exceptions.HTTPError as exc:
            raise ITSMError(
                f"Ivanti assign incident {platform_id} failed: {exc}"
            ) from exc

    def health_check(self) -> bool:
        try:
            resp = self._session.get(
                f"{self.base_url}/api/odata/businessobject/incidents?$top=1",
                timeout=self._timeout,
            )
            return resp.status_code == 200
        except requests.exceptions.RequestException:
            return False

    @property
    def platform_name(self) -> str:
        return "Ivanti"

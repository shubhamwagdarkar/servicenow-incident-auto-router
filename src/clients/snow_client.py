"""
ServiceNow REST API Client
Wraps Table API calls for incident polling and assignment.
"""

import logging
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.clients.base_client import BaseITSMClient, ITSMError

logger = logging.getLogger(__name__)


class ServiceNowClient(BaseITSMClient):
    """
    ServiceNow Table REST API client.

    Usage::

        client = ServiceNowClient(
            instance_url="https://myinstance.service-now.com",
            username="admin",
            password="secret",
        )
        incidents = client.get_new_incidents(limit=25)
    """

    TABLE_INCIDENT = "incident"

    def __init__(
        self,
        instance_url: str,
        username: str,
        password: str,
        timeout: int = 30,
        max_retries: int = 3,
    ) -> None:
        self.base_url = instance_url.rstrip("/")
        self._auth = (username, password)
        self._timeout = timeout
        self._session = self._build_session(max_retries)

    def _build_session(self, max_retries: int) -> requests.Session:
        session = requests.Session()
        session.auth = self._auth
        session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        retry = Retry(
            total=max_retries,
            backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "PATCH"],
        )
        session.mount("https://", HTTPAdapter(max_retries=retry))
        session.mount("http://", HTTPAdapter(max_retries=retry))
        return session

    def _url(self, table: str, sys_id: Optional[str] = None) -> str:
        base = f"{self.base_url}/api/now/table/{table}"
        return f"{base}/{sys_id}" if sys_id else base

    # ─── Normalisation ────────────────────────────────────────────────────────

    def _normalise(self, record: dict) -> dict:
        """Convert a ServiceNow incident record to the shared incident schema."""
        return {
            "platform_id": record.get("sys_id", ""),
            "number": record.get("number", ""),
            "short_description": record.get("short_description", ""),
            "description": record.get("description", ""),
            "priority": record.get("priority", ""),
        }

    # ─── Public Methods ───────────────────────────────────────────────────────

    def get_new_incidents(
        self,
        limit: int = 50,
        states: Optional[list[int]] = None,
    ) -> list[dict]:
        """Fetch unassigned incidents in the given states (default: New = 1)."""
        if states is None:
            states = [1]

        state_query = "^ORstate=".join(str(s) for s in states)
        sysparm_query = (
            f"state={state_query}"
            "^assignment_group=NULL"
            "^ORassignment_group.nameISEMPTY"
        )
        params = {
            "sysparm_query": sysparm_query,
            "sysparm_limit": limit,
            "sysparm_fields": (
                "sys_id,number,short_description,description,priority,category,state"
            ),
            "sysparm_display_value": "false",
        }
        try:
            resp = self._session.get(
                self._url(self.TABLE_INCIDENT),
                params=params,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            records = resp.json().get("result", [])
            logger.info("Fetched %d incidents from ServiceNow", len(records))
            return [self._normalise(r) for r in records]
        except requests.exceptions.HTTPError as exc:
            raise ITSMError(f"ServiceNow GET incidents failed: {exc}") from exc

    def assign_incident(
        self,
        platform_id: str,
        group_id: str,
        work_notes: str = "",
    ) -> dict:
        """Update an incident's assignment_group. group_id = ServiceNow sys_id."""
        payload: dict = {"assignment_group": group_id}
        if work_notes:
            payload["work_notes"] = work_notes
        try:
            resp = self._session.patch(
                self._url(self.TABLE_INCIDENT, platform_id),
                json=payload,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            logger.info("Assigned ServiceNow incident %s → group=%s", platform_id, group_id)
            return resp.json().get("result", {})
        except requests.exceptions.HTTPError as exc:
            raise ITSMError(f"ServiceNow assign {platform_id} failed: {exc}") from exc

    def health_check(self) -> bool:
        try:
            resp = self._session.get(
                f"{self.base_url}/api/now/table/sys_user",
                params={"sysparm_limit": 1},
                timeout=self._timeout,
            )
            return resp.status_code == 200
        except requests.exceptions.RequestException:
            return False

    @property
    def platform_name(self) -> str:
        return "ServiceNow"

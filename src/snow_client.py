"""
ServiceNow REST API Client
Wraps Table API calls for incident polling and assignment.
"""

import logging
from typing import Optional
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


class ServiceNowError(Exception):
    """Raised when the ServiceNow API returns an unexpected response."""


class ServiceNowClient:
    """
    Minimal wrapper around the ServiceNow Table REST API.

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

    # ─── Session Setup ────────────────────────────────────────────────────────

    def _build_session(self, max_retries: int) -> requests.Session:
        session = requests.Session()
        session.auth = self._auth
        session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )
        retry_strategy = Retry(
            total=max_retries,
            backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "PATCH"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    # ─── Table API Helpers ────────────────────────────────────────────────────

    def _url(self, table: str, sys_id: Optional[str] = None) -> str:
        base = f"{self.base_url}/api/now/table/{table}"
        return f"{base}/{sys_id}" if sys_id else base

    def _get(self, table: str, params: dict) -> list[dict]:
        url = self._url(table)
        try:
            resp = self._session.get(url, params=params, timeout=self._timeout)
            resp.raise_for_status()
            return resp.json().get("result", [])
        except requests.exceptions.HTTPError as exc:
            raise ServiceNowError(f"GET {url} failed: {exc}") from exc

    def _patch(self, table: str, sys_id: str, payload: dict) -> dict:
        url = self._url(table, sys_id)
        try:
            resp = self._session.patch(url, json=payload, timeout=self._timeout)
            resp.raise_for_status()
            return resp.json().get("result", {})
        except requests.exceptions.HTTPError as exc:
            raise ServiceNowError(f"PATCH {url} failed: {exc}") from exc

    # ─── Public Methods ───────────────────────────────────────────────────────

    def get_new_incidents(
        self,
        limit: int = 50,
        states: Optional[list[int]] = None,
    ) -> list[dict]:
        """
        Fetch unassigned incidents in the given states (default: New = 1).

        Returns a list of incident records with the fields needed for routing.
        """
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
                "sys_id,number,short_description,description,"
                "priority,category,state,opened_at,caller_id"
            ),
            "sysparm_display_value": "false",
        }
        logger.debug("Polling for new incidents | query=%s limit=%d", sysparm_query, limit)
        incidents = self._get(self.TABLE_INCIDENT, params)
        logger.info("Fetched %d new incidents from ServiceNow", len(incidents))
        return incidents

    def assign_incident(
        self,
        sys_id: str,
        assignment_group_id: str,
        work_notes: str = "",
    ) -> dict:
        """
        Update an incident's assignment_group and optionally add work_notes.

        Returns the updated incident record.
        """
        payload: dict = {"assignment_group": assignment_group_id}
        if work_notes:
            payload["work_notes"] = work_notes

        result = self._patch(self.TABLE_INCIDENT, sys_id, payload)
        logger.info(
            "Assigned incident sys_id=%s to group=%s",
            sys_id,
            assignment_group_id,
        )
        return result

    def get_incident(self, sys_id: str) -> dict:
        """Fetch a single incident record by sys_id."""
        params = {"sysparm_display_value": "false"}
        try:
            resp = self._session.get(
                self._url(self.TABLE_INCIDENT, sys_id),
                params=params,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            return resp.json().get("result", {})
        except requests.exceptions.HTTPError as exc:
            raise ServiceNowError(f"GET incident {sys_id} failed: {exc}") from exc

    def health_check(self) -> bool:
        """
        Quick connectivity check — returns True if ServiceNow is reachable
        and credentials are valid.
        """
        try:
            resp = self._session.get(
                f"{self.base_url}/api/now/table/sys_user",
                params={"sysparm_limit": 1},
                timeout=self._timeout,
            )
            return resp.status_code == 200
        except requests.exceptions.RequestException:
            return False

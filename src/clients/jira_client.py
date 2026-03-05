"""
Jira Service Management Client
Wraps the Jira REST API v3 + Service Desk API for incident polling and assignment.

Auth: Basic auth — Jira account email + API token
Docs: https://developer.atlassian.com/cloud/jira/service-desk/rest/
"""

import logging
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.clients.base_client import BaseITSMClient, ITSMError

logger = logging.getLogger(__name__)


class JiraClient(BaseITSMClient):
    """
    Jira Service Management client.

    Usage::

        client = JiraClient(
            instance_url="https://yourorg.atlassian.net",
            email="admin@yourorg.com",
            api_token="your_api_token",
        )
        incidents = client.get_new_incidents(limit=25)
    """

    def __init__(
        self,
        instance_url: str,
        email: str,
        api_token: str,
        timeout: int = 30,
        max_retries: int = 3,
    ) -> None:
        self.base_url = instance_url.rstrip("/")
        self._timeout = timeout
        self._session = self._build_session(email, api_token, max_retries)

    def _build_session(self, email: str, api_token: str, max_retries: int) -> requests.Session:
        session = requests.Session()
        session.auth = (email, api_token)
        session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        retry = Retry(
            total=max_retries,
            backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "PUT", "POST"],
        )
        session.mount("https://", HTTPAdapter(max_retries=retry))
        return session

    # ─── Normalisation ────────────────────────────────────────────────────────

    def _normalise(self, issue: dict) -> dict:
        """Convert a Jira issue dict to the shared incident schema."""
        fields = issue.get("fields", {})
        return {
            "platform_id": issue.get("key", ""),          # e.g. "HELPDESK-142"
            "number": issue.get("key", ""),
            "short_description": fields.get("summary", ""),
            "description": (
                fields.get("description", {}) or {}
            ).get("content", [{}])[0].get("content", [{}])[0].get("text", "")
            if isinstance(fields.get("description"), dict)
            else str(fields.get("description", "")),
            "priority": (fields.get("priority") or {}).get("name", ""),
        }

    # ─── Public Methods ───────────────────────────────────────────────────────

    def get_new_incidents(self, limit: int = 50) -> list[dict]:
        """
        Fetch unassigned open Jira Service Management issues.
        JQL: type = "Service Request" OR type = "Incident", status = Open, unassigned.
        """
        jql = (
            'issuetype in ("Incident", "Service Request") '
            'AND status in ("Open", "Waiting for support") '
            "AND assignee is EMPTY "
            "ORDER BY created ASC"
        )
        params = {
            "jql": jql,
            "maxResults": limit,
            "fields": "summary,description,priority,status,issuetype,assignee",
        }
        try:
            resp = self._session.get(
                f"{self.base_url}/rest/api/3/search",
                params=params,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            issues = resp.json().get("issues", [])
            logger.info("Fetched %d incidents from Jira", len(issues))
            return [self._normalise(i) for i in issues]
        except requests.exceptions.HTTPError as exc:
            raise ITSMError(f"Jira GET issues failed: {exc}") from exc

    def assign_incident(
        self,
        platform_id: str,
        group_id: str,
        work_notes: str = "",
    ) -> dict:
        """
        Assign a Jira issue to a component (group_id = component name or team ID).
        Also adds a comment if work_notes is provided.
        """
        url = f"{self.base_url}/rest/api/3/issue/{platform_id}"
        payload: dict = {"fields": {"components": [{"name": group_id}]}}
        try:
            resp = self._session.put(url, json=payload, timeout=self._timeout)
            resp.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            raise ITSMError(f"Jira assign issue {platform_id} failed: {exc}") from exc

        if work_notes:
            self._add_comment(platform_id, work_notes)

        logger.info("Assigned Jira issue %s → component=%s", platform_id, group_id)
        return {"key": platform_id, "component": group_id}

    def _add_comment(self, issue_key: str, text: str) -> None:
        """Add a plain-text comment to a Jira issue."""
        url = f"{self.base_url}/rest/api/3/issue/{issue_key}/comment"
        payload = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
            }
        }
        try:
            resp = self._session.post(url, json=payload, timeout=self._timeout)
            resp.raise_for_status()
        except requests.exceptions.HTTPError:
            logger.warning("Failed to add comment to Jira issue %s", issue_key)

    def health_check(self) -> bool:
        try:
            resp = self._session.get(
                f"{self.base_url}/rest/api/3/myself",
                timeout=self._timeout,
            )
            return resp.status_code == 200
        except requests.exceptions.RequestException:
            return False

    @property
    def platform_name(self) -> str:
        return "Jira"

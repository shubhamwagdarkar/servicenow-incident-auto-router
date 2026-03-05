"""
Freshservice REST API Client
Wraps Freshservice v2 API for incident polling and group assignment.

Auth: Basic auth — API key as username, "X" as password
Docs: https://api.freshservice.com/
"""

import logging

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.clients.base_client import BaseITSMClient, ITSMError

logger = logging.getLogger(__name__)

# Freshservice ticket status codes
STATUS_OPEN = 2
STATUS_PENDING = 3

# Ticket type filter
TYPE_INCIDENT = "Incident"


class FreshserviceClient(BaseITSMClient):
    """
    Freshservice incident client.

    Usage::

        client = FreshserviceClient(
            instance_url="https://yourorg.freshservice.com",
            api_key="your_api_key",
        )
        incidents = client.get_new_incidents(limit=25)
    """

    def __init__(
        self,
        instance_url: str,
        api_key: str,
        timeout: int = 30,
        max_retries: int = 3,
    ) -> None:
        self.base_url = instance_url.rstrip("/")
        self._timeout = timeout
        self._session = self._build_session(api_key, max_retries)

    def _build_session(self, api_key: str, max_retries: int) -> requests.Session:
        # Freshservice uses API key as username, "X" as password
        session = requests.Session()
        session.auth = (api_key, "X")
        session.headers.update({"Content-Type": "application/json"})
        retry = Retry(
            total=max_retries,
            backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "PUT"],
        )
        session.mount("https://", HTTPAdapter(max_retries=retry))
        return session

    # ─── Normalisation ────────────────────────────────────────────────────────

    def _normalise(self, ticket: dict) -> dict:
        """Convert a Freshservice ticket dict to the shared incident schema."""
        return {
            "platform_id": str(ticket.get("id", "")),
            "number": f"FS-{ticket.get('id', '')}",
            "short_description": ticket.get("subject", ""),
            "description": ticket.get("description_text", ""),
            "priority": str(ticket.get("priority", "")),
        }

    # ─── Public Methods ───────────────────────────────────────────────────────

    def get_new_incidents(self, limit: int = 50) -> list[dict]:
        """
        Fetch open Freshservice incident tickets with no group assigned.
        """
        params = {
            "type": TYPE_INCIDENT,
            "status": STATUS_OPEN,
            "per_page": min(limit, 100),  # Freshservice max is 100
            "order_type": "asc",
            "order_by": "created_at",
        }
        url = f"{self.base_url}/api/v2/tickets"
        try:
            resp = self._session.get(url, params=params, timeout=self._timeout)
            resp.raise_for_status()
            tickets = resp.json().get("tickets", [])
            # Filter: no group assigned
            unassigned = [t for t in tickets if not t.get("group_id")]
            logger.info(
                "Fetched %d open tickets, %d unassigned from Freshservice",
                len(tickets),
                len(unassigned),
            )
            return [self._normalise(t) for t in unassigned[:limit]]
        except requests.exceptions.HTTPError as exc:
            raise ITSMError(f"Freshservice GET tickets failed: {exc}") from exc

    def assign_incident(
        self,
        platform_id: str,
        group_id: str,
        work_notes: str = "",
    ) -> dict:
        """
        Assign a Freshservice ticket to a group.
        group_id = numeric Freshservice group_id as string (e.g. "12001").
        """
        url = f"{self.base_url}/api/v2/tickets/{platform_id}"
        payload: dict = {"group_id": int(group_id)}
        try:
            resp = self._session.put(url, json=payload, timeout=self._timeout)
            resp.raise_for_status()
            result = resp.json().get("ticket", {})
        except requests.exceptions.HTTPError as exc:
            raise ITSMError(
                f"Freshservice assign ticket {platform_id} failed: {exc}"
            ) from exc

        if work_notes:
            self._add_note(platform_id, work_notes)

        logger.info(
            "Assigned Freshservice ticket %s → group_id=%s", platform_id, group_id
        )
        return result

    def _add_note(self, ticket_id: str, text: str) -> None:
        """Add a private note to a Freshservice ticket."""
        url = f"{self.base_url}/api/v2/tickets/{ticket_id}/notes"
        payload = {"body": text, "private": True}
        try:
            resp = self._session.post(url, json=payload, timeout=self._timeout)
            resp.raise_for_status()
        except requests.exceptions.HTTPError:
            logger.warning("Failed to add note to Freshservice ticket %s", ticket_id)

    def health_check(self) -> bool:
        try:
            resp = self._session.get(
                f"{self.base_url}/api/v2/tickets?per_page=1",
                timeout=self._timeout,
            )
            return resp.status_code == 200
        except requests.exceptions.RequestException:
            return False

    @property
    def platform_name(self) -> str:
        return "Freshservice"

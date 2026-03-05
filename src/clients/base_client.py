"""
BaseITSMClient — Abstract interface that all ITSM platform clients must implement.

Every client must normalise its platform's incident format to the shared dict schema:

    {
        "platform_id":        str,   # Native ID used to update the record
        "number":             str,   # Human-readable ticket number (INC0001, PD-123, etc.)
        "short_description":  str,   # One-line summary
        "description":        str,   # Full incident body
        "priority":           str,   # Platform-native priority string or number
    }

This schema is the only contract the classifier and router ever see — they remain
completely platform-agnostic.
"""

from abc import ABC, abstractmethod


class ITSMError(Exception):
    """Raised when an ITSM platform API returns an unexpected response."""


class BaseITSMClient(ABC):
    """Abstract base class for all ITSM platform clients."""

    @abstractmethod
    def get_new_incidents(self, limit: int = 50) -> list[dict]:
        """
        Fetch unassigned / open incidents from the platform.

        Returns a list of normalised incident dicts (see module docstring).
        """

    @abstractmethod
    def assign_incident(
        self,
        platform_id: str,
        group_id: str,
        work_notes: str = "",
    ) -> dict:
        """
        Assign an incident to the specified group.

        Parameters
        ----------
        platform_id : str
            The platform's native record identifier.
        group_id : str
            The platform-specific group/team/policy identifier from routing_rules.yaml.
        work_notes : str
            Optional note to append to the incident.

        Returns the updated record dict (best-effort — some platforms return empty).
        """

    @abstractmethod
    def health_check(self) -> bool:
        """Return True if the platform is reachable and credentials are valid."""

    @property
    def platform_name(self) -> str:
        """Human-readable platform name for logging and audit records."""
        return self.__class__.__name__.replace("Client", "")

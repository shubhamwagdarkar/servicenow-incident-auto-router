"""
PostgreSQL Audit Logger

Persists every RoutingDecision to a `routing_audit` table so that routing
history is queryable, reportable, and never lost if the process restarts.

Schema (auto-created on first run):

    routing_audit (
        id               SERIAL PRIMARY KEY,
        incident_sys_id  TEXT        NOT NULL,
        incident_number  TEXT        NOT NULL,
        short_description TEXT,
        assigned_group   TEXT,
        group_sys_id     TEXT,
        classification_method TEXT,
        confidence       NUMERIC(5,4),
        matched_keywords TEXT[],
        is_critical      BOOLEAN,
        success          BOOLEAN,
        error_message    TEXT,
        routed_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
"""

import logging
from contextlib import contextmanager
from typing import Generator, Optional

import psycopg2
import psycopg2.extras
from psycopg2.extensions import connection as PgConnection

from src.router import RoutingDecision

logger = logging.getLogger(__name__)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS routing_audit (
    id                   SERIAL PRIMARY KEY,
    incident_sys_id      TEXT            NOT NULL,
    incident_number      TEXT            NOT NULL,
    short_description    TEXT,
    assigned_group       TEXT,
    group_sys_id         TEXT,
    classification_method TEXT,
    confidence           NUMERIC(5, 4),
    matched_keywords     TEXT[],
    is_critical          BOOLEAN         DEFAULT FALSE,
    success              BOOLEAN         DEFAULT TRUE,
    error_message        TEXT,
    routed_at            TIMESTAMPTZ     NOT NULL,
    created_at           TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_routing_audit_number
    ON routing_audit (incident_number);

CREATE INDEX IF NOT EXISTS idx_routing_audit_routed_at
    ON routing_audit (routed_at DESC);
"""

_INSERT_SQL = """
INSERT INTO routing_audit (
    incident_sys_id,
    incident_number,
    short_description,
    assigned_group,
    group_sys_id,
    classification_method,
    confidence,
    matched_keywords,
    is_critical,
    success,
    error_message,
    routed_at
) VALUES (
    %(incident_sys_id)s,
    %(incident_number)s,
    %(short_description)s,
    %(assigned_group)s,
    %(group_sys_id)s,
    %(classification_method)s,
    %(confidence)s,
    %(matched_keywords)s,
    %(is_critical)s,
    %(success)s,
    %(error_message)s,
    %(routed_at)s
)
RETURNING id;
"""


class AuditLogger:
    """
    Wraps a PostgreSQL connection and exposes audit-write methods.

    Parameters
    ----------
    dsn : str
        PostgreSQL connection string, e.g.
        "postgresql://user:pass@localhost:5432/autorouter"
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn: Optional[PgConnection] = None
        self._ensure_connection()
        self._ensure_schema()

    # ─── Connection Management ────────────────────────────────────────────────

    def _ensure_connection(self) -> None:
        """Open a new connection if one doesn't exist or has been lost."""
        if self._conn is None or self._conn.closed:
            logger.debug("Opening PostgreSQL connection")
            self._conn = psycopg2.connect(self._dsn)
            self._conn.autocommit = False

    def _ensure_schema(self) -> None:
        """Create the audit table if it doesn't exist."""
        self._ensure_connection()
        with self._cursor() as cur:
            cur.execute(_CREATE_TABLE_SQL)
        self._conn.commit()  # type: ignore[union-attr]
        logger.info("Audit schema verified / created")

    @contextmanager
    def _cursor(self) -> Generator:
        self._ensure_connection()
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)  # type: ignore
        try:
            yield cur
        except Exception:
            self._conn.rollback()  # type: ignore[union-attr]
            raise
        finally:
            cur.close()

    def close(self) -> None:
        """Close the database connection gracefully."""
        if self._conn and not self._conn.closed:
            self._conn.close()
            logger.debug("PostgreSQL connection closed")

    # ─── Write ────────────────────────────────────────────────────────────────

    def log_decision(self, decision: RoutingDecision) -> int:
        """
        Persist a single RoutingDecision.

        Returns the new row id.
        """
        params = {
            "incident_sys_id": decision.incident_sys_id,
            "incident_number": decision.incident_number,
            "short_description": decision.short_description[:500] if decision.short_description else None,
            "assigned_group": decision.assigned_group_name,
            "group_sys_id": decision.assigned_group_sys_id,
            "classification_method": decision.classification_method,
            "confidence": decision.confidence,
            "matched_keywords": decision.matched_keywords or [],
            "is_critical": decision.is_critical,
            "success": decision.success,
            "error_message": decision.error,
            "routed_at": decision.routed_at,
        }
        with self._cursor() as cur:
            cur.execute(_INSERT_SQL, params)
            row_id = cur.fetchone()["id"]  # type: ignore[index]
        self._conn.commit()  # type: ignore[union-attr]
        logger.debug(
            "Audit row %d written for incident %s",
            row_id,
            decision.incident_number,
        )
        return row_id

    def log_batch(self, decisions: list[RoutingDecision]) -> list[int]:
        """Persist a batch of decisions; returns list of inserted row ids."""
        ids = []
        for decision in decisions:
            row_id = self.log_decision(decision)
            ids.append(row_id)
        logger.info("Logged %d routing decisions to audit table", len(ids))
        return ids

    # ─── Read ─────────────────────────────────────────────────────────────────

    def get_recent(self, limit: int = 50) -> list[dict]:
        """Return the most recent `limit` audit rows as dicts."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM routing_audit ORDER BY routed_at DESC LIMIT %s",
                (limit,),
            )
            return [dict(row) for row in cur.fetchall()]

    def get_stats(self) -> dict:
        """
        Return aggregate routing statistics.

        Returns a dict with keys:
            total, succeeded, failed, by_method, by_group
        """
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*)                              AS total,
                    SUM(CASE WHEN success THEN 1 END)    AS succeeded,
                    SUM(CASE WHEN NOT success THEN 1 END) AS failed
                FROM routing_audit
                """
            )
            totals = dict(cur.fetchone())  # type: ignore[arg-type]

            cur.execute(
                """
                SELECT classification_method, COUNT(*) AS cnt
                FROM routing_audit
                GROUP BY classification_method
                ORDER BY cnt DESC
                """
            )
            totals["by_method"] = {row["classification_method"]: row["cnt"] for row in cur.fetchall()}

            cur.execute(
                """
                SELECT assigned_group, COUNT(*) AS cnt
                FROM routing_audit
                GROUP BY assigned_group
                ORDER BY cnt DESC
                """
            )
            totals["by_group"] = {row["assigned_group"]: row["cnt"] for row in cur.fetchall()}

        return totals

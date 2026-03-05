"""
ServiceNow Incident Auto-Router
================================
Polls ServiceNow for unassigned incidents, classifies them using a two-stage
keyword + ML classifier, auto-assigns them to the correct team via REST API,
and logs every routing decision to PostgreSQL for audit and reporting.

Usage:
    python main.py                  # Run once
    python main.py --schedule       # Poll on a schedule (default: 60s interval)
    python main.py --dry-run        # Classify but don't update ServiceNow
    python main.py --stats          # Print audit stats and exit
    python main.py --help           # Show this help
"""

import argparse
import logging
import sys
import time
from pathlib import Path

import schedule
import yaml
from dotenv import load_dotenv

from src.audit import AuditLogger
from src.classifier import IncidentClassifier
from src.router import IncidentRouter
from src.snow_client import ServiceNowClient

# ─── Environment & Logging ────────────────────────────────────────────────────

load_dotenv()

import os

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("auto_router")

# ─── Config ───────────────────────────────────────────────────────────────────

RULES_PATH = Path("config/routing_rules.yaml")
MODEL_PATH = Path("model/classifier.joblib")


def load_routing_rules() -> dict:
    with open(RULES_PATH, "r") as f:
        return yaml.safe_load(f)


def get_env(key: str, required: bool = True) -> str:
    value = os.getenv(key, "")
    if required and not value:
        logger.error("Missing required environment variable: %s", key)
        sys.exit(1)
    return value


# ─── Core Run Logic ───────────────────────────────────────────────────────────

def build_components(rules: dict, dry_run: bool):
    """Construct and return all runtime components."""

    snow_client = ServiceNowClient(
        instance_url=get_env("SNOW_INSTANCE_URL"),
        username=get_env("SNOW_USERNAME"),
        password=get_env("SNOW_PASSWORD"),
    )

    model_path = str(MODEL_PATH) if MODEL_PATH.exists() else None
    classifier = IncidentClassifier(routing_rules=rules, model_path=model_path)

    router = IncidentRouter(
        snow_client=snow_client,
        classifier=classifier,
        routing_rules=rules,
        dry_run=dry_run,
    )

    audit_logger: AuditLogger | None = None
    db_url = os.getenv("DATABASE_URL", "")
    if db_url:
        audit_logger = AuditLogger(dsn=db_url)
    else:
        logger.warning(
            "DATABASE_URL not set — routing decisions will NOT be persisted"
        )

    return snow_client, classifier, router, audit_logger


def run_once(
    snow_client: ServiceNowClient,
    router: IncidentRouter,
    audit_logger: "AuditLogger | None",
    rules: dict,
) -> None:
    """Fetch, classify, assign, and log one batch of new incidents."""

    polling_cfg = rules.get("polling", {})
    limit = int(polling_cfg.get("max_incidents_per_run", 50))
    states = polling_cfg.get("target_states", [1])

    logger.info("─── Polling ServiceNow for new incidents ───")

    # Health check (non-fatal — might be a test/demo instance)
    if not snow_client.health_check():
        logger.warning(
            "ServiceNow health check failed — proceeding anyway (check credentials)"
        )

    incidents = snow_client.get_new_incidents(limit=limit, states=states)

    if not incidents:
        logger.info("No new unassigned incidents found")
        return

    decisions = router.route_batch(incidents)

    if audit_logger:
        try:
            audit_logger.log_batch(decisions)
        except Exception as exc:
            logger.error("Audit logging failed: %s", exc)

    # ── Summary ───────────────────────────────────────────────────────────────
    succeeded = [d for d in decisions if d.success]
    failed = [d for d in decisions if not d.success]

    logger.info(
        "Run complete │ Total: %d │ Routed: %d │ Failed: %d",
        len(decisions),
        len(succeeded),
        len(failed),
    )
    for decision in succeeded:
        logger.info(
            "  ✓ %s → %-30s [%s | conf=%.0f%%]%s",
            decision.incident_number,
            decision.assigned_group_name,
            decision.classification_method,
            decision.confidence * 100,
            " ⚠ CRITICAL" if decision.is_critical else "",
        )
    for decision in failed:
        logger.error(
            "  ✗ %s FAILED: %s", decision.incident_number, decision.error
        )


def print_stats(audit_logger: "AuditLogger") -> None:
    stats = audit_logger.get_stats()
    print("\n─── Routing Audit Statistics ───────────────────────")
    print(f"  Total routed : {stats.get('total', 0)}")
    print(f"  Succeeded    : {stats.get('succeeded', 0)}")
    print(f"  Failed       : {stats.get('failed', 0)}")
    print("\n  By Classification Method:")
    for method, count in (stats.get("by_method") or {}).items():
        print(f"    {method:<12} : {count}")
    print("\n  By Assignment Group:")
    for group, count in (stats.get("by_group") or {}).items():
        print(f"    {group:<30} : {count}")
    print("────────────────────────────────────────────────────\n")


# ─── CLI ─────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ServiceNow Incident Auto-Router",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--schedule",
        action="store_true",
        help="Run on a recurring schedule (interval from routing_rules.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify but do NOT update ServiceNow (safe for testing)",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print audit statistics and exit (requires DATABASE_URL)",
    )
    parser.add_argument(
        "--save-model",
        action="store_true",
        help="Train and save the ML model to disk, then exit",
    )
    return parser.parse_args()


# ─── Entrypoint ───────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()
    rules = load_routing_rules()

    snow_client, classifier, router, audit_logger = build_components(
        rules=rules, dry_run=args.dry_run
    )

    # ── Save model and exit ────────────────────────────────────────────────────
    if args.save_model:
        MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        classifier.save_model(str(MODEL_PATH))
        logger.info("Model saved to %s — exiting", MODEL_PATH)
        return

    # ── Stats mode ────────────────────────────────────────────────────────────
    if args.stats:
        if audit_logger is None:
            logger.error("DATABASE_URL required for --stats")
            sys.exit(1)
        print_stats(audit_logger)
        return

    # ── Closure for scheduler / immediate run ─────────────────────────────────
    def job() -> None:
        run_once(snow_client, router, audit_logger, rules)

    # ── Scheduled mode ────────────────────────────────────────────────────────
    if args.schedule:
        interval = int(
            rules.get("polling", {}).get("interval_seconds", 60)
        )
        logger.info("Scheduled mode: polling every %d seconds", interval)
        job()  # Run immediately on start
        schedule.every(interval).seconds.do(job)
        try:
            while True:
                schedule.run_pending()
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Scheduler stopped by user")
        finally:
            if audit_logger:
                audit_logger.close()
        return

    # ── Single run ────────────────────────────────────────────────────────────
    try:
        job()
    finally:
        if audit_logger:
            audit_logger.close()


if __name__ == "__main__":
    main()

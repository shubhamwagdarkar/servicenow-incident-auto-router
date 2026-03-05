"""
Enterprise Incident Auto-Router
=================================
Polls your ITSM platform for unassigned incidents, classifies them using a
two-stage keyword + ML engine, auto-assigns them to the correct team, and
logs every routing decision to PostgreSQL for full audit traceability.

Supported platforms: servicenow · jira · pagerduty · ivanti · freshservice

Usage:
    python main.py --platform servicenow            # Run once (ServiceNow)
    python main.py --platform jira --schedule       # Poll Jira every 60s
    python main.py --platform pagerduty --dry-run   # Classify PD, no writes
    python main.py --platform freshservice --stats  # Audit stats for Freshservice
    python main.py --save-model                     # Train + save ML model
    python main.py --help
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import schedule
import yaml
from dotenv import load_dotenv

from src.audit import AuditLogger
from src.classifier import IncidentClassifier
from src.clients import (
    BaseITSMClient,
    FreshserviceClient,
    IvantiClient,
    JiraClient,
    PagerDutyClient,
    ServiceNowClient,
)
from src.router import IncidentRouter

# ─── Environment & Logging ────────────────────────────────────────────────────

load_dotenv()

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

SUPPORTED_PLATFORMS = ["servicenow", "jira", "pagerduty", "ivanti", "freshservice"]


def load_routing_rules() -> dict:
    with open(RULES_PATH, "r") as f:
        return yaml.safe_load(f)


def get_env(key: str, required: bool = True) -> str:
    value = os.getenv(key, "")
    if required and not value:
        logger.error("Missing required environment variable: %s", key)
        sys.exit(1)
    return value


# ─── Client Factory ───────────────────────────────────────────────────────────

def build_client(platform: str) -> BaseITSMClient:
    """Instantiate the correct ITSM client from environment variables."""

    if platform == "servicenow":
        return ServiceNowClient(
            instance_url=get_env("SNOW_INSTANCE_URL"),
            username=get_env("SNOW_USERNAME"),
            password=get_env("SNOW_PASSWORD"),
        )

    if platform == "jira":
        return JiraClient(
            instance_url=get_env("JIRA_INSTANCE_URL"),
            email=get_env("JIRA_EMAIL"),
            api_token=get_env("JIRA_API_TOKEN"),
        )

    if platform == "pagerduty":
        return PagerDutyClient(
            api_key=get_env("PAGERDUTY_API_KEY"),
            from_email=get_env("PAGERDUTY_FROM_EMAIL"),
        )

    if platform == "ivanti":
        return IvantiClient(
            instance_url=get_env("IVANTI_INSTANCE_URL"),
            api_token=get_env("IVANTI_API_TOKEN"),
        )

    if platform == "freshservice":
        return FreshserviceClient(
            instance_url=get_env("FRESHSERVICE_INSTANCE_URL"),
            api_key=get_env("FRESHSERVICE_API_KEY"),
        )

    logger.error("Unknown platform: %s. Choose from: %s", platform, SUPPORTED_PLATFORMS)
    sys.exit(1)


# ─── Component Builder ────────────────────────────────────────────────────────

def build_components(platform: str, rules: dict, dry_run: bool):
    """Construct and return all runtime components."""
    client = build_client(platform)

    model_path = str(MODEL_PATH) if MODEL_PATH.exists() else None
    classifier = IncidentClassifier(routing_rules=rules, model_path=model_path)

    router = IncidentRouter(
        client=client,
        classifier=classifier,
        routing_rules=rules,
        dry_run=dry_run,
    )

    audit_logger = None
    db_url = os.getenv("DATABASE_URL", "")
    if db_url:
        audit_logger = AuditLogger(dsn=db_url)
    else:
        logger.warning("DATABASE_URL not set — routing decisions will NOT be persisted")

    return client, classifier, router, audit_logger


# ─── Core Run Logic ───────────────────────────────────────────────────────────

def run_once(
    client: BaseITSMClient,
    router: IncidentRouter,
    audit_logger: "AuditLogger | None",
    rules: dict,
) -> None:
    """Fetch, classify, assign, and log one batch of new incidents."""
    polling_cfg = rules.get("polling", {})
    limit = int(polling_cfg.get("max_incidents_per_run", 50))
    states = polling_cfg.get("target_states", [1])

    platform = client.platform_name
    logger.info("─── Polling %s for new incidents ───", platform)

    if not client.health_check():
        logger.warning("%s health check failed — proceeding anyway", platform)

    incidents = client.get_new_incidents(limit=limit)

    if not incidents:
        logger.info("No new unassigned incidents found on %s", platform)
        return

    decisions = router.route_batch(incidents)

    if audit_logger:
        try:
            audit_logger.log_batch(decisions)
        except Exception as exc:
            logger.error("Audit logging failed: %s", exc)

    succeeded = [d for d in decisions if d.success]
    failed = [d for d in decisions if not d.success]

    logger.info(
        "Run complete │ Platform: %s │ Total: %d │ Routed: %d │ Failed: %d",
        platform, len(decisions), len(succeeded), len(failed),
    )
    for d in succeeded:
        logger.info(
            "  ✓ %s → %-30s [%s | conf=%.0f%%]%s",
            d.incident_number, d.assigned_group_name, d.classification_method,
            d.confidence * 100, " ⚠ CRITICAL" if d.is_critical else "",
        )
    for d in failed:
        logger.error("  ✗ %s FAILED: %s", d.incident_number, d.error)


def print_stats(audit_logger: "AuditLogger") -> None:
    stats = audit_logger.get_stats()
    print("\n─── Routing Audit Statistics ───────────────────────")
    print(f"  Total routed : {stats.get('total', 0)}")
    print(f"  Succeeded    : {stats.get('succeeded', 0)}")
    print(f"  Failed       : {stats.get('failed', 0)}")
    print("\n  By Platform:")
    for platform, count in (stats.get("by_platform") or {}).items():
        print(f"    {platform:<16} : {count}")
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
        description="Enterprise Incident Auto-Router",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--platform",
        choices=SUPPORTED_PLATFORMS,
        default="servicenow",
        help="ITSM platform to connect to (default: servicenow)",
    )
    parser.add_argument(
        "--schedule",
        action="store_true",
        help="Run on a recurring schedule (interval from routing_rules.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify but do NOT update the platform (safe for testing)",
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

    client, classifier, router, audit_logger = build_components(
        platform=args.platform,
        rules=rules,
        dry_run=args.dry_run,
    )

    if args.save_model:
        MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        classifier.save_model(str(MODEL_PATH))
        logger.info("Model saved to %s — exiting", MODEL_PATH)
        return

    if args.stats:
        if audit_logger is None:
            logger.error("DATABASE_URL required for --stats")
            sys.exit(1)
        print_stats(audit_logger)
        return

    def job() -> None:
        run_once(client, router, audit_logger, rules)

    if args.schedule:
        interval = int(rules.get("polling", {}).get("interval_seconds", 60))
        logger.info(
            "Scheduled mode: polling %s every %d seconds", args.platform, interval
        )
        job()
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

    try:
        job()
    finally:
        if audit_logger:
            audit_logger.close()


if __name__ == "__main__":
    main()

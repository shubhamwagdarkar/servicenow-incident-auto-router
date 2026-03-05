# enterprise-incident-auto-router

> Multi-platform ITSM incident classifier and auto-router.
> Supports ServiceNow · Jira · PagerDuty · Ivanti · Freshservice

Built by **Shubham Wagdarkar** · Content Architect @ Resolve Systems

Polls your ITSM platform for unassigned incidents, classifies them using a **two-stage keyword + ML engine**, auto-assigns them to the correct team via REST API, and logs every routing decision to PostgreSQL for full audit traceability.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                       enterprise-incident-auto-router                        │
│                                                                              │
│   ┌─────────────┐  poll every N sec  ┌────────────────────────────────────┐ │
│   │   schedule  │───────────────────▶│   BaseITSMClient (abstract)        │ │
│   └─────────────┘                    │                                    │ │
│                                      │  ┌────────────┐ ┌───────────────┐  │ │
│                                      │  │ ServiceNow │ │     Jira      │  │ │
│                                      │  └────────────┘ └───────────────┘  │ │
│                                      │  ┌────────────┐ ┌───────────────┐  │ │
│                                      │  │ PagerDuty  │ │    Ivanti     │  │ │
│                                      │  └────────────┘ └───────────────┘  │ │
│                                      │  ┌──────────────┐                  │ │
│                                      │  │ Freshservice  │                  │ │
│                                      │  └──────────────┘                  │ │
│                                      └────────────────┬───────────────────┘ │
│                                                       │ normalised incidents │
│                                                       ▼                      │
│                                      ┌────────────────────────────────────┐ │
│                                      │   IncidentClassifier               │ │
│                                      │                                    │ │
│                                      │  Stage 1: Keyword Match            │ │
│                                      │  ┌──────────────────────────────┐  │ │
│                                      │  │ routing_rules.yaml           │  │ │
│                                      │  │ • network keywords           │  │ │
│                                      │  │ • security keywords          │  │ │
│                                      │  │ • database keywords          │  │ │
│                                      │  │ • app / infra keywords       │  │ │
│                                      │  └──────────────────────────────┘  │ │
│                                      │         │ no match                  │ │
│                                      │         ▼                           │ │
│                                      │  Stage 2: ML Pipeline              │ │
│                                      │  ┌──────────────────────────────┐  │ │
│                                      │  │  TF-IDF + Logistic Regr.     │  │ │
│                                      │  │  (sklearn Pipeline)          │  │ │
│                                      │  └──────────────────────────────┘  │ │
│                                      │         │ conf < 0.72              │ │
│                                      │         ▼                           │ │
│                                      │  Stage 3: Fallback → Service Desk  │ │
│                                      └────────────────┬───────────────────┘ │
│                                                       │ ClassificationResult │
│                                                       ▼                      │
│                                      ┌────────────────────────────────────┐ │
│                                      │   IncidentRouter                   │ │
│                                      │   • Resolve platform group ID      │ │
│                                      │   • Build work_notes               │ │
│                                      │   • Critical keyword escalation    │ │
│                                      └──────────┬─────────────────────────┘ │
│                           ┌───────────────────┐ │                            │
│                           │                   │ │ RoutingDecision            │
│                           ▼                   ▼ ▼                            │
│               ┌──────────────────┐  ┌──────────────────────────┐            │
│               │  AuditLogger     │  │  Platform API (assign)   │            │
│               │  PostgreSQL      │  │  + work_notes / comment  │            │
│               │  routing_audit   │  └──────────────────────────┘            │
│               └──────────────────┘                                           │
└──────────────────────────────────────────────────────────────────────────────┘
```

### Data Flow

```
ITSM Platform (unassigned incidents)
        │
        │  GET incidents (platform-specific query)
        ▼
  Normalised Incident Dict
  { platform_id, number, short_description, description, priority }
        │
        ▼
  IncidentClassifier.classify(short_desc, description)
        │
        ├─ Keyword match found? ──YES──▶ group_key, conf ≥ 0.75, method="keyword"
        │
        └─ No keyword match ──────────▶ TF-IDF + LogReg predict_proba()
                                              │
                                              ├─ conf ≥ 0.72? ──YES──▶ method="ml"
                                              │
                                              └─ conf < 0.72  ──────▶ method="fallback"
        │
        ▼
  RoutingDecision
  { group_key, group_platform_id, platform, confidence, method, is_critical }
        │
        ├──▶  Platform API assign (group_platform_id + work_notes)
        │
        └──▶  INSERT routing_audit row (PostgreSQL)
```

---

## Supported Platforms

| Platform | Auth | Group ID Type | Assign Method |
|---|---|---|---|
| ServiceNow | Basic auth | sys_id string | PATCH assignment_group |
| Jira Service Management | Email + API token | Component name | PUT fields.components |
| PagerDuty | API key header | Escalation policy ID | PUT escalation_policy |
| Ivanti Neurons | API token header | Team name string | PATCH Team field |
| Freshservice | API key + "X" | Numeric group_id | PUT group_id |

---

## Assignment Groups

| Group Key | Team | Example Keywords |
|---|---|---|
| `network` | Network Operations | vpn, firewall, dns, latency, packet loss |
| `security` | Security Operations | malware, phishing, breach, vulnerability |
| `database` | Database Administration | postgresql, deadlock, replication, query |
| `application` | Application Operations | pod, kubernetes, container, deploy, api |
| `infrastructure` | Infrastructure Engineering | server, vm, disk, cpu, hypervisor, azure |
| `service_desk` | IT Service Desk (fallback) | password reset, printer, outlook |

---

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/shubhamwagdarkar/enterprise-incident-auto-router.git
cd enterprise-incident-auto-router

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Fill in credentials for the platform(s) you use
```

### 3. Run

```bash
# Single run — pick your platform
python main.py --platform servicenow
python main.py --platform jira
python main.py --platform pagerduty
python main.py --platform ivanti
python main.py --platform freshservice

# Dry-run — classify only, no platform writes (safe for testing)
python main.py --platform servicenow --dry-run

# Scheduled mode — polls every 60s (configurable in routing_rules.yaml)
python main.py --platform jira --schedule

# Save trained ML model to disk
python main.py --save-model

# Print audit statistics
python main.py --stats
```

### 4. Run tests

```bash
pytest tests/ -v
# 36 tests, all passing
```

---

## Project Structure

```
enterprise-incident-auto-router/
├── main.py                              # CLI entrypoint, client factory, scheduler
├── src/
│   ├── clients/
│   │   ├── base_client.py              # Abstract BaseITSMClient + ITSMError
│   │   ├── snow_client.py              # ServiceNow Table REST API
│   │   ├── jira_client.py              # Jira Service Management REST API
│   │   ├── pagerduty_client.py         # PagerDuty REST API v2
│   │   ├── ivanti_client.py            # Ivanti Neurons OData REST API
│   │   └── freshservice_client.py      # Freshservice REST API v2
│   ├── classifier.py                   # Two-stage keyword + ML classifier
│   ├── router.py                       # Orchestrates classify → assign → log
│   └── audit.py                        # PostgreSQL audit logger
├── config/
│   └── routing_rules.yaml              # Keywords, platform group IDs, ML threshold
├── tests/
│   ├── clients/
│   │   ├── test_jira_client.py         # Jira client unit tests
│   │   └── test_freshservice_client.py # Freshservice client unit tests
│   ├── test_classifier.py              # Classification logic unit tests
│   └── test_router.py                  # Routing + dry-run + multi-platform tests
├── model/                              # (generated) saved sklearn Pipeline
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

---

## Configuration — `config/routing_rules.yaml`

```yaml
ml_confidence_threshold: 0.72        # Below this → fallback to service_desk

polling:
  interval_seconds: 60
  max_incidents_per_run: 50
  target_states: [1]                 # ServiceNow: 1 = New

assignment_groups:
  network:
    display_name: "Network Operations"
    platform_ids:
      servicenow:   "netops-group-001"   # ServiceNow sys_id
      jira:         "Network"            # Jira component name
      pagerduty:    "PD_NETOPS_EP_001"  # Escalation policy ID
      ivanti:       "Network Operations" # Team name
      freshservice: "12001"              # Group ID
    keywords: [vpn, firewall, dns, ...]

priority_escalation:
  critical_keywords: [outage, down, ransomware, ...]
```

---

## Environment Variables

| Variable | Platform | Required |
|---|---|---|
| `SNOW_INSTANCE_URL` | ServiceNow | If using ServiceNow |
| `SNOW_USERNAME` | ServiceNow | If using ServiceNow |
| `SNOW_PASSWORD` | ServiceNow | If using ServiceNow |
| `JIRA_INSTANCE_URL` | Jira | If using Jira |
| `JIRA_EMAIL` | Jira | If using Jira |
| `JIRA_API_TOKEN` | Jira | If using Jira |
| `PAGERDUTY_API_KEY` | PagerDuty | If using PagerDuty |
| `PAGERDUTY_FROM_EMAIL` | PagerDuty | If using PagerDuty |
| `IVANTI_INSTANCE_URL` | Ivanti | If using Ivanti |
| `IVANTI_API_TOKEN` | Ivanti | If using Ivanti |
| `FRESHSERVICE_INSTANCE_URL` | Freshservice | If using Freshservice |
| `FRESHSERVICE_API_KEY` | Freshservice | If using Freshservice |
| `DATABASE_URL` | All | No — disables audit if unset |
| `LOG_LEVEL` | All | No — default: INFO |

---

## PostgreSQL Audit Schema

```sql
CREATE TABLE routing_audit (
    id                   SERIAL PRIMARY KEY,
    incident_platform_id TEXT,               -- Native platform record ID
    incident_number      TEXT,               -- e.g. INC0001234, HELPDESK-42, FS-99
    short_description    TEXT,
    assigned_group       TEXT,               -- "Network Operations"
    group_platform_id    TEXT,               -- Platform-native group identifier
    platform             TEXT,               -- "ServiceNow" | "Jira" | "PagerDuty" | etc.
    classification_method TEXT,              -- "keyword" | "ml" | "fallback"
    confidence           NUMERIC(5,4),
    matched_keywords     TEXT[],
    is_critical          BOOLEAN,
    success              BOOLEAN,
    error_message        TEXT,
    routed_at            TIMESTAMPTZ,
    created_at           TIMESTAMPTZ DEFAULT NOW()
);
```

Query examples:
```sql
-- Breakdown by platform
SELECT platform, COUNT(*) FROM routing_audit GROUP BY platform;

-- Routing method accuracy
SELECT classification_method, COUNT(*), ROUND(AVG(confidence)::NUMERIC, 2) AS avg_conf
FROM routing_audit GROUP BY classification_method;

-- Top 5 busiest teams this week
SELECT assigned_group, COUNT(*) AS total
FROM routing_audit
WHERE routed_at >= NOW() - INTERVAL '7 days'
GROUP BY assigned_group ORDER BY total DESC LIMIT 5;

-- Failed routing attempts
SELECT platform, incident_number, error_message, routed_at
FROM routing_audit WHERE success = FALSE ORDER BY routed_at DESC;
```

---

## Classification Engine

### Stage 1: Keyword Match (fast path)
- Normalises incident text (lowercase, strip punctuation)
- Scans all group keyword lists — **O(n·k)** where n=groups, k=keywords
- Group with most keyword hits wins
- Confidence: `min(0.99, 0.75 + hit_ratio × 20)`

### Stage 2: ML Pipeline (sklearn)
- **TF-IDF Vectorizer** — `ngram_range=(1,3)`, `sublinear_tf=True`
- **Logistic Regression** — `C=5.0`, `class_weight="balanced"`
- Bootstrapped from keyword corpus — no labeled data needed on Day 1
- Falls back if `predict_proba()` confidence < `ml_confidence_threshold`

### Stage 3: Fallback
- Routes to `service_desk` group (configurable)
- Guarantees every incident gets assigned — 100% routing rate

---

## What I Learned

1. **Two-stage classification beats pure ML for enterprise incident routing** — keyword rules handle 70%+ of volume with near-perfect accuracy, freeing ML for the edge cases that actually need it.

2. **Abstract base clients make multi-platform support clean** — the classifier and router never need to know which ITSM is active. Swapping platforms is a one-flag change.

3. **Dry-run mode is non-negotiable** when writing back to production ITSM systems. Every automation touching live data needs a safe test path.

4. **Audit logging is architectural, not optional** — without `routing_audit`, you have no visibility into routing quality across platforms and no data for retraining.

---

## LinkedIn Post

```
Week 1/42 — shipped: enterprise-incident-auto-router

At Resolve Systems, misrouted incidents are a constant pain point — they add
15-30 min of delay to P1s and burn time for teams that shouldn't own them.

So I built a multi-platform auto-router that works with ServiceNow, Jira,
PagerDuty, Ivanti, and Freshservice:

- Polls your ITSM for unassigned incidents
- Classifies them: keyword matching → scikit-learn ML fallback
- Auto-assigns to the right team via REST API
- Logs every decision to PostgreSQL for audit

Stack: Python + requests + scikit-learn + psycopg2 + schedule
Build time: ~6 hours

github.com/shubhamwagdarkar/enterprise-incident-auto-router

Key insight: one abstract client interface = 5 platforms, zero changes to
the classification or routing logic.

#EnterpriseAutomation #AIOps #ServiceNow #Jira #PagerDuty #Python #OpenSource
```

---

## License

MIT

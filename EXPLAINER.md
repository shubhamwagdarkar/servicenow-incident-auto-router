# Project Explainer — enterprise-incident-auto-router

> How to explain this project to anyone — from a non-technical recruiter to a Staff Engineer.
> Written by Shubham Wagdarkar

---

## The Elevator Pitch (30 seconds)

> "I built a Python service that connects to any major ITSM platform — ServiceNow, Jira,
> PagerDuty, Ivanti, or Freshservice — and automatically routes unassigned incidents to the
> correct team. Instead of a human manually reading each ticket and deciding which team should
> handle it, my tool reads the incident description, figures out what kind of problem it is —
> network issue, security breach, database failure — and auto-assigns it. Every routing decision
> gets logged to a database so you can audit accuracy and improve over time."

---

## The Problem It Solves

- In enterprise IT, incidents come in 24/7 across multiple platforms
- Someone has to read each one and decide: is this a network problem? A database problem? A security issue?
- Manual triage is slow, inconsistent, and happens at 3am when nobody wants to do it
- Misrouted tickets waste time — the wrong team gets paged, they re-route it, add 20–30 min delay to a P1 outage
- Most enterprises run 2–3 ITSM tools simultaneously — this router works across all of them

---

## How It Works — Plain English

1. **Every 60 seconds**, the tool asks your ITSM platform: "give me all new incidents with no team assigned"
2. For each incident, it reads the title and description
3. It runs the text through a three-stage classifier to figure out which team should own it
4. It updates the ticket in the platform with the correct team assignment
5. It writes a log entry to a database recording what happened, why, and which platform it came from

---

## The Classification Logic — The Most Important Part

This is what makes it interesting technically. It's a **three-stage decision cascade:**

### Stage 1 — Keyword Matching
- You define keywords for each team in a YAML config file
- Network team: `vpn, firewall, dns, latency, packet loss`
- Security team: `malware, phishing, breach, vulnerability`
- The tool scans the incident text for these keywords
- The team with the most keyword hits wins
- Handles ~70% of incidents — fast, transparent, easy to debug

### Stage 2 — Machine Learning (fallback)
- If no keywords match, a trained ML model takes over
- Uses TF-IDF (converts text to numbers) + Logistic Regression (classifies)
- The model was trained on the keyword list itself — no labeled incident data needed
- If the model is confident enough (≥ 72%), it routes based on ML prediction

### Stage 3 — Hardcoded Fallback
- If ML confidence is too low, route to IT Service Desk
- Guarantees 100% assignment rate — no ticket ever gets dropped

### Why This Cascade Design?
- Keywords are fast, accurate, and explainable ("it matched 'vpn' and 'firewall'")
- ML handles ambiguous language keywords can't catch
- Fallback ensures the system never fails silently

```
Incident Text
      │
      ▼
 Keyword Match? ──YES──▶ Route (confidence ≥ 0.75)
      │
      NO
      │
      ▼
 ML Predict ──conf ≥ 0.72──▶ Route
      │
    conf < 0.72
      │
      ▼
 Fallback → IT Service Desk (always routes)
```

---

## The Multi-Platform Design — What Makes This Advanced

Every ITSM platform has a different REST API, different auth, different data formats. The solution is a **BaseITSMClient abstract class** — it defines the interface every platform client must implement:

```
get_new_incidents() → list[dict]   # Normalised incident schema
assign_incident()   → dict         # Platform-native assignment
health_check()      → bool         # Connectivity test
```

Each platform client (ServiceNow, Jira, PagerDuty, Ivanti, Freshservice) implements this interface and normalises its data to the same dict schema. The classifier and router never know which platform is active — they always receive the same format.

**Result:** switching platforms is one CLI flag: `--platform jira`

---

## Key Technical Concepts

### Abstract Base Class (BaseITSMClient)
- Defines a contract that all platform clients must fulfil
- Uses Python's `abc.ABC` and `@abstractmethod` decorators
- Guarantees the router always has `get_new_incidents()`, `assign_incident()`, `health_check()`
- Classic interface/implementation pattern from enterprise software architecture

### ServiceNow REST API
- ServiceNow exposes all its data through a **Table API**
- `GET /api/now/table/incident` — fetch incidents with OData-style filters
- `PATCH /api/now/table/incident/{sys_id}` — update a field on a record
- `sys_id` is ServiceNow's internal unique ID for every record

### Jira Service Management REST API
- Auth: Basic auth — your Atlassian email + an API token (not your password)
- `GET /rest/api/3/search` — JQL-based query for issues
- Assignment via `PUT /rest/api/3/issue/{key}` with component field

### PagerDuty REST API
- Auth: `Authorization: Token token=YOUR_KEY` header
- `GET /incidents?statuses[]=triggered` — fetch triggered incidents
- Assignment via `PUT /incidents/{id}` with escalation_policy

### Ivanti Neurons REST API
- Uses OData query syntax: `$filter=Status eq 'Active' and Team eq ''`
- `PATCH /api/odata/businessobject/incidents('{id}')` — update record fields

### Freshservice REST API
- Auth: API key as username, literal string "X" as password (Freshservice design)
- `GET /api/v2/tickets?type=Incident&status=2` — fetch open tickets
- `PUT /api/v2/tickets/{id}` with `group_id` (integer)

### TF-IDF (Term Frequency–Inverse Document Frequency)
- Converts raw text into a numerical vector a model can process
- "VPN" in an incident about networking scores high
- Common words like "the", "is" score low — they appear everywhere
- `ngram_range=(1,3)` means it considers single words AND 2–3 word phrases

### Logistic Regression
- A classification algorithm — takes a vector of numbers, outputs a category
- Simple, fast, interpretable — good choice when you have limited training data
- Returns `predict_proba()` — confidence scores per class, not just a single label
- That confidence score is what determines keyword vs ML vs fallback routing

### sklearn Pipeline
- Chains TF-IDF + Logistic Regression into one object
- `pipeline.fit(X, y)` — train on text samples
- `pipeline.predict_proba(text)` — classify new text
- Can be saved to disk with `joblib` and reloaded without retraining

### PostgreSQL Audit Log
- Every routing decision is written to a `routing_audit` table
- Columns: incident ID, platform, team assigned, method, confidence, matched keywords, success/failure
- Includes `platform` column so you can compare routing accuracy across ServiceNow vs Jira vs PagerDuty

### Retry Logic
- HTTP calls to any ITSM can fail — timeouts, rate limits, server errors
- `urllib3.Retry` automatically retries on 429, 500, 502, 503, 504 status codes
- Exponential backoff — waits 2s, then 4s, then 8s between retries
- Same retry strategy applied consistently across all 5 platform clients

---

## Design Decisions You Can Defend

**"Why keyword matching before ML?"**
> Keywords are deterministic, fast, and fully explainable. If a ticket has "vpn" and
> "firewall" in it, you don't need a model. Saving ML for ambiguous cases makes the
> system more reliable and easier to debug.

**"Why bootstrap ML from keywords instead of using labeled data?"**
> We don't have a labeled incident dataset on Day 1. By generating training examples
> from the keyword corpus, we get a working model immediately. As real incident data
> accumulates, you retrain on that instead.

**"Why Logistic Regression instead of a neural network?"**
> LR is fast, interpretable, and works well on short text with limited training data.
> A transformer model would be overkill — it adds latency, cost, and complexity for
> marginal accuracy gains on structured IT incident text.

**"Why an abstract base class for clients?"**
> It enforces a contract: every platform client must implement the same three methods.
> The router stays completely platform-agnostic. Adding a 6th platform (e.g. Zendesk)
> means writing one new client file — nothing else changes.

**"Why YAML for routing rules?"**
> Ops teams need to update keywords without touching Python code. YAML is readable,
> version-controllable, and doesn't require a redeployment to change routing behavior.

**"Why log to PostgreSQL instead of a file?"**
> PostgreSQL lets you answer questions like "which platform has the highest fallback rate?"
> or "which team gets the most P1s?" with a single SQL query. Files can't do that.

---

## Assignment Groups

| Group | Team | Sample Keywords |
|---|---|---|
| `network` | Network Operations | vpn, firewall, dns, latency, packet loss |
| `security` | Security Operations | malware, phishing, breach, vulnerability |
| `database` | Database Administration | postgresql, deadlock, replication, query |
| `application` | Application Operations | pod, kubernetes, container, deploy, api |
| `infrastructure` | Infrastructure Engineering | server, vm, disk, cpu, hypervisor, azure |
| `service_desk` | IT Service Desk (fallback) | password reset, printer, outlook |

---

## Metrics

| Metric | Value |
|---|---|
| Supported ITSM platforms | 5 |
| Assignment groups | 6 |
| Keywords across all groups | 100+ |
| Classification stages | 3 (keyword → ML → fallback) |
| ML confidence threshold | 72% |
| Unit tests | 36, all passing |
| Polling interval | 60 seconds (configurable) |
| Max incidents per run | 50 (configurable) |

---

## One-Line Answers to Common Questions

| Question | Answer |
|---|---|
| "What if the ITSM platform is down?" | Retry logic with exponential backoff; errors logged to audit table |
| "What if the ML routes wrong?" | Adjust keywords in YAML — no code change needed; or lower the ML threshold |
| "How do you improve accuracy over time?" | Audit table gives you ground truth to retrain the ML model |
| "What's dry-run mode?" | Classifies incidents but never writes back to the platform — safe for testing in prod |
| "Can you add a 6th platform like Zendesk?" | Yes — implement BaseITSMClient, add env vars, register in build_client(). Nothing else changes. |
| "How does it handle incidents it can't classify?" | Always falls back to IT Service Desk — 100% assignment rate guaranteed |
| "Is it safe to run against production?" | Yes — use `--dry-run` flag to classify without any writes |
| "How do you compare routing quality across platforms?" | `SELECT platform, classification_method, COUNT(*) FROM routing_audit GROUP BY 1,2` |

---

## File Map

| File | What It Does | Key Concept |
|---|---|---|
| `main.py` | CLI entrypoint, client factory, scheduler | argparse, factory pattern |
| `src/clients/base_client.py` | Abstract interface all clients implement | ABC, abstractmethod |
| `src/clients/snow_client.py` | ServiceNow Table REST API | Basic auth, sys_id |
| `src/clients/jira_client.py` | Jira Service Management REST API | API token, JQL |
| `src/clients/pagerduty_client.py` | PagerDuty REST API v2 | Token header, escalation policy |
| `src/clients/ivanti_client.py` | Ivanti Neurons OData REST API | OData filter, PATCH |
| `src/clients/freshservice_client.py` | Freshservice REST API v2 | API key auth, group_id |
| `src/classifier.py` | Two-stage text classification engine | TF-IDF, Logistic Regression |
| `src/router.py` | Orchestrates classify → assign → decision | Dataclass, dry-run pattern |
| `src/audit.py` | Writes every decision to PostgreSQL | psycopg2, auto-schema |
| `config/routing_rules.yaml` | Keywords, platform group IDs, thresholds | YAML config, platform_ids |
| `tests/test_classifier.py` | Unit tests for classification logic | pytest, fixtures |
| `tests/test_router.py` | Unit tests for routing, dry-run, multi-platform | pytest, MagicMock |
| `tests/clients/test_jira_client.py` | Jira client normalisation + error tests | pytest, patch |
| `tests/clients/test_freshservice_client.py` | Freshservice client normalisation + error tests | pytest, patch |

---

## Stack Summary

| Library | Why Used |
|---|---|
| `requests` | HTTP calls to all 5 ITSM REST APIs |
| `scikit-learn` | TF-IDF vectorizer + Logistic Regression classifier |
| `psycopg2` | PostgreSQL connection and query execution |
| `PyYAML` | Parse routing_rules.yaml config file |
| `schedule` | Lightweight Python job scheduler |
| `python-dotenv` | Load credentials from .env file |
| `joblib` | Save and reload trained sklearn model |
| `pytest` | Unit testing framework |

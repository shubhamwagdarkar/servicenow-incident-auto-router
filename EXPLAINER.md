# Project Explainer — servicenow-incident-auto-router

> How to explain this project to anyone — from a non-technical recruiter to a Staff Engineer.
> Written by Shubham Wagdarkar

---

## The Elevator Pitch (30 seconds)

> "I built a Python service that monitors ServiceNow for new IT incidents that haven't been
> assigned yet. Instead of a human manually reading each ticket and deciding which team should
> handle it, my tool reads the incident description, figures out what kind of problem it is —
> network issue, security breach, database failure — and automatically assigns it to the right
> team. Every routing decision gets logged to a database so you can audit and improve accuracy
> over time."

---

## The Problem It Solves

- In enterprise IT, incidents come in 24/7
- Someone has to read each one and decide: is this a network problem? A database problem? A security issue?
- Manual triage is slow, inconsistent, and happens at 3am when nobody wants to do it
- Misrouted tickets waste time — the wrong team gets paged, they re-route it, add 20–30 min delay to a P1 outage
- This tool eliminates that entire manual step

---

## How It Works — Plain English

1. **Every 60 seconds**, the tool asks ServiceNow: "give me all new incidents with no team assigned"
2. For each incident, it reads the title and description
3. It tries to figure out which team should own it
4. It updates the ticket in ServiceNow with the correct team
5. It writes a log entry to a database recording what happened and why

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

## Key Technical Concepts

### ServiceNow REST API
- ServiceNow exposes all its data through a **Table API**
- `GET /api/now/table/incident` — fetch incidents with filters
- `PATCH /api/now/table/incident/{sys_id}` — update a field on a record
- You authenticate with basic auth (username + password)
- `sys_id` is ServiceNow's internal unique ID for every record
- `sysparm_query` is how you filter — like a URL-encoded WHERE clause

### TF-IDF (Term Frequency–Inverse Document Frequency)
- Converts raw text into a numerical vector a model can process
- "VPN" in an incident about networking scores high
- Common words like "the", "is" score low — they appear everywhere
- `ngram_range=(1,3)` means it considers single words AND 2–3 word phrases
  ("packet loss", "connection timeout")

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
- Columns: incident number, which team it went to, how it was classified,
  confidence score, matched keywords, whether it succeeded
- Without this you can't measure accuracy, retrain the model, or answer
  "why did this P1 go to the wrong team?"

### Retry Logic
- HTTP calls to ServiceNow can fail — timeouts, rate limits, server errors
- `urllib3.Retry` automatically retries on 429, 500, 502, 503, 504 status codes
- Exponential backoff — waits 2s, then 4s, then 8s between retries
- Prevents the tool from crashing on a transient network blip

### `schedule` Library
- Pure Python job scheduler — no Celery, no Redis, no infrastructure needed
- `schedule.every(60).seconds.do(job)` — that's the entire scheduling logic
- Appropriate for a single-process polling service

---

## Design Decisions You Can Defend

**"Why keyword matching before ML?"**
> Keywords are deterministic, fast, and fully explainable. If a ticket has "vpn" and
> "firewall" in it, you don't need a model — the answer is obvious. Saving ML for
> ambiguous cases makes the system more reliable and easier to debug.

**"Why bootstrap ML from keywords instead of using labeled data?"**
> We don't have a labeled incident dataset on Day 1. By generating training examples
> from the keyword corpus, we get a working model immediately. As real incident data
> accumulates, you retrain on that instead.

**"Why Logistic Regression instead of a neural network?"**
> LR is fast, interpretable, and works well on short text with limited training data.
> A transformer model would be overkill here — it adds latency, cost, and complexity
> for marginal accuracy gains on structured IT incident text.

**"Why YAML for routing rules?"**
> Ops teams need to update keywords without touching Python code. YAML is readable,
> version-controllable, and doesn't require a redeployment to change routing behavior.

**"Why log to PostgreSQL instead of a file?"**
> Files don't give you aggregations, trend queries, or easy integration with dashboards.
> PostgreSQL lets you answer questions like "what % of incidents route via ML?" or
> "which team gets the most P1s?" with a single SQL query.

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
| Assignment groups | 6 |
| Keywords across all groups | 100+ |
| Classification stages | 3 (keyword → ML → fallback) |
| ML confidence threshold | 72% |
| Unit tests | 20, all passing |
| Polling interval | 60 seconds (configurable) |
| Max incidents per run | 50 (configurable) |

---

## One-Line Answers to Common Questions

| Question | Answer |
|---|---|
| "What if ServiceNow is down?" | Retry logic with exponential backoff; errors logged to audit table |
| "What if the ML routes wrong?" | Adjust keywords in YAML — no code change needed; or lower the ML threshold |
| "How do you improve accuracy over time?" | Audit table gives you ground truth to retrain the ML model |
| "What's dry-run mode?" | Classifies incidents but never writes back to ServiceNow — safe for testing in prod |
| "Could this work for Jira or PagerDuty?" | Yes — swap out `snow_client.py` for a Jira/PagerDuty client, rest stays the same |
| "How does it handle incidents it can't classify?" | Always falls back to IT Service Desk — 100% assignment rate guaranteed |
| "Is it safe to run against production?" | Yes — use `--dry-run` flag to classify without any ServiceNow writes |

---

## File Map

| File | What It Does | Key Concept |
|---|---|---|
| `main.py` | CLI entrypoint, wires everything together | argparse, schedule |
| `src/snow_client.py` | Talks to ServiceNow REST API | HTTP, retry, basic auth |
| `src/classifier.py` | Two-stage text classification engine | TF-IDF, Logistic Regression |
| `src/router.py` | Orchestrates classify → assign → decision | Dataclass, dry-run pattern |
| `src/audit.py` | Writes every decision to PostgreSQL | psycopg2, auto-schema |
| `config/routing_rules.yaml` | Keywords, group IDs, thresholds | YAML config pattern |
| `tests/test_classifier.py` | Unit tests for classification logic | pytest, fixtures |
| `tests/test_router.py` | Unit tests for routing + dry-run | pytest, MagicMock |

---

## Stack Summary

| Library | Why Used |
|---|---|
| `requests` | HTTP calls to ServiceNow REST API |
| `scikit-learn` | TF-IDF vectorizer + Logistic Regression classifier |
| `psycopg2` | PostgreSQL connection and query execution |
| `PyYAML` | Parse routing_rules.yaml config file |
| `schedule` | Lightweight Python job scheduler |
| `python-dotenv` | Load credentials from .env file |
| `joblib` | Save and reload trained sklearn model |
| `pytest` | Unit testing framework |

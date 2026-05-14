# Scout

> **See the failure before anyone notices.**

Scout is an open-source recon agent for Tableau Cloud and Tableau Server. When a refresh fails, Scout watches it happen, runs a Claude-powered triage playbook, and writes a plain-English diagnosis — cause, owner, fix steps — to a local audit log and your terminal.

Scout is **eyes-on, hands-off, mouth-shut**. It reads from Tableau and writes to a local SQLite database. It does not deliver to Slack, page anyone, or mutate your Tableau site. The delivery + action arm — TableauOps Autopilot — is a separate paid product at [tableauops.com/scout](https://tableauops.com/scout?utm_source=github&utm_medium=readme&utm_campaign=scout).

## What Scout does

When a refresh fails at 5:30am, Scout:

1. Pulls your refresh history into a local SQLite database (`scout.db`)
2. Computes per-target baselines — success rate, median duration, typical run hour, drift signals
3. Runs a two-stage triage playbook with Claude: a fast **router** (Haiku 4.5) classifies the failure pattern (`transient`, `schedule_drift`, `missing_creds`, `silent_cluster`, `per_target`), then a **specialist** loaded with only the tools and heuristic for that pattern produces _what changed → who's affected → cause and fix_
4. Writes the diagnosis to the local `audit_log` and prints it to your terminal — target, owner, cause, and the literal UI fix steps

```text
🔴 Sales_Daily_Extract failed (3-failure streak, started 17:32 UTC after the deploy)
   Owner: marcus@acme.com
   Cause: missing embedded credentials on the Snowflake connection
   Fix:
     1. Open Sales_Daily_Extract in Tableau Cloud → Data Sources → Connections
     2. Click Edit Connection on each connection, check Embedded password,
        re-enter username + password, save
     3. Trigger a verification refresh (Refresh Extracts → Run Now) and confirm it goes green
```

No execution. No notification. No API mechanics. No wire-format. The diagnosis sits in your local audit log and terminal, and you decide what to do with it.

That's already more than most Tableau ops teams have today. Most teams find out about a broken refresh when a stakeholder messages "the dashboard looks wrong" at 9:15am.

## Quick start

```bash
git clone https://github.com/tableauops/scout.git
cd scout
pip install -r requirements.txt

# Configure: Tableau PAT + Anthropic key
cp .env.example .env
# edit .env

# Pull your refresh history into scout.db
python pull_jobs.py --reset

# Run Scout
python agent.py
```

That's it. Scout reads the last 7 days of refresh jobs, opens incidents on failures, and writes diagnoses to `scout.db` + terminal.

## Autonomy

Scout OSS is fixed at **autonomy level 1 (plan-only)**. It watches and diagnoses; it does not deliver, notify, or execute. Every finding lands in `audit_log` and on your terminal — you decide what to do next.

Higher levels live in [TableauOps Autopilot](https://tableauops.com/scout?utm_source=github&utm_medium=readme&utm_campaign=scout-autonomy):

| Level | Name | Where |
|---|---|---|
| **1** | Plan only | **Scout (free)** |
| 2 | Notify owner — Slack / PagerDuty / Teams, on-call routing | Autopilot |
| 3 | Confirm-each-action | Autopilot |
| 4 | Auto-safe execution | Autopilot |
| 5 | Full autonomy | Autopilot |

## Free vs. paid

| Free OSS Scout (level 1) | TableauOps Autopilot (levels 2–5) |
|---|---|
| Diagnosis to local audit log + terminal | **Owner notification** — Slack, PagerDuty, Teams, on-call routing |
| Single Tableau site | **Autopilot** — actually fixes the safe stuff |
| 7-day rolling baselines | **Persistent baselines** that learn across months |
| Local SQLite | **Web dashboard** — incident timeline, audit explorer, drift charts |
| Single user | **Fleet view** across multiple sites/orgs, RBAC, SSO |
| You run it | **Hosted deployment** option |

[Start a 30-day pilot →](https://tableauops.com/scout/start?utm_source=github&utm_medium=readme&utm_campaign=scout-pilot)

## Configuration

Required:

```bash
TABLEAU_PAT_NAME=...
TABLEAU_PAT_SECRET=...
TABLEAU_SITE=mycompany
TABLEAU_SERVER=https://10ax.online.tableau.com
ANTHROPIC_API_KEY=sk-ant-...
```

Optional:

```bash
SCOUT_SUCCESS_TARGET=99.5                                # reliability bar (default 100.0)
SCOUT_NO_NAG=1                                           # silence the upgrade footer in Block 10
REPLAY_MODE=1                                            # use cached Anthropic responses (offline-safe)
SCOUT_ROUTER_MODEL=claude-haiku-4-5-20251001             # Stage 1 classifier model
SCOUT_SPECIALIST_FAST_MODEL=claude-haiku-4-5-20251001    # Stage 2 model for transient / schedule_drift / silent_cluster
SCOUT_SPECIALIST_DEEP_MODEL=claude-opus-4-7              # Stage 2 model for missing_creds / per_target
```

[`.env.example`](.env.example) carries the required vars; copy it to `.env` and fill in your values.

## Replay mode

Every Anthropic call is cached in `scout.db`'s `api_cache` table. Set `REPLAY_MODE=1` and Scout reproduces the cached decision deterministically — useful for offline demos, CI, or development without burning API credits.

## Architecture

```
agent.py            ← entrypoint; the 10-block flow (auth → pull → baseline → triage → log)
agent_loop.py       ← two-stage triage: router (Haiku, ~5 tools) → pattern-specific specialist (Haiku/Opus, 4–7 tools). Falls back to a single-loop legacy triage if the router can't classify or the specialist needs to reclassify twice.
db.py               ← SQLite schema + queries (refresh_history, baselines, audit_log, incidents FSM)
tableau_tools.py    ← Tableau REST + Metadata API client (read-only)
remediation.py      ← plain-English fix-step renderer
pull_jobs.py        ← refresh-history ingestion
```

## License

[Apache 2.0](LICENSE). Use it, fork it, ship it.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Issues and PRs welcome.

## Security

See [`SECURITY.md`](SECURITY.md). Report vulnerabilities privately to security@tableauops.com.

---

Built by [TableauOps](https://tableauops.com/?utm_source=github&utm_medium=readme&utm_campaign=scout-footer). If you want Scout to actually deliver the diagnosis or fix what it finds, run [TableauOps Autopilot](https://tableauops.com/scout?utm_source=github&utm_medium=readme&utm_campaign=scout-footer).

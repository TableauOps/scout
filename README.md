# Scout

> **Catch the failure before anyone notices.**

Scout is an open-source recon agent for Tableau Cloud and Tableau Server. When a refresh fails, Scout watches it happen, runs a repeatable triage playbook, identifies the cause and the owner, and calls it in to Slack — with the diagnosis and the fix in plain English.

Scout is **eyes-on, hands-off**. It reads from Tableau and writes only to Slack; it never mutates your site. The action arm — Autopilot — is a separate paid product at [tableauops.com/scout](https://tableauops.com/scout?utm_source=github&utm_medium=readme&utm_campaign=scout).

## What Scout does

When a refresh fails at 5:30am, Scout:

1. Pulls your refresh history into a local SQLite database (`scout.db`)
2. Computes per-target baselines — success rate, median duration, typical run hour, drift signals
3. Runs a two-stage triage playbook with Claude: a fast **router** (Haiku 4.5) classifies the failure pattern (`transient`, `schedule_drift`, `missing_creds`, `silent_cluster`, `per_target`), then a **specialist** loaded with only the tools and heuristic for that pattern produces _what changed → who's affected → cause and fix_
4. Posts to Slack with the target, the owner, the diagnosis, and the literal UI steps to fix it

```text
🔴 Sales_Daily_Extract failed (3-failure streak, started 17:32 UTC after the deploy)
   Cause: missing embedded credentials on the Snowflake connection
   Owner: marcus@acme.com
   Fix:
     1. Open Sales_Daily_Extract in Tableau Cloud → Data Sources → Connections
     2. Click Edit Connection on each connection, check Embedded password,
        re-enter username + password, save
     3. Trigger a verification refresh (Refresh Extracts → Run Now) and confirm it goes green
```

No execution. No API mechanics. No wire-format. Just the diagnosis and the human steps.

By the time the 9am exec dashboard is opened, Marcus has already fixed it.

## Quick start

```bash
git clone https://github.com/tableauops/scout.git
cd scout
pip install -r requirements.txt

# Configure: Tableau PAT + Anthropic key + (optional) Slack webhook
cp .env.example .env
# edit .env

# Pull your refresh history into scout.db
python pull_jobs.py --reset

# Run Scout
python agent.py --autonomy 2
```

That's it. Scout reads the last 7 days of refresh jobs, opens incidents on failures, posts to Slack.

## Autonomy levels

| Level | Name | Behavior |
|---|---|---|
| **1** | Plan only | Silent recon. Triage runs, audit log captures the recommendation, no Slack. |
| **2** | Notify owner | Triage + Slack call-in to the right human. _Default._ |

Levels 3–5 (execution) live in [TableauOps Autopilot](https://tableauops.com/scout?utm_source=github&utm_medium=readme&utm_campaign=scout-autonomy), the paid action arm. Scout itself is hard-capped at level 2 by design — it's a scout, not a sapper.

## Free vs. paid

OSS Scout is a complete, free diagnostician. The paid [TableauOps](https://tableauops.com/scout?utm_source=github&utm_medium=readme&utm_campaign=scout-paid) product adds:

| Free OSS Scout | TableauOps (paid) |
|---|---|
| Diagnose + Slack call-in (autonomy 1–2) | **Autopilot** — actually fixes the safe stuff |
| Local SQLite + terminal viewer | **Web dashboard** — incident timeline, audit explorer, drift charts |
| Single Tableau site | **Fleet view** across multiple sites/orgs |
| 7-day rolling baselines | **Persistent baselines** that learn across months |
| Slack webhook | **PagerDuty, Teams, Jira**, on-call routing |
| Single user | **RBAC, SSO, audit retention** |
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
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...   # if unset, falls back to a console panel
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
agent.py            ← entrypoint; the 10-block flow (auth → pull → baseline → triage → call-in → audit)
agent_loop.py       ← two-stage triage: router (Haiku, ~5 tools) → pattern-specific specialist (Haiku/Opus, 4–7 tools). Falls back to a single-loop legacy triage if the router can't classify or the specialist needs to reclassify twice.
db.py               ← SQLite schema + queries (refresh_history, baselines, audit_log, incidents FSM)
tableau_tools.py    ← Tableau REST + Metadata API client (read-only)
slack.py            ← Block Kit message builder
remediation.py      ← plain-English fix-step renderer for owner notifications
pull_jobs.py        ← refresh-history ingestion
```

## License

[Apache 2.0](LICENSE). Use it, fork it, ship it.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Issues and PRs welcome.

## Security

See [`SECURITY.md`](SECURITY.md). Report vulnerabilities privately to security@tableauops.com.

---

Built by [TableauOps](https://tableauops.com/?utm_source=github&utm_medium=readme&utm_campaign=scout-footer). If you want Scout to actually fix what it finds, run [TableauOps Autopilot](https://tableauops.com/scout?utm_source=github&utm_medium=readme&utm_campaign=scout-footer).

# Security policy

## Reporting a vulnerability

If you've found a security issue in Scout, please report it privately — **do not open a public issue.**

Email **security@tableauops.com** with:

- A description of the issue
- Steps to reproduce (or a proof-of-concept)
- Impact assessment (what an attacker could do)
- Your name / handle for credit if you want it (optional)

We'll acknowledge within 72 hours and keep you posted on the fix. Once a patch ships and a reasonable disclosure window has passed, we'll publish the advisory.

## What's in scope

- Anything in this repository: `agent.py`, `agent_loop.py`, `db.py`, `tableau_tools.py`, `slack.py`, `remediation.py`, etc.
- The Tableau API client patterns (auth, session handling, rate limiting)
- The Slack webhook posting path
- The Anthropic API caching layer (`api_cache` table in `scout.db`)
- Any scenario where Scout could leak credentials, PII, or secrets to a place it shouldn't

## What's out of scope

- The paid TableauOps Autopilot product (executor, dashboard, multi-tenant SaaS) — those are not in this repo. Report issues for those to the same address; they'll be triaged separately.
- Vulnerabilities in dependencies (Anthropic SDK, `tableauserverclient`, Rich, etc.) — please report upstream. We'll update our pins when fixes ship.
- Issues that require local filesystem access on the machine running Scout (Scout is a local agent; assume the operator is trusted).

## What Scout does and doesn't touch

For threat modeling:

- **Reads** from Tableau Cloud / Server via the configured PAT (REST + Metadata API)
- **Reads + writes** to a local SQLite file (`scout.db`)
- **Writes** to Anthropic's API (LLM calls, with prompt caching)
- **Writes** to your Slack webhook URL (if configured)
- **Does NOT** call `update_*`, `refresh()`, `embed_credentials`, or any other mutating Tableau API
- **Does NOT** phone home to tableauops.com or anywhere else
- **Does NOT** persist credentials beyond the `.env` file the operator provides

## Hall of fame

We maintain a list of researchers who've reported valid issues. If you'd like to be listed, say so in your report.

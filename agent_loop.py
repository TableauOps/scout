"""Scout triage agent — Claude tool-use loop with api_cache for stage replay.

Every Claude call is keyed by hash(model + system + tools + messages) and stored
in scout.db's api_cache table. Set REPLAY_MODE=1 to read from cache only — if a
key isn't there, we raise rather than hit the API. That's the on-stage WiFi
fallback: every prep run populates the cache; demo day either runs live (and
keeps writing) or runs from cache (REPLAY_MODE=1).
"""
import hashlib
import json
import os
from datetime import datetime, timezone

from anthropic import Anthropic, APIError
from rich.console import Console
from rich.panel import Panel

from db import (
    api_cache_get,
    api_cache_put,
    current_failure_streaks,
    get_baseline_for_target,
    get_recent_failures,
    get_target_history,
    health_summary,
    overdue_targets,
    schedule_drift_candidates,
    get_schedule_baseline,
    get_open_incidents,
)
from tableau_tools import (
    get_datasource_owner       as tt_get_datasource_owner,
    get_datasource_connections as tt_get_datasource_connections,
    get_downstream_assets      as tt_get_downstream_assets,
    get_failed_job_details     as tt_get_failed_job_details,
)
def _broker_policy(target_name: str) -> dict:
    """Ask the credential broker whether it can embed creds for this target.

    Why this isn't cred_store.lookup(): the agent does not hold SCOUT_CRED_*
    env vars. Only the broker does. The agent only learns whether creds
    *exist* (yes/no) — it never sees them.

    In OSS Scout there is no broker; SCOUT_BROKER_URL is unset and this
    short-circuits without touching the network. The broker ships with
    TableauOps Autopilot.
    """
    import json as _json
    import urllib.request, urllib.error
    from urllib.parse import urlencode
    broker_url = os.environ.get("SCOUT_BROKER_URL")
    if not broker_url:
        return {
            "target_name":     target_name,
            "in_allowlist":    False,
            "creds_available": False,
            "broker_disabled": True,
            "note":             "No credential broker configured (set SCOUT_BROKER_URL to enable). "
                                "Scout OSS recommends notify_owner; the embed_credentials path is in Autopilot.",
        }
    try:
        with urllib.request.urlopen(f"{broker_url}/policy?{urlencode({'target_name': target_name})}", timeout=5) as resp:
            return _json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        return {"target_name": target_name, "in_allowlist": False, "creds_available": False, "broker_error": str(e)}

console = Console()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_KEY") or os.environ.get("ANTHROPIC_API_KEY")
MODEL = os.environ.get("SCOUT_MODEL", "claude-opus-4-7")
ROUTER_MODEL = os.environ.get("SCOUT_ROUTER_MODEL", "claude-haiku-4-5-20251001")
SPECIALIST_FAST_MODEL = os.environ.get("SCOUT_SPECIALIST_FAST_MODEL", ROUTER_MODEL)
SPECIALIST_DEEP_MODEL = os.environ.get("SCOUT_SPECIALIST_DEEP_MODEL", MODEL)
REPLAY_MODE = os.environ.get("REPLAY_MODE") == "1"
SUCCESS_TARGET = float(os.environ.get("SCOUT_SUCCESS_TARGET", "100.0"))

# Defer the missing-key error to first actual use. agent.py preflights env vars
# at startup so a missing key surfaces in the run plan, not after a 3-minute
# refresh-history pull. REPLAY_MODE=1 doesn't need a real key (cache-only).
claude = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None


SYSTEM_PROMPT = """You are Scout, an ops triage assistant for Tableau Cloud refresh failures.

## Reliability bar
Treat any success rate below {success_target}% as a problem worth investigating, regardless of how small the gap. A 99.5% success rate is NOT acceptable when the bar is {success_target}% — that's still real failures hitting real users. Do not dismiss low failure counts ("only 6 failures in 24h", "fleet is healthy at 94.7%") as acceptable. Every individual failure has a cause; your job is to find it.

Do NOT compare success rates across windows (24h vs 7d vs 30d) and conclude "things are fine." A higher recent rate doesn't mean the older failures don't matter — they had causes too, and those causes may recur.

## Your investigation toolkit
Map the question to the right tool. Don't default to one tool for everything:

| Question | Tool |
|---|---|
| Overall fleet health, gap to bar | get_health_summary |
| What's actively failing right now (streaks)? | get_failing_targets |
| Past N runs for a specific target (timing, finish codes) | get_target_history |
| Is THIS run anomalous vs. its own normal? | get_target_baseline |
| Is something overdue based on its cadence? | get_overdue_targets |
| Who owns this datasource (for notify_owner action)? | get_datasource_owner |
| What does this datasource connect to upstream? Are multiple targets sharing one bad connection? | get_datasource_connections |
| Blast radius — what depends on this downstream? | get_downstream_assets |
| Actual error message / notes for a specific job_id | get_failed_job_details (rate-limited; use only when you have a job_id and need the error string) |
| Is this target running at the wrong hour, or with a different cadence than usual? | get_schedule_drift_candidates |
| What's the established baseline for one target's schedule? | get_schedule_baseline |

## Heuristics
- Start with get_health_summary, then get_failing_targets. Don't skip ahead.
- **Scan the full failing_targets list before picking a target.** Before drilling on the longest streak, look for clusters of multiple targets sharing the same recent `last_failure_at` (within ~1 hour of each other). A recent cluster of even short streaks (2–3 each) is a stronger signal of an active incident than an old long-running streak — long streaks are usually known/parked problems, fresh clusters are the new fire. If you see a cluster, prioritize it over the longest single streak unless that streak is also fresh.
- Once you have a target, get_target_history + get_target_baseline together tell you "what changed."
- If failures cluster in time across multiple targets → upstream/infra (use get_datasource_connections to confirm shared dependency).
- If only one target fails repeatedly → target-specific (use get_target_baseline + get_failed_job_details for the error).
- **Missing-embed-credentials pattern:** if multiple targets failed simultaneously starting at the same recent timestamp (e.g. just after a deployment window), and they're all datasources, suspect a release that uploaded without embedded credentials. Call get_datasource_connections (you'll see connections exist) and check_credentials_available (Scout may have creds saved that can be embedded). If creds are available, recommend `embed_credentials`.
- **Schedule-drift pattern:** if you see jobs running at unusual hours, or a cluster of recent runs at the same off-hour, call get_schedule_drift_candidates and compare each candidate's recent_hour vs. baseline_hour. If the new pattern looks deliberate (consistent over multiple recent runs, baseline sample size is small), recommend `approve_schedule_change`. If it looks accidental or its cause is unclear, recommend `flag_drift_for_owner`.
- **Transient-failure pattern:** streak=1 or 2, recent (last few hours), notes contain "cancelled" / "timeout" / "connection reset" / "service unavailable" — or notes are empty/sparse — AND no auth/permission/credential keywords appear. Target has a healthy success history (high baseline success rate). This is a one-off blip, not a systemic break. Recommend `retry_refresh` at high confidence. Do NOT recommend embed_credentials — the creds were never the issue.

## Output contract — three-step triage playbook
After gathering enough evidence, deliver your reasoning as three explicit steps. Each step is a paragraph beginning with the EXACT label below (caps, em dash, colon). Do not skip a step; if you don't have data for a step, say so explicitly.

STEP 1 — WHAT CHANGED:
Describe the failure pattern grounded in the evidence you just fetched. Cite specific facts: the streak length, when it started, baseline duration vs. recent duration, the gap from baseline. Distinguish facts (from tool output) from inference (your reasoning).

STEP 2 — WHO'S AFFECTED:
Quantify the blast radius. How many downstream workbooks/sheets depend on this? Who owns it? If you didn't fetch this data, say so and explain why it matters.

STEP 3 — LIKELY CAUSE & REMEDIATION:
Form a hypothesis connecting facts to inference, then state the most useful next action **in plain English**, describing what a human operator should do in the Tableau Cloud UI or by re-publishing from Tableau Desktop. Examples of cause hypotheses: timeout from query growth, upstream connection failure, credential rotation, schedule contention.

Then, on its own line:

   RECOMMENDATION: {{"action": "retry_refresh" | "embed_credentials" | "approve_schedule_change" | "flag_drift_for_owner" | "notify_owner" | "investigate" | "no_action", "target_name": "...", "reason": "...", "confidence": "high"|"medium"|"low"}}

## Output discipline — Scout is recon, not execution
Scout watches and logs. Scout does NOT mutate the Tableau site, and your output reflects that: never emit REST endpoints, HTTP method names (PUT/POST/PATCH/DELETE), URL paths (`/api/.../sites/...`), payload bodies, JSON request shapes, curl commands, or any wire-format API call. Describe remediation as a UI walkthrough or a desktop-publish step — exactly the way a human would tell another human. STEP 3 lands verbatim in the local `audit_log` and on the operator's terminal — write it for the *human* who is going to fix it by hand, not for an automation that is going to call the API. If the user explicitly asks for an API call, decline and rewrite as UI/desktop steps.

## Tool-call narration (audience-facing)
Before each tool call, emit a single sentence in your text response in this exact form:

   WHY: <one sentence — what we need, which tool, what we expect back>

Examples:
- `WHY: We need to know whether this target's recent runs match its usual schedule; calling get_schedule_drift_candidates; expecting a small list of drifted targets or empty.`
- `WHY: We need the actual error string for job_id abc123 to confirm the auth-failure hypothesis; calling get_failed_job_details; expecting notes with a connection error message.`

The WHY sentence MUST appear immediately before the tool_use, on its own line, in the same text response. This narration is rendered for the audience.

Only use "no_action" if the fleet success rate is at or above {success_target}% AND there are no recent failures to explain. Otherwise pick the most useful next action.

Be concise. Use tools to gather evidence — never ask the user for data you can fetch yourself. If a tool returns empty or errors, note it and proceed with what you have rather than retrying the same call.
"""


# ---------------------------------------------------------------------------
# Two-stage prompts: router + per-pattern specialist
# Same behavioral contract as the monolithic SYSTEM_PROMPT above; split so
# Stage 1 (Haiku, ~3 tools) only classifies, and Stage 2 loads only the
# heuristic and tools relevant to the routed pattern. See README two-stage
# section for cost analysis.
# ---------------------------------------------------------------------------

ROUTER_SYSTEM_PROMPT = """You are Scout's router. Your only job is to classify the dominant failure pattern and pick the single most concerning target. You do NOT investigate, diagnose, or recommend — a specialist runs after you.

## Reliability bar
Any success rate below {success_target}% is worth investigating. Even small failure counts deserve a cause.

## Patterns
- `transient`      — one or two recent failures on a normally-healthy target. Streak short OR scattered single-run failures, baseline good.
- `schedule_drift` — targets running at unusual hours, or cadence has shifted vs. baseline. Multiple recent off-hour runs.
- `missing_creds`  — multiple datasources FAIL simultaneously at the same recent timestamp (smells like a deploy that dropped embedded creds).
- `silent_cluster` — multiple targets STOPPED running (clean stops, finish_code=0, no failure events) and are now significantly overdue. Smells like a paused schedule, a deactivated DB user, or a permission change. Distinct from missing_creds — silent_cluster has NO failure events, just silence.
- `per_target`     — one target with a sustained issue (long streak, growing duration, repeated failures over many days). No simultaneous cluster.
- `none`           — fleet at or above the success target AND no failing streaks AND no overdue cluster AND no scattered recent failures.

## How to pick
1. Call get_health_summary to see the gap to the bar.
2. Call get_failing_targets — currently-failing targets (streaks + last_failure_at).
3. Call get_overdue_targets — targets that should have run by now but didn't.
4. If failing_targets is empty but the fleet shows a gap, call get_recent_failures — it surfaces scattered single-run failures that don't form streaks.
5. Optionally call get_open_incidents to avoid re-opening cases already in flight.

## Picking the target
- get_failing_targets has a fresh CLUSTER (multiple targets with last_failure_at within ~1 hour) → route `missing_creds`. Pick one of them.
- get_failing_targets is empty BUT get_overdue_targets shows a CLUSTER (multiple targets with similar overdue_ratio, all silent for ~the same duration) → route `silent_cluster`. Pick one of the cluster members.
- get_recent_failures shows scattered isolated failures (no streaks, no cluster) → route `transient` if the picked target has a healthy baseline, `per_target` if it has sustained issues.
- get_failing_targets has one fresh, short streak on a healthy target → `transient`.
- get_failing_targets has a long streak (many days) on a target with a degraded baseline → `per_target`.
- Runs are happening at unusual hours → `schedule_drift`.
- Prefer FRESHEST signal over old ones; long streaks are usually known/parked.

## Output contract
Emit exactly one line, nothing else:

   ROUTE: {{"pattern": "transient" | "schedule_drift" | "missing_creds" | "silent_cluster" | "per_target" | "none", "target_name": "..." or null, "rationale": "<one short sentence>"}}

If pattern is `none`, target_name should be null. Otherwise target_name is required.

Do not produce STEP 1/2/3 panels, do not produce RECOMMENDATION, do not investigate further. One ROUTE line and stop.
"""


_COMMON_FOOTER = """

## Reliability bar
Treat any success rate below {success_target}% as a problem worth investigating, regardless of how small the gap. Do not dismiss low failure counts ("only 6 in 24h", "fleet healthy at 94.7%") as acceptable. Every individual failure has a cause; your job is to find it.

## Tool-call narration
Before each tool call, emit a single sentence in your text response in this exact form, on its own line:

   WHY: <one sentence — what we need, which tool, what we expect back>

Example: `WHY: We need the recent run pattern for this target; calling get_target_history; expecting a list of completed_at + finish_code rows.`

## Output contract — three-step triage playbook
After gathering enough evidence, deliver your reasoning as three explicit steps. Each step is a paragraph beginning with the EXACT label below (caps, em dash, colon):

STEP 1 — WHAT CHANGED:
Describe the failure pattern grounded in evidence. Cite specific facts: streak length, baseline duration vs. recent, gap from baseline. Distinguish facts from inference.

STEP 2 — WHO'S AFFECTED:
Quantify blast radius. Downstream workbooks, owner. If you didn't fetch this, say so and explain why it matters.

STEP 3 — LIKELY CAUSE & REMEDIATION:
Form a hypothesis connecting facts to inference, then state the most useful next action in plain English, describing what a human operator should do in the Tableau Cloud UI or by re-publishing from Tableau Desktop.

Then on its own line:

   RECOMMENDATION: {{"action": "retry_refresh" | "embed_credentials" | "approve_schedule_change" | "flag_drift_for_owner" | "notify_owner" | "investigate" | "no_action", "target_name": "...", "reason": "...", "confidence": "high"|"medium"|"low"}}

## Reclassify escape hatch
If your evidence STRONGLY contradicts the routed hypothesis (e.g., you were routed `transient` but the baseline shows persistent flakiness over weeks), do NOT force a recommendation. Instead, on its own line, emit:

   RECLASSIFY: {{"pattern": "transient" | "schedule_drift" | "missing_creds" | "per_target", "reason": "<short>"}}

Use this sparingly — only when the data clearly disagrees with the routing. Otherwise complete the playbook with the best recommendation you can.

## Output discipline — Scout is recon, not execution
Scout watches and logs. Scout does NOT mutate the Tableau site. Never emit REST endpoints, HTTP method names, URL paths, payload bodies, JSON request shapes, or curl commands. Describe remediation as a UI walkthrough or a desktop-publish step — exactly the way a human would tell another human. STEP 3 lands in the local `audit_log` and on the operator's terminal — write it for the *human* who will fix it by hand.

Be concise. Use tools to gather evidence — never ask the user for data you can fetch yourself. If a tool returns empty or errors, note it and proceed with what you have rather than retrying the same call.
"""


_PATTERN_HEURISTICS = {
    "transient": """## Hypothesis: transient blip
Streak is 1–2, recent (last few hours), target has a healthy success history. A one-off cancellation, timeout, connection reset, or service hiccup — credentials were never the issue.

Steps:
- get_target_history: confirm streak length and timing.
- get_target_baseline: confirm baseline success rate is high (>95%).
- get_failed_job_details: optional — read notes to confirm no auth/credential/permission keywords. Costs a rate-limited call; skip if the streak is obvious.

Decision rules:
- Notes mention 'cancelled' / 'timeout' / 'connection reset' / 'service unavailable' OR notes are empty/sparse, AND no auth keywords, AND baseline is healthy → recommend retry_refresh, high confidence.
- Baseline shows persistent flakiness (long-term low success rate, repeated streaks over weeks) → emit RECLASSIFY to per_target.
""",

    "schedule_drift": """## Hypothesis: schedule drift
A target is running at a different hour-of-day or with a different cadence than its established baseline.

Steps:
- get_schedule_drift_candidates: confirm the target appears with significant drift.
- get_schedule_baseline: read the established baseline (typical hour-of-day, inter-arrival, sample size).
- get_target_history: see whether recent runs cluster consistently at the new pattern (deliberate) or scattered (accidental).
- get_datasource_owner: needed for either approve_schedule_change or flag_drift_for_owner.

Decision rules:
- New pattern is consistent over multiple recent runs AND baseline sample size is small → likely deliberate, recommend approve_schedule_change.
- New pattern is irregular or its cause is unclear → recommend flag_drift_for_owner.
- Target is not actually drifting (recent runs match baseline within a small tolerance) → emit RECLASSIFY to per_target or none.
""",

    "missing_creds": """## Hypothesis: missing embed credentials
Multiple targets failed simultaneously starting at the same recent timestamp, suggesting a release that uploaded targets without embedded credentials.

Steps:
- get_datasource_connections: confirm valid endpoints exist (rules out a missing-source problem).
- check_credentials_available: ask the broker whether Scout has saved credentials for this datasource. Broker returns yes/no — Scout never sees the secret.
- get_target_history: verify simultaneous start of failures (cluster around one timestamp).
- get_datasource_owner: needed for the notify_owner fallback if creds are not available.

Decision rules:
- creds_available=true → recommend embed_credentials, high confidence.
- creds_available=false → recommend notify_owner; STEP 3 describes how the owner re-publishes from Desktop with embedded creds.
- Failures are NOT simultaneous (single target, different timestamps) → emit RECLASSIFY to per_target.
""",

    "silent_cluster": """## Hypothesis: silent cluster (paused schedule or invalidated DB user)
Multiple targets have STOPPED running at roughly the same time. Last runs were SUCCESSES (finish_code=0, clean stops) — there are no failure events. Common causes: a paused/disabled schedule, a deactivated or password-rotated DB user that prevents the scheduler from starting the job at all, or an ownership change.

Steps:
- get_overdue_targets: confirm the cluster — count cluster members and their overdue_ratio, look for similar gap_sec values (silence began at roughly the same time).
- get_target_history: pick one cluster member; confirm its most recent run was a clean SUCCESS (finish_code=0), not a failure. That's the diagnostic difference vs. missing_creds.
- get_datasource_connections: pull connections on a couple of cluster members; confirm a SHARED upstream (same server/database/user). A shared user is the strongest signal that this is a credential or permission issue at the user level, not per-target.
- get_open_incidents: critical — the FSM may already track this exact cluster from a prior run. Don't open a duplicate incident or re-notify.
- get_datasource_owner: needed for notify_owner if no incident covers it yet.

Decision rules:
- An open incident already covers this cluster (matching target_name OR matching shared upstream/owner) → recommend no_action, high confidence; cite the incident_id in `reason`.
- Shared upstream + clean stops + no incident → recommend notify_owner. STEP 3 should walk the owner through (a) checking the schedule's Active state in Tableau Cloud's Schedules view, (b) verifying the DB user is still valid (test connection, re-embed credentials if rotated), (c) running one cluster member as a smoke test before resuming the cadence.
- Cluster members do NOT share an upstream (different hosts/users) → emit RECLASSIFY to per_target — it's coincidence, not a single root cause.
- Most recent run was a FAILURE, not a success → emit RECLASSIFY to missing_creds (the silence is downstream of failures, not a clean stop).
""",

    "per_target": """## Hypothesis: target-specific cause
This single target has its own cause — query growth, broken upstream connection, schema change, credential rotation, or something else specific to it. No simultaneous cluster across other targets.

Steps:
- get_target_history: recent run pattern (timing, finish codes, durations).
- get_target_baseline: 'what changed' — duration growth, success rate degradation.
- get_failed_job_details: actual error string for the most recent failure.
- get_datasource_connections: upstream — a DB-side issue can mimic a target-specific cause.
- get_downstream_assets: blast radius for urgency weighting.
- get_datasource_owner: for notify_owner.

Decision rules:
- Recommend notify_owner with diagnosis + UI fix steps tailored to the error string.
- Confidence: high if the error string explicitly names the cause; medium if inferring from baseline shifts; low if the error is generic.
""",
}


def _specialist_prompt(pattern: str, target_name: str, rationale: str) -> str:
    """Build the Stage 2 system prompt: pattern header + heuristic + common footer."""
    heuristic = _PATTERN_HEURISTICS[pattern]
    header = (
        f"You are Scout investigating a {pattern!r} hypothesis on target {target_name!r}.\n"
        f"Router rationale: {rationale}\n\n"
        f"Confirm or reject the hypothesis. If confirmed, deliver the three-step playbook + RECOMMENDATION. "
        f"If your evidence strongly contradicts the routing, emit RECLASSIFY instead.\n\n"
    )
    return header + heuristic + _COMMON_FOOTER.format(success_target=SUCCESS_TARGET)


def _extract_whys(text: str) -> tuple[str, list[str]]:
    """Pull out 'WHY: ...' lines from text so they can be rendered next to
    their tool call instead of cluttering the body. Returns (cleaned_text, whys)."""
    import re
    whys: list[str] = []
    cleaned_lines: list[str] = []
    for line in text.splitlines():
        m = re.match(r"\s*WHY\s*:\s*(.+)\s*$", line, re.IGNORECASE)
        if m:
            whys.append(m.group(1).strip())
        else:
            cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip(), whys


def _split_playbook(text: str) -> list[tuple[str, str]] | None:
    """Parse 'STEP N — TITLE: body' segments out of the agent's text. Returns
    [(label, body)] or None if no STEP markers found. Body may follow the label
    on the same line (after the ':') or on subsequent lines."""
    import re
    pattern = re.compile(r"(STEP\s*\d+\s*[—\-].*?)(?=STEP\s*\d+\s*[—\-]|RECOMMENDATION:|\Z)", re.DOTALL | re.IGNORECASE)
    matches = pattern.findall(text)
    if not matches:
        return None
    out = []
    for m in matches:
        m = m.strip()
        m = re.sub(r"^\*+\s*", "", m)
        idx = m.find(":")
        if idx == -1:
            head, body = m, ""
        else:
            head = m[:idx].strip()
            body = m[idx + 1:].strip()
        body = re.sub(r"^\*+\s*", "", body)
        body = re.sub(r"\s*\*+\s*$", "", body)
        if body.replace("*", "").strip() == "":
            body = ""
        out.append((head, body or "(empty)"))
    return out


TOOLS_SCHEMA = [
    {
        "name": "get_health_summary",
        "description": (
            "Overall fleet health. Returns: all_rows (total in db), total (hydrated subset with finish_code), "
            "unhydrated (sparse rows from the Pager that haven't been backfilled via jobs.get_by_id), "
            "successes / failures / success_rate (computed ONLY over the hydrated subset — sparse rows are excluded). "
            "If unhydrated > 0, the success rate may understate or overstate fleet-wide reality; subsequent agent runs backfill those rows incrementally. "
            "Recent failure counts (last_24h, last_7d) are also hydrated-only."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_failing_targets",
        "description": "Targets currently in a failure streak (consecutive failures from most recent run, derived from history). Returns name, streak length, last run, last failure.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 10}},
        },
    },
    {
        "name": "get_recent_failures",
        "description": (
            "Recent hydrated failure rows within a time window — does NOT require a streak. "
            "Use this when get_failing_targets is empty but the fleet has scattered single-run "
            "failures that auto-recovered. Returns job_id, target_name, completed_at, "
            "duration_sec, finish_code, notes_excerpt (truncated ~200 chars) for each. "
            "Pair with get_failed_job_details(job_id) to read the full error string for any "
            "specific failure you want to drill into."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "window_h": {"type": "integer", "default": 24},
                "limit":    {"type": "integer", "default": 25},
            },
        },
    },
    {
        "name": "get_target_history",
        "description": "Most recent N runs for a specific target. Returns completed_at, finish_code (0 = success), duration_sec for each run.",
        "input_schema": {
            "type": "object",
            "properties": {
                "target_name": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["target_name"],
        },
    },
    {
        "name": "get_target_baseline",
        "description": "Baseline stats for a target: success rate, median/p95/stdev duration, total run count, first/last seen. Use to judge whether a recent run is anomalous.",
        "input_schema": {
            "type": "object",
            "properties": {"target_name": {"type": "string"}},
            "required": ["target_name"],
        },
    },
    {
        "name": "get_overdue_targets",
        "description": "Targets whose time-since-last-run exceeds their typical inter-arrival cadence. Surfaces 'should have run by now' signals.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 10}},
        },
    },
    {
        "name": "get_datasource_owner",
        "description": "Returns the owner (username, email) of a datasource. Use this to plan a notify_owner action or to attribute a failure.",
        "input_schema": {
            "type": "object",
            "properties": {"target_name": {"type": "string"}},
            "required": ["target_name"],
        },
    },
    {
        "name": "get_datasource_connections",
        "description": "Lists the upstream connections (databases, files) for a datasource. Use to investigate whether multiple failing targets share an upstream dependency (suggests infra/upstream cause vs. per-target).",
        "input_schema": {
            "type": "object",
            "properties": {"target_name": {"type": "string"}},
            "required": ["target_name"],
        },
    },
    {
        "name": "get_downstream_assets",
        "description": "Lists workbooks and sheets that depend on a datasource (via Metadata API / Tableau Catalog). Use for blast-radius / consequence weighting — a failing datasource with 50 downstream workbooks is more urgent than one with 0.",
        "input_schema": {
            "type": "object",
            "properties": {"target_name": {"type": "string"}},
            "required": ["target_name"],
        },
    },
    {
        "name": "get_failed_job_details",
        "description": "Hydrates a single job by job_id via TSC jobs.get_by_id — returns full notes (error string), finish_code, timestamps, datasource_id. WARNING: hits Tableau's rate-limited jobs/list bucket; will return {rate_limited: true} if throttled. Use only when you need the actual error message for a specific job.",
        "input_schema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
    },
    {
        "name": "get_open_incidents",
        "description": (
            "Returns the currently-open incidents Scout is tracking — each is a target+signal "
            "pair with a status in {open, triaging, fix_proposed, applying}. Use at the start of "
            "triage to see if there's already a case in flight on a target before opening a new one. "
            "Sorted by severity (critical first) then opened_at desc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 20}},
        },
    },
    {
        "name": "get_schedule_drift_candidates",
        "description": (
            "Targets whose recent runs deviate from their schedule baseline — "
            "either running at a wrong hour-of-day, or with a different inter-arrival cadence. "
            "Distinct from get_overdue_targets, which only flags lateness against the median interval. "
            "Use this to investigate 'is something running at the wrong time?' or to triage clusters of "
            "off-hour runs that look like a developer-triggered ad-hoc burst."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 10}},
        },
    },
    {
        "name": "get_schedule_baseline",
        "description": (
            "Stored schedule baseline for a single target: typical hour-of-day, typical inter-arrival, "
            "sample size, whether an owner has previously approved drift. Use to confirm what 'normal' "
            "looks like for a target before recommending approve_schedule_change vs flag_drift_for_owner."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"target_name": {"type": "string"}},
            "required": ["target_name"],
        },
    },
    {
        "name": "check_credentials_available",
        "description": (
            "Check Scout's credential store for saved credentials for this datasource. "
            "Use this when investigating a connection-related failure: if the failure looks "
            "like missing/invalid auth (e.g. multiple new datasources from a release all failing, "
            "or get_datasource_connections shows valid endpoints but refreshes still fail), call "
            "this to see if Scout can remediate by embedding stored creds. Returns "
            "{available: bool, hint: 'env vars to set if not'}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"target_name": {"type": "string"}},
            "required": ["target_name"],
        },
    },
]


TOOL_HANDLERS = {
    "get_health_summary":  lambda: health_summary(),
    "get_failing_targets": lambda limit=10: current_failure_streaks(limit=limit, min_streak=1),
    "get_recent_failures": lambda window_h=24, limit=25: get_recent_failures(window_h=window_h, limit=limit),
    "get_target_history":  lambda target_name, limit=20: get_target_history(target_name, limit),
    "get_target_baseline": lambda target_name: get_baseline_for_target(target_name),
    "get_overdue_targets":         lambda limit=10: overdue_targets(limit=limit),
    "get_open_incidents":          lambda limit=20: get_open_incidents(limit=limit),
    "get_schedule_drift_candidates": lambda limit=10: schedule_drift_candidates(limit=limit),
    "get_schedule_baseline":       lambda target_name: get_schedule_baseline(target_name) or {"error": f"no baseline for {target_name!r}"},
    "get_datasource_owner":        lambda target_name: tt_get_datasource_owner(target_name),
    "get_datasource_connections":  lambda target_name: tt_get_datasource_connections(target_name),
    "get_downstream_assets":       lambda target_name: tt_get_downstream_assets(target_name),
    "get_failed_job_details":      lambda job_id: tt_get_failed_job_details(job_id),
    "check_credentials_available": lambda target_name: (lambda p: {
        "target_name":          target_name,
        "available":            p.get("creds_available", False),
        "in_broker_allowlist":  p.get("in_allowlist", False),
        "broker_disabled":      p.get("broker_disabled", False),
        "note":                 p.get("note") or "Agent never reads creds. Broker confirms only yes/no presence.",
    })(_broker_policy(target_name)),
}


# ---------------------------------------------------------------------------
# Two-stage tool subsets.
# Router gets just enough to classify; specialists get only the tools their
# pattern actually uses. See _PATTERN_HEURISTICS for what each list covers.
# ---------------------------------------------------------------------------
_ROUTER_TOOL_NAMES = (
    "get_health_summary",
    "get_failing_targets",
    "get_recent_failures",   # scattered single-run failures that don't form streaks
    "get_overdue_targets",   # silent_cluster detection
    "get_open_incidents",
)

PATTERNS: dict[str, dict] = {
    "transient": {
        "tools":    ("get_target_history", "get_target_baseline", "get_recent_failures", "get_failed_job_details"),
        "model":    SPECIALIST_FAST_MODEL,
        "max_iter": 4,
    },
    "schedule_drift": {
        "tools":    ("get_schedule_drift_candidates", "get_schedule_baseline", "get_target_history", "get_datasource_owner"),
        "model":    SPECIALIST_FAST_MODEL,
        "max_iter": 4,
    },
    "missing_creds": {
        "tools":    ("get_datasource_connections", "check_credentials_available", "get_datasource_owner",
                     "get_target_history", "get_recent_failures"),
        "model":    SPECIALIST_DEEP_MODEL,
        "max_iter": 5,
    },
    "silent_cluster": {
        # Cluster diagnosis is mostly pattern-matching against history + connections,
        # not deep reasoning, so the fast model is enough. Keep get_open_incidents
        # in-loop so the specialist doesn't re-open a case the FSM already tracks.
        "tools":    ("get_overdue_targets", "get_target_history", "get_datasource_connections",
                     "get_datasource_owner", "get_open_incidents"),
        "model":    SPECIALIST_FAST_MODEL,
        "max_iter": 5,
    },
    "per_target": {
        "tools":    ("get_target_history", "get_target_baseline", "get_recent_failures", "get_failed_job_details",
                     "get_datasource_connections", "get_downstream_assets", "get_datasource_owner"),
        "model":    SPECIALIST_DEEP_MODEL,
        "max_iter": 5,
    },
}

_TOOL_BY_NAME = {t["name"]: t for t in TOOLS_SCHEMA}


def _tools_for(names) -> list:
    return [_TOOL_BY_NAME[n] for n in names]


def _cache_key(model: str, system, tools, messages) -> str:
    payload = json.dumps(
        {"model": model, "system": system, "tools": tools, "messages": messages},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _resolve_model(model: str | None) -> str:
    return model or MODEL


def _serialize_messages(messages):
    """Convert SDK content blocks back to plain dicts for hashing + caching."""
    out = []
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
            continue
        new_content = []
        for block in content:
            if isinstance(block, dict):
                new_content.append(block)
            else:
                new_content.append(block.model_dump() if hasattr(block, "model_dump") else dict(block))
        out.append({"role": m["role"], "content": new_content})
    return out


def cached_messages_create(messages, system, tools, model: str | None = None):
    """Wrap claude.messages.create with api_cache for replay mode.

    `model` defaults to module-level MODEL so existing callers keep their behavior.
    Two-stage triage passes ROUTER_MODEL / SPECIALIST_* explicitly; cache keys
    fold model in, so each stage gets its own slot."""
    model = _resolve_model(model)
    serialized = _serialize_messages(messages)
    key = _cache_key(model, system, tools, serialized)

    cached = api_cache_get(key)
    if cached is not None:
        from anthropic.types import Message
        return Message.model_validate_json(cached), True

    if REPLAY_MODE:
        raise RuntimeError(
            f"REPLAY_MODE=1 but cache miss for key {key[:12]}... "
            "Run live (unset REPLAY_MODE) to populate the cache, then retry."
        )

    if claude is None:
        raise RuntimeError(
            "ANTHROPIC_KEY (or ANTHROPIC_API_KEY) is not set. "
            "Scout's triage step needs a Claude API key to diagnose failures. "
            "Set it in .env or the environment, or run with REPLAY_MODE=1 if "
            "the api_cache is already populated."
        )

    response = claude.messages.create(
        model=model,
        max_tokens=16000,
        system=system,
        tools=tools,
        messages=messages,
        thinking={"type": "adaptive", "display": "summarized"},
    )

    api_cache_put(
        key,
        "anthropic",
        json.dumps({"model": model, "system": system, "tools": tools, "messages": serialized}, default=str),
        response.model_dump_json(),
    )
    return response, False


class _Reclassify(Exception):
    """Raised by investigate() when a specialist's evidence contradicts the
    router's classification. Carries a hint dict that route_triage can use to
    pick a different pattern on the next bounce."""

    def __init__(self, hint: dict):
        super().__init__(str(hint))
        self.hint = hint


def route_triage(prompt: str, prior_misroute: dict | None = None) -> dict:
    """Stage 1: classify the dominant failure pattern and pick a target.

    Runs the router model (Haiku by default) with a 3-tool subset. Returns:
      {pattern, target_name, rationale}
    where pattern is one of PATTERNS keys, "none", or "_unclear" if the router
    failed to emit a parseable ROUTE line within its iteration cap.
    """
    system = [{
        "type": "text",
        "text": ROUTER_SYSTEM_PROMPT.format(success_target=SUCCESS_TARGET),
        "cache_control": {"type": "ephemeral"},
    }]

    user_msg = prompt
    if prior_misroute:
        user_msg = (
            f"{prompt}\n\n"
            f"Note: a prior routing to pattern={prior_misroute.get('pattern')!r} "
            f"was rejected by the specialist with reason: "
            f"{prior_misroute.get('reason')!r}. Pick a different pattern."
        )
    messages = [{"role": "user", "content": user_msg}]
    tools = _tools_for(_ROUTER_TOOL_NAMES)

    valid_patterns = set(PATTERNS) | {"none"}

    for i in range(1, 4):  # router cap: 3 iterations
        try:
            response, from_cache = cached_messages_create(messages, system, tools, model=ROUTER_MODEL)
        except APIError as e:
            console.print(f"[red]Anthropic API error in router:[/red] {e}")
            return {"pattern": "_unclear", "target_name": None, "rationale": f"api_error: {e}"}

        cache_tag = " [dim](cached)[/dim]" if from_cache else ""
        console.print(f"[bold magenta]── router iter {i}{cache_tag} ──[/bold magenta]")

        route_dict: dict | None = None
        for block in response.content:
            if block.type == "text" and "ROUTE:" in block.text:
                raw = block.text.split("ROUTE:", 1)[1].strip().split("\n", 1)[0].strip()
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                pattern = parsed.get("pattern")
                if pattern in valid_patterns:
                    route_dict = {
                        "pattern":     pattern,
                        "target_name": parsed.get("target_name"),
                        "rationale":   parsed.get("rationale", ""),
                    }
            elif block.type == "tool_use":
                console.print(f"  [dim yellow]→ router calls {block.name}({json.dumps(block.input)})[/dim yellow]")

        if route_dict is not None:
            return route_dict

        if response.stop_reason != "tool_use":
            break

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            handler = TOOL_HANDLERS.get(block.name)
            if handler is None:
                result = {"error": f"unknown tool: {block.name}"}
            else:
                try:
                    result = handler(**(block.input or {}))
                except Exception as e:
                    result = {"error": f"{type(e).__name__}: {e}"}
            preview = json.dumps(result, default=str)
            console.print(f"  [dim]← {preview[:200]}{'...' if len(preview) > 200 else ''}[/dim]")
            tool_results.append({
                "type":        "tool_result",
                "tool_use_id": block.id,
                "content":     json.dumps(result, default=str),
            })
        messages.append({"role": "user", "content": tool_results})

    return {"pattern": "_unclear", "target_name": None, "rationale": "router did not emit a parseable ROUTE within its iteration cap"}


def investigate(route: dict) -> dict | None:
    """Stage 2: pattern-specific specialist. Returns the parsed RECOMMENDATION
    dict, or None if the specialist failed to produce one. Raises _Reclassify
    if the specialist's evidence contradicts the routing."""
    pattern = route["pattern"]
    if pattern not in PATTERNS:
        raise ValueError(f"investigate() got unsupported pattern: {pattern!r}")
    cfg = PATTERNS[pattern]
    target_name = route.get("target_name") or "(unspecified)"
    rationale   = route.get("rationale")   or ""

    system = [{
        "type": "text",
        "text": _specialist_prompt(pattern, target_name, rationale),
        "cache_control": {"type": "ephemeral"},
    }]

    initial_msg = (
        f"Investigate the {pattern!r} hypothesis on target {target_name!r}. "
        f"Confirm it with evidence, then deliver STEP 1/2/3 and RECOMMENDATION. "
        f"If the data strongly disagrees with the routing, emit RECLASSIFY instead."
    )
    messages = [{"role": "user", "content": initial_msg}]
    tools = _tools_for(cfg["tools"])

    recommendation: dict | None = None

    for i in range(1, cfg["max_iter"] + 1):
        try:
            response, from_cache = cached_messages_create(messages, system, tools, model=cfg["model"])
        except APIError as e:
            console.print(f"[red]Anthropic API error in specialist:[/red] {e}")
            return None

        cache_tag = " [dim](cached)[/dim]" if from_cache else ""
        console.print(f"\n[bold cyan]── stage 2 specialist ({pattern}, {cfg['model']}) — iter {i}{cache_tag} ──[/bold cyan]")

        last_why: str | None = None
        for block in response.content:
            if block.type == "thinking":
                if block.thinking:
                    console.print(f"[dim italic]thinking: {block.thinking}[/dim italic]")
            elif block.type == "text":
                # RECLASSIFY short-circuits — no point parsing playbook/recommendation.
                if "RECLASSIFY:" in block.text:
                    raw = block.text.split("RECLASSIFY:", 1)[1].strip().split("\n", 1)[0].strip()
                    try:
                        hint = json.loads(raw)
                    except json.JSONDecodeError:
                        hint = {"pattern": "per_target", "reason": "specialist emitted unparseable RECLASSIFY"}
                    console.print(Panel(
                        json.dumps(hint, indent=2),
                        title=f"[bold red]Specialist disagreed with routing → RECLASSIFY[/bold red]",
                        border_style="red",
                    ))
                    raise _Reclassify(hint)

                cleaned_text, whys = _extract_whys(block.text)
                if whys:
                    last_why = whys[-1]
                steps = _split_playbook(cleaned_text)
                if steps:
                    palette = ["cyan", "yellow", "magenta"]
                    for idx, (label, body) in enumerate(steps):
                        console.print(Panel(
                            body,
                            title=f"[bold]{label}[/bold]",
                            border_style=palette[idx % len(palette)],
                        ))
                    if "RECOMMENDATION:" in cleaned_text:
                        rec = cleaned_text[cleaned_text.index("RECOMMENDATION:"):]
                        console.print(rec)
                elif cleaned_text:
                    console.print(cleaned_text)

                if "RECOMMENDATION:" in block.text:
                    raw = block.text.split("RECOMMENDATION:", 1)[1].strip().split("\n", 1)[0].strip()
                    try:
                        recommendation = json.loads(raw)
                    except json.JSONDecodeError:
                        pass
            elif block.type == "tool_use":
                why_text = last_why or "(no WHY emitted before this call)"
                body = (
                    f"[bold]WHY:[/bold] {why_text}\n"
                    f"[dim]→ input:[/dim] {json.dumps(block.input)}"
                )
                console.print(Panel(
                    body,
                    title=f"[bold yellow]{block.name}[/bold yellow]",
                    border_style="yellow",
                    expand=False,
                ))

        if response.stop_reason != "tool_use":
            break

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            handler = TOOL_HANDLERS.get(block.name)
            if handler is None:
                result = {"error": f"unknown tool: {block.name}"}
            else:
                try:
                    result = handler(**(block.input or {}))
                except Exception as e:
                    result = {"error": f"{type(e).__name__}: {e}"}
            preview = json.dumps(result, default=str)
            console.print(f"  [dim]← {preview[:300]}{'...' if len(preview) > 300 else ''}[/dim]")
            tool_results.append({
                "type":        "tool_result",
                "tool_use_id": block.id,
                "content":     json.dumps(result, default=str),
            })
        messages.append({"role": "user", "content": tool_results})

    if recommendation:
        console.print(Panel.fit(
            json.dumps(recommendation, indent=2),
            title="[bold]Recommendation[/bold]",
            border_style="magenta",
        ))
    else:
        console.print("[yellow]Specialist produced no structured recommendation.[/yellow]")

    return recommendation


def run_triage(prompt: str, max_bounces: int = 1) -> dict | None:
    """Two-stage triage orchestrator.

    Stage 1 (router, ROUTER_MODEL): classify pattern + pick target.
    Stage 2 (specialist, pattern-specific model): produce recommendation.
    On RECLASSIFY: bounce back to Stage 1 once with the specialist's hint.
    On unparseable router output or exhausted bounces: fall back to the legacy
    single-loop full triage so we still return *something*.
    """
    prior_misroute: dict | None = None
    for bounce in range(max_bounces + 1):
        route = route_triage(prompt, prior_misroute=prior_misroute)
        pattern = route["pattern"]

        console.print(Panel.fit(
            f"[cyan]pattern[/cyan]={pattern}  "
            f"[cyan]target[/cyan]={route.get('target_name') or '(none)'}\n"
            f"[dim]rationale: {route.get('rationale') or '-'}[/dim]",
            title=f"[bold magenta]Stage 1 — Router (bounce {bounce})[/bold magenta]",
            border_style="magenta",
        ))

        if pattern == "none":
            return {
                "action":      "no_action",
                "target_name": None,
                "reason":      route.get("rationale") or "fleet at or above success target",
                "confidence":  "high",
            }

        if pattern == "_unclear":
            console.print("[yellow]Router did not produce a clean ROUTE — falling back to legacy single-loop triage.[/yellow]")
            return _legacy_run_triage(prompt)

        try:
            return investigate(route)
        except _Reclassify as r:
            prior_misroute = {
                "pattern": pattern,
                "reason":  r.hint.get("reason") if isinstance(r.hint, dict) else str(r.hint),
            }
            console.print(f"[yellow]Specialist requested reclassify → bouncing back to router (bounce {bounce + 1}/{max_bounces}).[/yellow]")

    console.print("[yellow]Reclassify budget exhausted — falling back to legacy single-loop triage.[/yellow]")
    return _legacy_run_triage(prompt)


def _legacy_run_triage(initial_message: str, max_iterations: int = 8) -> dict | None:
    """Original single-loop triage (Opus, all 14 tools, monolithic SYSTEM_PROMPT).
    Kept as the safety-net fallback when the two-stage flow can't make progress
    (router fails to emit ROUTE, or the reclassify bounce budget is exhausted)."""
    system = [{
        "type": "text",
        "text": SYSTEM_PROMPT.format(success_target=SUCCESS_TARGET),
        "cache_control": {"type": "ephemeral"},
    }]

    messages = [{"role": "user", "content": initial_message}]
    recommendation = None

    for i in range(1, max_iterations + 1):
        try:
            response, from_cache = cached_messages_create(messages, system, TOOLS_SCHEMA)
        except APIError as e:
            console.print(f"[red]Anthropic API error:[/red] {e}")
            return None

        cache_tag = " [dim](cached)[/dim]" if from_cache else ""
        console.print(f"\n[bold cyan]── iteration {i}{cache_tag} ──[/bold cyan]")

        last_why: str | None = None
        for block in response.content:
            if block.type == "thinking":
                if block.thinking:
                    console.print(f"[dim italic]thinking: {block.thinking}[/dim italic]")
            elif block.type == "text":
                cleaned_text, whys = _extract_whys(block.text)
                if whys:
                    last_why = whys[-1]
                steps = _split_playbook(cleaned_text)
                if steps:
                    palette = ["cyan", "yellow", "magenta"]
                    for idx, (label, body) in enumerate(steps):
                        console.print(Panel(
                            body,
                            title=f"[bold]{label}[/bold]",
                            border_style=palette[idx % len(palette)],
                        ))
                    if "RECOMMENDATION:" in cleaned_text:
                        rec = cleaned_text[cleaned_text.index("RECOMMENDATION:"):]
                        console.print(rec)
                elif cleaned_text:
                    console.print(cleaned_text)

                if "RECOMMENDATION:" in block.text:
                    raw = block.text.split("RECOMMENDATION:", 1)[1].strip()
                    raw = raw.split("\n", 1)[0].strip()
                    try:
                        recommendation = json.loads(raw)
                    except json.JSONDecodeError:
                        pass
            elif block.type == "tool_use":
                why_text = last_why or "(no WHY emitted before this call)"
                body = (
                    f"[bold]WHY:[/bold] {why_text}\n"
                    f"[dim]→ input:[/dim] {json.dumps(block.input)}"
                )
                console.print(Panel(
                    body,
                    title=f"[bold yellow]{block.name}[/bold yellow]",
                    border_style="yellow",
                    expand=False,
                ))
                # NOTE: do not clear last_why here. Models often emit ONE WHY
                # then a parallel batch of tool_use blocks; clearing would make
                # every tool past the first say "no WHY emitted." The WHY is
                # naturally replaced when the next text block arrives.

        if response.stop_reason != "tool_use":
            break

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            handler = TOOL_HANDLERS.get(block.name)
            if handler is None:
                result = {"error": f"unknown tool: {block.name}"}
            else:
                try:
                    result = handler(**(block.input or {}))
                except Exception as e:
                    result = {"error": f"{type(e).__name__}: {e}"}
            preview = json.dumps(result, default=str)
            console.print(f"  [dim]← {preview[:300]}{'...' if len(preview) > 300 else ''}[/dim]")
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
            })

        messages.append({"role": "user", "content": tool_results})

    if recommendation:
        console.print(Panel.fit(
            json.dumps(recommendation, indent=2),
            title="[bold]Recommendation[/bold]",
            border_style="magenta",
        ))
    else:
        console.print("[yellow]No structured recommendation produced.[/yellow]")

    return recommendation

import os
import sys
from datetime import datetime, timezone

# Force UTF-8 stdout so Rich's box-drawing + arrow chars survive Windows cp1252 consoles.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import tableauserverclient as TSC
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from db import (
    init_db,
    upsert_job,
    count_jobs,
    count_failures,
    health_summary,
    top_failing_targets,
    existing_job_ids,
    compute_baselines,
    get_baselines,
    reset_refresh_data,
    upsert_target,
    count_targets,
    current_failure_streaks,
    overdue_targets,
)

load_dotenv()
console = Console()


# ---------------------------------------------------------------------------
# Autonomy level — asked once at startup; controls Block 9 behavior
#
#   1 plan-only      silent recon; never call in, never execute
#   2 notify-owner   Slack the owner with diagnosis + plain-English fix steps
#
# Scout itself is hard-capped at 2 (eyes-on, hands-off). Levels 3–5 live in
# TableauOps Autopilot — a separate product that subscribes to Scout's
# recommendations and executes the safe ones. See https://tableauops.com/scout
# ---------------------------------------------------------------------------
MAX_AUTONOMY = 2
SLACK_ACTIONS = {"notify_owner", "flag_drift_for_owner"}

def _autopilot_upgrade_message(requested: int) -> str:
    return (
        f"[bold red]Autonomy {requested} is out of scope for Scout.[/bold red]\n\n"
        "Scout is a recon agent — it watches, diagnoses, and calls it in. "
        "Levels 3–5 (confirm-each, auto-safe, full-autonomy) require "
        "[bold]TableauOps Autopilot[/bold], the paid action arm.\n\n"
        "  → [link=https://tableauops.com/scout?utm_source=cli]https://tableauops.com/scout[/link]\n\n"
        "Run Scout at level 1 (silent recon) or 2 (call it in to Slack)."
    )

def _resolve_autonomy() -> int:
    raw = None
    if "--autonomy" in sys.argv:
        raw = sys.argv[sys.argv.index("--autonomy") + 1]
    elif os.environ.get("SCOUT_AUTONOMY"):
        raw = os.environ["SCOUT_AUTONOMY"]

    if raw is not None:
        try:
            level = int(raw)
        except ValueError:
            console.print(f"[red]Invalid autonomy value: {raw!r}. Expected 1 or 2.[/red]")
            sys.exit(2)
        if level > MAX_AUTONOMY:
            console.print(Panel.fit(_autopilot_upgrade_message(level), title="[bold]Autopilot required[/bold]", border_style="red"))
            sys.exit(2)
        if level < 1:
            console.print(f"[red]Autonomy must be 1 or 2; got {level}.[/red]")
            sys.exit(2)
        return level

    console.print(
        Panel.fit(
            "[bold]Select autonomy level:[/bold]\n"
            "  [cyan]1[/cyan]  Plan only      — silent recon; never call in\n"
            "  [cyan]2[/cyan]  Notify owner   — Slack the owner with diagnosis + fix steps  [dim](default)[/dim]\n\n"
            "[dim]Scout is capped at 2 (eyes-on, hands-off). Execution lives in TableauOps Autopilot.[/dim]",
            title="[bold]Autonomy[/bold]",
            border_style="cyan",
        )
    )
    while True:
        ans = input("Level [1-2, default 2]: ").strip() or "2"
        if ans in ("1", "2"):
            return int(ans)
        if ans in ("3", "4", "5"):
            console.print(Panel.fit(_autopilot_upgrade_message(int(ans)), title="[bold]Autopilot required[/bold]", border_style="red"))
            continue
        console.print("[red]Enter 1 or 2.[/red]")

AUTONOMY = _resolve_autonomy()
AUTONOMY_LABEL = {
    1: "plan-only",
    2: "notify-owner",
}[AUTONOMY]


def _check_required_env() -> None:
    """Fail fast if required env vars are missing, BEFORE the 3-minute refresh
    pull. Without this, a missing ANTHROPIC_KEY only surfaces at Block 6 when
    we import agent_loop — by which point we've already pulled 7k jobs.

    Tableau PAT is hard-required (Scout has nothing to read without it).
    Anthropic key is required unless REPLAY_MODE=1 (cache-only stage mode)."""
    required_tableau = {
        "TABLEAU_PAT_NAME":   "Personal Access Token name (create one at /#/site/<site>/account)",
        "TABLEAU_PAT_SECRET": "PAT secret value (shown once when the PAT is created)",
        "TABLEAU_SITE":       "Site content URL (e.g. 'tableauops')",
        "TABLEAU_SERVER":     "Pod URL (e.g. 'https://10ax.online.tableau.com')",
    }
    missing_tableau = [k for k in required_tableau if not os.environ.get(k)]

    replay = os.environ.get("REPLAY_MODE") == "1"
    has_anth = bool(os.environ.get("ANTHROPIC_KEY") or os.environ.get("ANTHROPIC_API_KEY"))
    missing_anth = (not has_anth) and (not replay)

    if not missing_tableau and not missing_anth:
        return

    lines = ["[bold red]Required environment variables are missing.[/bold red]\n"]
    if missing_tableau:
        lines.append("[bold]Tableau credentials[/bold] (required):")
        for k in missing_tableau:
            lines.append(f"  [red]✗[/red] [cyan]{k}[/cyan] — [dim]{required_tableau[k]}[/dim]")
        lines.append("")
    if missing_anth:
        lines.append("[bold]Anthropic API key[/bold] (required for triage):")
        lines.append("  [red]✗[/red] [cyan]ANTHROPIC_KEY[/cyan] (or [cyan]ANTHROPIC_API_KEY[/cyan]) — [dim]get one at https://console.anthropic.com[/dim]")
        lines.append("  [dim]Set [cyan]REPLAY_MODE=1[/cyan] to skip the live call and read from scout.db's api_cache instead.[/dim]")
        lines.append("")
    lines.append("[dim]Copy [cyan].env.example[/cyan] to [cyan].env[/cyan] and fill it in, or export the variables in your shell.[/dim]")

    console.print(Panel.fit("\n".join(lines), title="[bold]Setup incomplete[/bold]", border_style="red"))
    sys.exit(2)


def _print_preflight() -> None:
    """Show the user, in plain English, what this run will and won't do
    BEFORE any sign-in or API call happens. Cheap insurance against
    surprise behavior on a fresh install."""
    site_env   = os.environ.get("TABLEAU_SITE", "[red]not set[/red]")
    server_env = os.environ.get("TABLEAU_SERVER", "[red]not set[/red]")
    pat_env    = "[green]set[/green]" if os.environ.get("TABLEAU_PAT_NAME") and os.environ.get("TABLEAU_PAT_SECRET") else "[red]not set[/red]"
    anth_env   = "[green]set[/green]" if (os.environ.get("ANTHROPIC_KEY") or os.environ.get("ANTHROPIC_API_KEY")) else "[red]not set[/red]"
    slack_env  = "[green]configured[/green]" if os.environ.get("SLACK_WEBHOOK_URL") else "[yellow]not set — falls back to console panel[/yellow]"
    replay     = os.environ.get("REPLAY_MODE") == "1"
    success_t  = os.environ.get("SCOUT_SUCCESS_TARGET", "100.0")
    model      = os.environ.get("SCOUT_MODEL", "claude-opus-4-7")

    mode_blurb = {
        1: "[bold]Plan-only.[/bold] Scout watches, diagnoses, writes the audit log. [bold]No Slack[/bold], no notifications.",
        2: "[bold]Notify-owner.[/bold] Scout watches, diagnoses, and Slacks the right human with the diagnosis and UI fix steps. Never mutates Tableau.",
    }[AUTONOMY]

    plan = Table.grid(padding=(0, 2))
    plan.add_column(style="bold cyan", no_wrap=True)
    plan.add_column()
    plan.add_row("Mode",     f"autonomy {AUTONOMY} ({AUTONOMY_LABEL}) — {mode_blurb}")
    plan.add_row("Reads",    f"Tableau ([dim]{server_env} · site={site_env}[/dim]) [dim]· read-only[/dim]")
    plan.add_row("Calls",    f"Anthropic [dim]({model})[/dim]" + (" [yellow](REPLAY_MODE=1; cache-only, no live calls)[/yellow]" if replay else " [dim]· will use credits unless cache hits[/dim]"))
    plan.add_row("Writes",   "scout.db (local SQLite — refresh_history, baselines, audit_log, incidents)")
    if AUTONOMY == 2:
        plan.add_row("",     "Slack webhook " + slack_env)
    plan.add_row("",         "")
    plan.add_row("Won't do", "[bold]Mutate Tableau.[/bold] Read-only on Tableau API. No retries, no credential edits, no schedule writes — execution lives in [link=https://tableauops.com/scout]TableauOps Autopilot[/link].")
    plan.add_row("",         "")
    plan.add_row("Creds",    f"Tableau PAT {pat_env}  ·  Anthropic API key {anth_env}")
    plan.add_row("Bar",      f"success-rate target = {success_t}% (any gap is treated as worth investigating)")

    console.print(Panel(plan, title="[bold]Scout — Tableau ops recon[/bold] [dim]· about to run[/dim]", border_style="cyan"))
    console.print("[dim]Press Ctrl+C now to abort. Run with --autonomy 1 for silent (no Slack) mode.[/dim]\n")


_check_required_env()
_print_preflight()


# ---------------------------------------------------------------------------
# Block 1 — Sign in
# ---------------------------------------------------------------------------
PAT_NAME   = os.environ["TABLEAU_PAT_NAME"]
PAT_SECRET = os.environ["TABLEAU_PAT_SECRET"]
SITE       = os.environ["TABLEAU_SITE"]
SERVER_URL = os.environ["TABLEAU_SERVER"]

auth   = TSC.PersonalAccessTokenAuth(PAT_NAME, PAT_SECRET, site_id=SITE)
server = TSC.Server(SERVER_URL, use_server_version=True)
# Bound the TLS handshake / read so a transient pod hiccup fails fast instead
# of hanging on stage. Tableau Cloud sign-in is ~500ms healthy.
server.http_options.update({"timeout": (10, 30)})  # (connect, read) seconds


def _sign_in_cm(srv, creds, attempts: int = 3, backoff_s: float = 2.0):
    """Returns the context manager from server.auth.sign_in, retrying transient
    network errors (TLS handshake timeout, ReadTimeout, connection reset). Lets
    auth errors (401/403) and the final attempt re-raise."""
    import time as _time
    for i in range(1, attempts + 1):
        try:
            return srv.auth.sign_in(creds)
        except Exception as e:
            msg = f"{type(e).__name__}: {str(e)[:140]}"
            transient = any(k in msg.lower() for k in (
                "timeout", "timed out", "connection", "ssl", "handshake",
                "remote end closed", "max retries",
            ))
            if not transient or i == attempts:
                raise
            console.print(f"[yellow]sign_in attempt {i}/{attempts} hit transient error → {msg}; retrying in {backoff_s:.0f}s[/yellow]")
            _time.sleep(backoff_s)

console.print("[dim]WHY: authenticate to the site so every later TSC call carries a valid session token; calling server.auth.sign_in(auth); expecting is_signed_in()=True and a non-null site_id.[/dim]")
with _sign_in_cm(server, auth):
    # Force a real version probe NOW that we're authenticated. TSC's
    # use_server_version=True kwarg can silently leave server.version at the
    # 2.4 default if the unauthenticated serverInfo probe in __init__ didn't
    # succeed — which then trips assert_at_least_version on Pager(server.jobs).
    try:
        server.use_server_version()
    except Exception:
        pass
    console.print(
        Panel.fit(
            f"[green]signed_in[/green]={server.is_signed_in()}  "
            f"[cyan]site_id[/cyan]={server.site_id}  "
            f"[cyan]api[/cyan]={server.version}\n"
            f"[dim]{SERVER_URL}  •  site='{SITE}'[/dim]",
            title="[bold]Block 1 — Sign in[/bold]",
            border_style="green",
        )
    )

    # -----------------------------------------------------------------------
    # Block 2 — Pull refresh history → scout.db
    # -----------------------------------------------------------------------
    init_db()

    if "--reset" in sys.argv:
        reset_refresh_data()
        console.print("[yellow]--reset: wiped refresh_history and target_baselines[/yellow]")

    pulled_at = datetime.now(timezone.utc).isoformat()
    known = existing_job_ids()

    pulled = 0
    skipped = 0
    rate_limited = False
    console.print("[dim]WHY: we need every recent refresh job to build local history; iterating TSC.Pager(server.jobs) (paginated jobs/list); skipping job_ids already in scout.db so this is incremental and rate-limit-friendly.[/dim]")
    try:
        for job in TSC.Pager(server.jobs):
            jt = (job.type or "").lower()
            if "refresh" not in jt and "extract" not in jt:
                continue

            # Skip jobs we already have — keeps subsequent runs cheap and
            # avoids the N+1 / rate-limit trap.
            if job.id in known:
                skipped += 1
                continue

            started   = getattr(job, "started_at", None)
            # Pager exposes 'ended_at'; the rich get_by_id call uses 'completed_at'.
            # Use whichever is present.
            completed = getattr(job, "completed_at", None) or getattr(job, "ended_at", None)
            duration  = None
            if started and completed:
                duration = int((completed - started).total_seconds())

            raw_notes = getattr(job, "notes", None)
            if raw_notes is None:
                notes_text = None
            elif isinstance(raw_notes, str):
                notes_text = raw_notes
            else:
                try:
                    notes_text = "\n".join(
                        getattr(n, "text", None) or getattr(n, "value", None) or str(n)
                        for n in raw_notes
                    )
                except TypeError:
                    notes_text = str(raw_notes)

            # Pager items don't include datasource_id/workbook_id; use subtitle
            # as a stand-in target name. Block 3 resolves real ids.
            target_name = getattr(job, "subtitle", None) or getattr(job, "title", None)

            upsert_job({
                "job_id":       job.id,
                "job_type":     job.type,
                "target_type":  None,
                "target_id":    None,
                "target_name":  target_name,
                "created_at":   job.created_at.isoformat()   if job.created_at   else None,
                "started_at":   started.isoformat()           if started           else None,
                "completed_at": completed.isoformat()         if completed         else None,
                "duration_sec": duration,
                "finish_code":  getattr(job, "finish_code", None),
                "notes":        notes_text,
                "site_id":      server.site_id,
                "pulled_at":    pulled_at,
                "raw_xml":      None,
            })
            pulled += 1
    except TSC.ServerResponseError as e:
        if "429" in str(e):
            rate_limited = True
            console.print(f"[yellow]Rate limited after {pulled} new rows — proceeding with what we have.[/yellow]")
        else:
            raise

    # -----------------------------------------------------------------------
    # Block 2.1 — Hydrate sparse rows via get_by_id (rich finish_code, target,
    # notes). The Pager doesn't expose those; this fills them in for the most
    # recent N jobs without busting the rate limit. Skips fully-hydrated rows.
    # -----------------------------------------------------------------------
    from db import jobs_needing_hydration
    HYDRATE_BUDGET = int(os.environ.get("SCOUT_HYDRATE_LIMIT", "200"))
    if HYDRATE_BUDGET > 0 and not rate_limited:
        candidates = jobs_needing_hydration(limit=HYDRATE_BUDGET)
        hydrated = 0
        hydrate_429 = False
        if candidates:
            console.print(f"[dim]WHY: Pager returns ended_at + started_at but no finish_code/target_name; we need those for triage. Hydrating {len(candidates)} recent rows via jobs.get_by_id (rate-limited bucket — capped by SCOUT_HYDRATE_LIMIT, default 200).[/dim]")
            try:
                import time
                for jid in candidates:
                    try:
                        full = server.jobs.get_by_id(jid)
                    except TSC.ServerResponseError as e:
                        if "429" in str(e):
                            hydrate_429 = True
                            console.print(f"[yellow]Hydration rate-limited after {hydrated} rows.[/yellow]")
                            break
                        raise
                    raw_notes = getattr(full, "notes", None)
                    if raw_notes is None:
                        notes_text = None
                    elif isinstance(raw_notes, str):
                        notes_text = raw_notes
                    else:
                        try:
                            notes_text = "\n".join(getattr(n, "text", None) or str(n) for n in raw_notes)
                        except TypeError:
                            notes_text = str(raw_notes)
                    # Derive target_name from notes (e.g. "for Data Source 'X'") or fall back to subtitle.
                    target_name = getattr(full, "subtitle", None) or getattr(full, "title", None)
                    if not target_name and notes_text:
                        import re
                        m = re.search(r"for Data Source '([^']+)'", notes_text) or re.search(r"for Workbook '([^']+)'", notes_text)
                        if m:
                            target_name = m.group(1)
                    ds_id = getattr(full, "datasource_id", None)
                    wb_id = getattr(full, "workbook_id", None)
                    target_type = "datasource" if ds_id else ("workbook" if wb_id else None)
                    target_id   = ds_id or wb_id
                    started_h   = getattr(full, "started_at", None)
                    completed_h = getattr(full, "completed_at", None) or getattr(full, "ended_at", None)
                    duration_h  = None
                    if started_h and completed_h:
                        duration_h = int((completed_h - started_h).total_seconds())
                    upsert_job({
                        "job_id":       full.id,
                        "job_type":     full.type,
                        "target_type":  target_type,
                        "target_id":    target_id,
                        "target_name":  target_name,
                        "created_at":   full.created_at.isoformat() if full.created_at else None,
                        "started_at":   started_h.isoformat()        if started_h        else None,
                        "completed_at": completed_h.isoformat()      if completed_h      else None,
                        "duration_sec": duration_h,
                        "finish_code":  getattr(full, "finish_code", None),
                        "notes":        notes_text,
                        "site_id":      server.site_id,
                        "pulled_at":    pulled_at,
                        "raw_xml":      None,
                    })
                    hydrated += 1
                    time.sleep(0.05)   # gentle pacing
            except TSC.ServerResponseError:
                raise
            console.print(f"[dim]hydrated={hydrated}{' (rate-limited)' if hydrate_429 else ''}[/dim]")

    total     = count_jobs()
    failures  = count_failures()
    rl_note   = "  [yellow](rate limited)[/yellow]" if rate_limited else ""
    console.print(
        Panel.fit(
            f"[green]new[/green]={pulled}  "
            f"[dim]skipped[/dim]={skipped}  "
            f"[cyan]total_in_db[/cyan]={total}  "
            f"[red]failures[/red]={failures}{rl_note}\n"
            f"[dim]scout.db • table: refresh_history[/dim]",
            title="[bold]Block 2 — Refresh history[/bold]",
            border_style="green" if not rate_limited else "yellow",
        )
    )

    # Health summary
    h = health_summary()
    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="dim")
    summary.add_column()
    summary.add_row("date range",   f"{h['earliest']}  →  {h['latest']}")
    summary.add_row("rows in db",   f"{h['all_rows']:,} total  •  [bold]{h['total']:,} hydrated[/bold]  •  [dim]{h.get('unhydrated', 0):,} sparse (no finish_code yet)[/dim]")
    summary.add_row("success rate", f"[bold]{h['success_rate']:.1%}[/bold]  [dim]over {h['total']:,} hydrated rows ({h['successes']:,} success / {h['failures']:,} fail) — {h.get('unhydrated', 0):,} sparse rows excluded[/dim]")
    summary.add_row("avg duration", f"{h['avg_duration_sec'] or 0:.0f}s  (median {h['median_duration_sec'] or 0:.0f}s, max {h['max_duration_sec'] or 0:,}s) [dim]over hydrated rows[/dim]")
    summary.add_row("last 24h",     f"{h['last_24h_total']} runs  •  [red]{h['last_24h_failures']} failed[/red] [dim](hydrated only)[/dim]")
    summary.add_row("last 7d",      f"{h['last_7d_total']} runs  •  [red]{h['last_7d_failures']} failed[/red] [dim](hydrated only)[/dim]")
    console.print(Panel(summary, title="[bold]Health summary[/bold]", border_style="cyan"))
    if h.get("unhydrated", 0) > 0:
        console.print(
            f"[dim yellow]ℹ  {h['unhydrated']:,} rows are sparse (Pager returns ended_at/started_at but no finish_code). "
            f"Subsequent agent runs will backfill them incrementally (capped per run by SCOUT_HYDRATE_LIMIT, default 200).[/dim yellow]"
        )

    # By job_type
    jt = Table(title="By job type", show_header=True, header_style="bold")
    jt.add_column("type")
    jt.add_column("total", justify="right")
    jt.add_column("failed", justify="right", style="red")
    jt.add_column("fail %", justify="right")
    jt.add_column("avg sec", justify="right")
    for row in h["by_job_type"]:
        rate = (row["failed"] / row["total"] * 100) if row["total"] else 0
        jt.add_row(row["job_type"] or "(none)", f"{row['total']:,}", f"{row['failed']:,}", f"{rate:.1f}%", f"{row['avg_sec'] or 0:.0f}")
    console.print(jt)

    # Top failing targets
    top = top_failing_targets(limit=10)
    if top:
        tt = Table(title="Top 10 failing targets", show_header=True, header_style="bold")
        tt.add_column("target")
        tt.add_column("runs", justify="right")
        tt.add_column("failed", justify="right", style="red")
        tt.add_column("fail %", justify="right")
        for row in top:
            rate = (row["failed"] / row["runs"] * 100) if row["runs"] else 0
            name = (row["target_name"] or "")[:50]
            tt.add_row(name, f"{row['runs']:,}", f"{row['failed']:,}", f"{rate:.1f}%")
        console.print(tt)
    else:
        console.print("[yellow]No target_name populated — pull more jobs or check Block 2 ingestion.[/yellow]")

    # -----------------------------------------------------------------------
    # Block 2.5 — Pull task catalog -> targets (real target_id)
    # -----------------------------------------------------------------------
    # Build id -> name lookup from datasources + workbooks (tasks endpoint
    # only returns target.id, not target.name).
    console.print("[dim]WHY: tasks.get() returns target_id but not target_name; pulling datasources + workbooks first builds an id→name lookup so every task we ingest carries a human-readable name.[/dim]")
    ds_name_by_id = {ds.id: ds.name for ds in TSC.Pager(server.datasources)}
    wb_name_by_id = {wb.id: wb.name for wb in TSC.Pager(server.workbooks)}

    console.print("[dim]WHY: enumerate every scheduled refresh task (extract refresh, flow run, etc.); calling server.tasks.get(); expecting ~one entry per scheduled task with a target reference.[/dim]")
    tasks_pulled = 0
    tasks_result = server.tasks.get()
    tasks_list = tasks_result[0] if isinstance(tasks_result, tuple) else tasks_result
    for task in tasks_list:
        target = getattr(task, "target", None)
        if target is None:
            continue

        ttype  = getattr(target, "type", None) or "unknown"
        tid    = getattr(target, "id", None)
        if not tid:
            continue

        if ttype == "datasource":
            tname = ds_name_by_id.get(tid)
        elif ttype == "workbook":
            tname = wb_name_by_id.get(tid)
        else:
            tname = None

        upsert_target({
            "task_id":                  task.id,
            "target_type":              ttype,
            "target_id":                tid,
            "target_name":              tname,
            "task_type":                getattr(task, "task_type", None),
            "schedule_summary":         None,    # Cloud strips schedules
            "priority":                 getattr(task, "priority", None),
            "consecutive_failed_count": None,    # Cloud strips this; derived from history instead
            "last_run_at":              None,    # Cloud strips this; derived from history instead
            "pulled_at":                pulled_at,
        })
        tasks_pulled += 1

    console.print(
        Panel.fit(
            f"[green]tasks_pulled[/green]={tasks_pulled}  "
            f"[cyan]total_in_db[/cyan]={count_targets()}\n"
            f"[dim]{len(ds_name_by_id)} datasources + {len(wb_name_by_id)} workbooks resolved[/dim]\n"
            f"[dim]scout.db • table: targets[/dim]",
            title="[bold]Block 2.5 — Task catalog[/bold]",
            border_style="green",
        )
    )

    # -----------------------------------------------------------------------
    # Block 3.5 — Live ops signal (derived from refresh_history, not tasks)
    # Tableau Cloud strips schedule + last_run_at + consecutive_failed_count
    # from the tasks endpoint, so we infer everything from job history.
    # -----------------------------------------------------------------------
    streaks = current_failure_streaks(limit=15, min_streak=1)
    if streaks:
        ft = Table(title="Currently failing targets (derived: consecutive failures from latest run)", show_header=True, header_style="bold")
        ft.add_column("target")
        ft.add_column("streak", justify="right", style="red")
        ft.add_column("last run")
        ft.add_column("last failure")
        for row in streaks:
            name = (row["target_name"] or "")[:50]
            ft.add_row(name, f"{row['streak']}", (row["last_run_at"] or "")[:19], (row["last_failure_at"] or "")[:19])
        console.print(ft)
    else:
        console.print("[dim]No failure streaks detected (or refresh_history lacks target_name — re-run with --reset after rate limit lifts).[/dim]")

    overdue = overdue_targets(limit=10, overdue_factor=2.0)
    if overdue:
        ot = Table(title="Overdue targets (gap > 2× median inter-arrival)", show_header=True, header_style="bold")
        ot.add_column("target")
        ot.add_column("runs", justify="right")
        ot.add_column("median interval")
        ot.add_column("gap since last")
        ot.add_column("× overdue", justify="right", style="red")
        for row in overdue:
            name   = (row["target_name"] or "")[:50]
            med_h  = row["median_interval_sec"] / 3600
            gap_h  = row["gap_sec"] / 3600
            ot.add_row(name, f"{row['run_count']:,}", f"{med_h:.1f}h", f"{gap_h:.1f}h", f"{row['overdue_ratio']:.1f}×")
        console.print(ot)

    # -----------------------------------------------------------------------
    # Block 3 — Baselines per target
    # -----------------------------------------------------------------------
    written = compute_baselines(min_runs=3)
    console.print(
        Panel.fit(
            f"[green]baselines_computed[/green]={written}  "
            f"[dim](min 3 runs; duration stats use successes only)[/dim]\n"
            f"[dim]scout.db • table: target_baselines[/dim]",
            title="[bold]Block 3 — Baselines[/bold]",
            border_style="green",
        )
    )

    # Most-active targets — these are the ones we care about most
    bt = Table(title="Top 15 most-active targets (by run count)", show_header=True, header_style="bold")
    bt.add_column("target")
    bt.add_column("runs",     justify="right")
    bt.add_column("succ %",   justify="right")
    bt.add_column("median s", justify="right")
    bt.add_column("p95 s",    justify="right")
    bt.add_column("stdev s",  justify="right")
    for row in get_baselines(limit=15, order_by="total_runs DESC"):
        name   = (row["target_name"] or "")[:50]
        succ   = f"{row['success_rate']:.1%}" if row["success_rate"] is not None else "-"
        med    = f"{row['median_duration_sec']:.0f}" if row["median_duration_sec"] is not None else "-"
        p95    = f"{row['p95_duration_sec']:.0f}"    if row["p95_duration_sec"]    is not None else "-"
        stdev  = f"{row['stdev_duration_sec']:.0f}"  if row["stdev_duration_sec"]  is not None else "-"
        bt.add_row(name, f"{row['total_runs']:,}", succ, med, p95, stdev)
    console.print(bt)

    # Least-reliable targets — top candidates for triage
    rt = Table(title="Top 10 least-reliable (success rate ASC, ≥3 runs)", show_header=True, header_style="bold")
    rt.add_column("target")
    rt.add_column("runs",     justify="right")
    rt.add_column("succ %",   justify="right", style="red")
    rt.add_column("median s", justify="right")
    rt.add_column("last seen")
    for row in get_baselines(limit=10, order_by="success_rate ASC"):
        name      = (row["target_name"] or "")[:50]
        succ      = f"{row['success_rate']:.1%}" if row["success_rate"] is not None else "-"
        med       = f"{row['median_duration_sec']:.0f}" if row["median_duration_sec"] is not None else "-"
        last_seen = (row["last_seen"] or "")[:19]
        rt.add_row(name, f"{row['total_runs']:,}", succ, med, last_seen)
    console.print(rt)

    # -----------------------------------------------------------------------
    # Block 6 — Triage agent (Claude + tool calls + api_cache replay)
    # -----------------------------------------------------------------------
    import tableau_tools
    tableau_tools.configure(server)

    from agent_loop import (
        run_triage,
        MODEL,
        ROUTER_MODEL,
        SPECIALIST_FAST_MODEL,
        SPECIALIST_DEEP_MODEL,
        REPLAY_MODE,
        SUCCESS_TARGET,
    )

    console.print(
        Panel.fit(
            f"[cyan]router[/cyan]={ROUTER_MODEL}  "
            f"[cyan]specialist_fast[/cyan]={SPECIALIST_FAST_MODEL}  "
            f"[cyan]specialist_deep[/cyan]={SPECIALIST_DEEP_MODEL}\n"
            f"[cyan]replay_mode[/cyan]={REPLAY_MODE}  "
            f"[cyan]success_target[/cyan]={SUCCESS_TARGET}%\n"
            f"[dim]Two-stage triage: Stage 1 router classifies the pattern, Stage 2 specialist drills in with only the tools that pattern needs.\n"
            f"Every Claude call is cached in scout.db.api_cache; set REPLAY_MODE=1 to run from cache only.\n"
            f"Override the bar with SCOUT_SUCCESS_TARGET (e.g. 99.5).[/dim]",
            title="[bold]Block 6 — Triage agent[/bold]",
            border_style="magenta",
        )
    )

    triage_prompt = (
        f"The reliability bar is {SUCCESS_TARGET}% success. "
        "Investigate any gap to that bar — even a single failure deserves a cause. "
        "Pick the most concerning failing target and walk the evidence."
    )
    recommendation = run_triage(triage_prompt)

    # -----------------------------------------------------------------------
    # Block 7 — Recommendation (Scout calls it in; never executes)
    # -----------------------------------------------------------------------
    # OSS Scout pass the LLM's recommendation through in plan-shape so the FSM,
    # audit log, and Slack code downstream all see the same dict. We deliberately
    # do NOT translate to wire-format API calls — that's TableauOps Autopilot's
    # job, and shipping it here would leak REST endpoints into the audit log /
    # Slack notification.
    _ACTIONABLE = {
        "retry_refresh", "embed_credentials", "approve_schedule_change",
        "flag_drift_for_owner", "notify_owner",
    }
    _rec   = recommendation or {}
    _action = _rec.get("action") or "no_action"
    plan = {
        "action":      _action,
        "target_name": _rec.get("target_name"),
        "reason":      _rec.get("reason"),
        "confidence":  _rec.get("confidence"),
        "executable":  _action in _ACTIONABLE and bool(_rec.get("target_name")),
        "blockers":    [],
        "rest_call":   None,
        "tsc_call":    None,
    }

    rt = Table.grid(padding=(0, 2))
    rt.add_column(style="dim")
    rt.add_column()
    rt.add_row("action",     plan["action"])
    rt.add_row("target",     plan["target_name"] or "(none)")
    rt.add_row("confidence", plan["confidence"]  or "-")
    rt.add_row("reason",     plan["reason"]      or "-")

    border = "yellow" if plan["executable"] else "red"
    title  = "[bold]Block 7 — Recommendation[/bold]  [dim](Scout never executes — call-in only)[/dim]"
    console.print(Panel(rt, title=title, border_style=border))

    if plan["blockers"]:
        console.print("[red bold]Blockers (would prevent paid Autopilot execution):[/red bold]")
        for b in plan["blockers"]:
            console.print(f"  • {b}")
    elif plan["executable"]:
        console.print("[yellow]This recommendation is actionable. Scout will call it in (autonomy 2) or stay silent (autonomy 1).[/yellow]")

    # -----------------------------------------------------------------------
    # Block 7.5 — Incident FSM: open/transition the case Scout is working
    # -----------------------------------------------------------------------
    from db import (
        audit_write,
        connect,
        open_incident,
        set_incident_status,
        close_incident,
        get_open_incidents,
        get_recent_incidents,
    )

    SIGNAL_FOR_ACTION = {
        "retry_refresh":           "failure_streak",
        "embed_credentials":       "missing_embed",
        "approve_schedule_change": "schedule_drift",
        "flag_drift_for_owner":    "schedule_drift",
        "notify_owner":            "manual",
    }
    SEVERITY_FOR_CONFIDENCE = {"high": "critical", "medium": "warn", "low": "info"}

    incident_id: int | None = None
    if plan.get("executable") and plan.get("target_name"):
        signal   = SIGNAL_FOR_ACTION.get(plan["action"], "manual")
        severity = SEVERITY_FOR_CONFIDENCE.get((plan.get("confidence") or "").lower(), "warn")
        incident_id = open_incident(
            target_name=plan["target_name"],
            signal=signal,
            severity=severity,
            notes=f"agent recommended {plan['action']!r}: {plan.get('reason') or '(no reason)'}",
        )
        set_incident_status(incident_id, "fix_proposed", notes=f"plan ready (autonomy={AUTONOMY_LABEL})")
        console.print(
            Panel.fit(
                f"[cyan]incident[/cyan]=#{incident_id}  "
                f"[cyan]signal[/cyan]={signal}  "
                f"[cyan]severity[/cyan]={severity}  "
                f"[cyan]status[/cyan]=fix_proposed\n"
                f"[dim]scout.db • table: incidents[/dim]",
                title="[bold]Block 7.5 — Incident opened[/bold]",
                border_style="cyan",
            )
        )

    # -----------------------------------------------------------------------
    # Block 8 — Audit log (always logs the dry-run plan)
    # -----------------------------------------------------------------------
    audit_id = audit_write(
        actor="agent",
        action=plan["action"],
        target=plan["target_name"] or None,
        dry_run=True,
        payload={**plan, "incident_id": incident_id},
        autonomy_level=AUTONOMY,
        result="planned (dry-run)",
    )

    mode_note = {
        1: "[dim]plan-only mode[/dim] (Block 9 will skip the call-in)",
        2: "[bold cyan]notify-owner mode[/bold cyan] (Block 9 will Slack the owner with diagnosis + UI fix steps)",
    }[AUTONOMY]
    console.print(
        Panel.fit(
            f"[green]audit_id[/green]={audit_id}  "
            f"[cyan]dry_run[/cyan]=Y  "
            f"[cyan]actor[/cyan]=agent\n"
            f"{mode_note}\n"
            f"[dim]scout.db • table: audit_log[/dim]",
            title="[bold]Block 8 — Audit log[/bold]",
            border_style="green",
        )
    )

    # -----------------------------------------------------------------------
    # Block 9 — Execute remediation per autonomy level
    # -----------------------------------------------------------------------
    executed = False

    # Scout's only outbound write is the Slack call-in. Any actual Tableau
    # mutation (retry_refresh, embed_credentials, approve_schedule_change live
    # in TableauOps Autopilot — a separate paid product that subscribes to
    # Scout's recommendations and executes the safe ones. See
    # https://tableauops.com/scout for details.

    def _post_owner_slack(plan: dict, kind: str) -> tuple[bool, str]:
        """Build a rich Slack message (diagnosis + UI fix steps) and post.

        Falls back to a console panel when SLACK_WEBHOOK_URL is unset, so a
        demo / dev run is still readable.
        """
        import slack
        from remediation import fix_steps_for, build_owner_summary
        from tableau_tools import get_datasource_owner

        target_name = plan["target_name"]
        owner_mention = None
        try:
            owner = get_datasource_owner(target_name) or {}
            who = owner.get("name") or owner.get("email")
            if who:
                owner_mention = f"owner: *{who}*"
        except Exception:
            pass  # owner lookup is best-effort

        diagnosis = build_owner_summary(plan)
        fix_steps = fix_steps_for(plan)
        autonomy_note = (
            f"Sent by Scout at autonomy level {AUTONOMY} ({AUTONOMY_LABEL}). "
            f"Scout watches and calls in; it never mutates Tableau. "
            f"For automated remediation see TableauOps Autopilot — https://tableauops.com/scout"
        )

        text = f"Scout {kind}: {target_name} — {plan.get('reason') or '(no reason)'}"
        blocks = slack.build_owner_blocks(
            target_name=target_name,
            diagnosis=diagnosis,
            fix_steps=fix_steps,
            owner_mention=owner_mention,
            autonomy_note=autonomy_note,
        )
        return slack.post_message(text, blocks)

    if not plan["executable"]:
        console.print("[dim]Block 9: plan is not executable — skipping.[/dim]")
    elif AUTONOMY == 1:
        console.print("[dim]Block 9: autonomy=1 (plan-only) — skipping execution and notification.[/dim]")
    elif AUTONOMY == 2:
        # Notify-owner mode: any executable plan is rerouted to a Slack post
        # with diagnosis + direct fix steps. We do NOT execute the underlying
        # remediation. Owner takes it from here.
        console.print(
            Panel.fit(
                f"[bold cyan]Notify-owner mode[/bold cyan] — rerouting [white]{plan['action']}[/white] on "
                f"[white]{plan['target_name']}[/white] to a Slack post; not executing.",
                title="[bold]Block 9 — Notification[/bold]",
                border_style="cyan",
            )
        )
        try:
            ok, result_str = _post_owner_slack(plan, kind="notify_owner")
            color = "green" if ok else "yellow"
            glyph = "✓" if ok else "•"
            console.print(f"[{color}]{glyph} {result_str}[/{color}]")
            notify_audit_id = audit_write(
                actor=f"agent({AUTONOMY_LABEL})",
                action="notify_owner",
                target=plan["target_name"],
                dry_run=False,
                payload={**plan, "rerouted_from": plan["action"], "incident_id": incident_id},
                autonomy_level=AUTONOMY,
                result=result_str,
            )
            # Incident stays OPEN — owner closes it externally. Just note the handoff.
            if incident_id is not None and ok:
                set_incident_status(incident_id, "fix_proposed", notes=f"delegated to owner via Slack (audit#{notify_audit_id})")
            executed = ok
        except Exception as e:
            err = f"error: {type(e).__name__}: {str(e)[:200]}"
            console.print(f"[red]✗ {err}[/red]")
            audit_write(actor=f"agent({AUTONOMY_LABEL})", action="notify_owner", target=plan["target_name"], dry_run=False, payload={**plan, "incident_id": incident_id}, autonomy_level=AUTONOMY, result=err)
    # No `else` — autonomy is hard-capped at MAX_AUTONOMY (2). Execution paths
    # for confirm-each / auto-safe / full-autonomy live in the paid TableauOps
    # Autopilot product, not in OSS Scout.

    # -----------------------------------------------------------------------
    # Block 10 — Reproducibility recap (audit log + incidents + cache stats)
    # -----------------------------------------------------------------------
    from db import get_recent_audit, api_cache_stats

    incident_rows = get_recent_incidents(limit=8)
    if incident_rows:
        it = Table(title="Recent incidents (FSM)", show_header=True, header_style="bold")
        it.add_column("id",       justify="right")
        it.add_column("opened")
        it.add_column("target")
        it.add_column("signal")
        it.add_column("sev", justify="center")
        it.add_column("status")
        it.add_column("res audit", justify="right")
        for r in incident_rows:
            sev = r["severity"] or ""
            sev_color = "red" if sev == "critical" else "yellow" if sev == "warn" else "dim"
            status = r["status"] or ""
            status_color = (
                "green" if status == "resolved" else
                "yellow" if status in ("fix_proposed", "applying") else
                "red"   if status == "abandoned" else
                "cyan"
            )
            it.add_row(
                str(r["id"]),
                (r["opened_at"] or "")[:19],
                (r["target_name"] or "")[:30],
                r["signal"] or "",
                f"[{sev_color}]{sev}[/{sev_color}]",
                f"[{status_color}]{status}[/{status_color}]",
                str(r["resolution_audit_id"]) if r["resolution_audit_id"] is not None else "-",
            )
        console.print(it)

    audit_rows = get_recent_audit(limit=8)
    if audit_rows:
        at = Table(title="Recent audit log entries", show_header=True, header_style="bold")
        at.add_column("id",      justify="right")
        at.add_column("ts")
        at.add_column("actor")
        at.add_column("auto", justify="center")
        at.add_column("action")
        at.add_column("target")
        at.add_column("dry", justify="center")
        at.add_column("result")
        for r in audit_rows:
            auto = r["autonomy_level"]
            at.add_row(
                str(r["id"]),
                (r["ts"] or "")[:19],
                r["actor"] or "",
                str(auto) if auto is not None else "-",
                r["action"] or "",
                (r["target"] or "")[:30] or "-",
                "Y" if r["dry_run"] else "N",
                (r["result"] or "")[:50],
            )
        console.print(at)

    cache_stats = api_cache_stats()
    sources = "\n".join(f"  [dim]{s['source']}[/dim]: {s['n']}" for s in cache_stats["by_source"]) or "  [dim](empty)[/dim]"
    console.print(
        Panel.fit(
            f"[cyan]api_cache[/cyan] entries: [bold]{cache_stats['total']}[/bold]\n"
            f"{sources}\n\n"
            f"[dim]Replay this entire run with REPLAY_MODE=1 (cache must be populated).\n"
            f"Repo + scout.db = the demo. Anyone can clone and reproduce.[/dim]",
            title="[bold]Block 10 — Reproducibility[/bold]",
            border_style="green" if executed else "cyan",
        )
    )

    # ----- "What now?" — post-run guidance ---------------------------------
    next_steps = Table.grid(padding=(0, 2))
    next_steps.add_column(style="bold cyan", no_wrap=True)
    next_steps.add_column()
    next_steps.add_row("Replay this run",  "[cyan]REPLAY_MODE=1 python agent.py --autonomy 2[/cyan] — re-run from cache, no Anthropic credits used")
    if not os.environ.get("SLACK_WEBHOOK_URL"):
        next_steps.add_row("Wire Slack",   "Set SLACK_WEBHOOK_URL in .env — the next autonomy-2 run will post the diagnosis to your channel")
    if AUTONOMY == 1:
        next_steps.add_row("Notify owners","Re-run with [cyan]--autonomy 2[/cyan] — Scout will Slack the right human with diagnosis + UI fix steps")
    next_steps.add_row("Close the loop",  "[link=https://tableauops.com/scout?utm_source=cli]TableauOps Autopilot[/link] — actually fix what Scout finds, plus the dashboard, fleet view, and persistent baselines")
    console.print(Panel(next_steps, title="[bold]What now?[/bold]", border_style="cyan"))

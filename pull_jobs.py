"""Standalone job-history pull. Use to backfill scout.db without running the
full agent.py block sequence.

Usage:
  python pull_jobs.py                # delta pull (skip job_ids already in DB)
  python pull_jobs.py --reset        # wipe refresh_history + target_baselines, then full pull
  python pull_jobs.py --limit 500    # cap how many new jobs to ingest in this run

Captures from each Pager item: id, type, dates, finish_code, notes, and
subtitle/title (used as target_name). No per-job get_by_id calls — those hit
the rate-limited jobs/list bucket.
"""
import os
import sys
from datetime import datetime, timezone

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
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from db import (
    count_failures,
    count_jobs,
    existing_job_ids,
    init_db,
    reset_refresh_data,
    upsert_job,
)

load_dotenv()
console = Console()

PAT_NAME   = os.environ["TABLEAU_PAT_NAME"]
PAT_SECRET = os.environ["TABLEAU_PAT_SECRET"]
SITE       = os.environ["TABLEAU_SITE"]
SERVER_URL = os.environ["TABLEAU_SERVER"]


def parse_args() -> dict:
    args = {"reset": False, "limit": None}
    if "--reset" in sys.argv:
        args["reset"] = True
    if "--limit" in sys.argv:
        i = sys.argv.index("--limit")
        if i + 1 < len(sys.argv):
            args["limit"] = int(sys.argv[i + 1])
    return args


def normalize_notes(raw_notes) -> str | None:
    if raw_notes is None:
        return None
    if isinstance(raw_notes, str):
        return raw_notes
    try:
        return "\n".join(
            getattr(n, "text", None) or getattr(n, "value", None) or str(n)
            for n in raw_notes
        )
    except TypeError:
        return str(raw_notes)


def _print_preflight(args: dict) -> None:
    plan = Table.grid(padding=(0, 2))
    plan.add_column(style="bold cyan", no_wrap=True)
    plan.add_column()
    plan.add_row("Will do",
                 "Page Tableau's [cyan]jobs/list[/cyan] endpoint and write each new job into [cyan]scout.db.refresh_history[/cyan]. "
                 "[bold]Read-only on Tableau[/bold] — no refresh triggers, no mutations.")
    plan.add_row("Reads",   f"Tableau ([dim]{SERVER_URL} · site={SITE}[/dim])")
    plan.add_row("Writes",  "scout.db (local SQLite) — refresh_history table" + (", and clears existing rows first" if args["reset"] else ""))
    if args["limit"]:
        plan.add_row("Limit",  f"stop after {args['limit']:,} new rows this run")
    if args["reset"]:
        plan.add_row("[yellow]--reset[/yellow]", "[yellow]about to wipe refresh_history + target_baselines and re-pull from scratch[/yellow]")
    plan.add_row("Skips",   "job_ids already in scout.db (delta pull) — safe to re-run")
    plan.add_row("Note",    "[dim]Pager rows are sparse (no finish_code/target_name). Subsequent agent.py runs backfill them incrementally.[/dim]")
    console.print(Panel(plan, title="[bold]pull_jobs[/bold] [dim]· about to run[/dim]", border_style="cyan"))


def main():
    args = parse_args()
    _print_preflight(args)
    init_db()

    if args["reset"]:
        reset_refresh_data()
        console.print("[yellow]--reset: wiped refresh_history and target_baselines[/yellow]")

    auth   = TSC.PersonalAccessTokenAuth(PAT_NAME, PAT_SECRET, site_id=SITE)
    server = TSC.Server(SERVER_URL, use_server_version=True)
    server.http_options.update({"timeout": (10, 30)})

    def _sign_in_cm(srv, creds, attempts=3, backoff_s=2.0):
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
                console.print(f"[yellow]sign_in attempt {i}/{attempts} hit {msg}; retrying in {backoff_s:.0f}s[/yellow]")
                _time.sleep(backoff_s)

    with _sign_in_cm(server, auth):
        # Force version re-probe; see note in agent.py.
        try:
            server.use_server_version()
        except Exception:
            pass
        console.print(f"[green]signed_in[/green] site={SITE}  site_id={server.site_id}  api={server.version}")

        known = existing_job_ids()
        pulled_at = datetime.now(timezone.utc).isoformat()

        pulled = 0
        skipped = 0
        rate_limited = False
        last_subtitle = None

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            TextColumn("•"),
            TextColumn("[bold green]new={task.fields[pulled]}"),
            TextColumn("•"),
            TextColumn("[dim]skipped={task.fields[skipped]}"),
            TextColumn("•"),
            TextColumn("[dim]last={task.fields[last]}"),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        ) as progress:
            task = progress.add_task("Paging jobs", pulled=0, skipped=0, last="-")

            try:
                for job in TSC.Pager(server.jobs):
                    jt = (job.type or "").lower()
                    if "refresh" not in jt and "extract" not in jt:
                        continue

                    if job.id in known:
                        skipped += 1
                        progress.update(task, skipped=skipped)
                        continue

                    started   = getattr(job, "started_at", None)
                    completed = getattr(job, "completed_at", None)
                    duration  = None
                    if started and completed:
                        duration = int((completed - started).total_seconds())

                    target_name = getattr(job, "subtitle", None) or getattr(job, "title", None)
                    last_subtitle = (target_name or "")[:40]

                    upsert_job({
                        "job_id":       job.id,
                        "job_type":     job.type,
                        "target_type":  None,
                        "target_id":    None,
                        "target_name":  target_name,
                        "created_at":   job.created_at.isoformat()  if job.created_at  else None,
                        "started_at":   started.isoformat()          if started          else None,
                        "completed_at": completed.isoformat()        if completed        else None,
                        "duration_sec": duration,
                        "finish_code":  getattr(job, "finish_code", None),
                        "notes":        normalize_notes(getattr(job, "notes", None)),
                        "site_id":      server.site_id,
                        "pulled_at":    pulled_at,
                        "raw_xml":      None,
                    })

                    pulled += 1
                    progress.update(task, pulled=pulled, last=last_subtitle)

                    if args["limit"] and pulled >= args["limit"]:
                        console.print(f"[yellow]--limit {args['limit']} reached, stopping.[/yellow]")
                        break

            except TSC.ServerResponseError as e:
                if "429" in str(e):
                    rate_limited = True
                    msg = str(e).strip().split("\n")[-1].strip()
                    console.print(f"[yellow]Rate limited at pulled={pulled}: {msg}[/yellow]")
                else:
                    raise

    total    = count_jobs()
    failures = count_failures()
    rl_note  = "  [yellow](rate limited)[/yellow]" if rate_limited else ""
    console.print(
        Panel.fit(
            f"[green]new[/green]={pulled}  "
            f"[dim]skipped[/dim]={skipped}  "
            f"[cyan]total_in_db[/cyan]={total}  "
            f"[red]failures[/red]={failures}{rl_note}\n"
            f"[dim]scout.db • table: refresh_history[/dim]",
            title="[bold]pull_jobs.py — done[/bold]",
            border_style="green" if not rate_limited else "yellow",
        )
    )


if __name__ == "__main__":
    main()

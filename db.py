import json
import os
import sqlite3
import statistics
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "scout.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS refresh_history (
  job_id        TEXT PRIMARY KEY,
  job_type      TEXT,
  target_type   TEXT,
  target_id     TEXT,
  target_name   TEXT,
  created_at    TEXT,
  started_at    TEXT,
  completed_at  TEXT,
  duration_sec  INTEGER,
  finish_code   INTEGER,
  notes         TEXT,
  site_id       TEXT,
  pulled_at     TEXT NOT NULL,
  raw_xml       TEXT
);
CREATE INDEX IF NOT EXISTS idx_rh_target   ON refresh_history(target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_rh_complete ON refresh_history(completed_at);
CREATE INDEX IF NOT EXISTS idx_rh_finish   ON refresh_history(finish_code);

CREATE TABLE IF NOT EXISTS api_cache (
  cache_key   TEXT PRIMARY KEY,
  source      TEXT NOT NULL,         -- 'tsc' | 'anthropic'
  request     TEXT NOT NULL,         -- JSON
  response    TEXT NOT NULL,         -- JSON
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS targets (
  task_id                   TEXT PRIMARY KEY,
  target_type               TEXT NOT NULL,
  target_id                 TEXT NOT NULL,
  target_name               TEXT,
  task_type                 TEXT,
  schedule_summary          TEXT,
  priority                  INTEGER,
  consecutive_failed_count  INTEGER,
  last_run_at               TEXT,
  pulled_at                 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_targets_target ON targets(target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_targets_failing ON targets(consecutive_failed_count DESC);

CREATE TABLE IF NOT EXISTS target_baselines (
  target_name          TEXT PRIMARY KEY,
  total_runs           INTEGER NOT NULL,
  success_runs         INTEGER NOT NULL,
  failure_runs         INTEGER NOT NULL,
  success_rate         REAL,
  median_duration_sec  REAL,
  p95_duration_sec     REAL,
  mean_duration_sec    REAL,
  stdev_duration_sec   REAL,
  first_seen           TEXT,
  last_seen            TEXT,
  computed_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bl_runs ON target_baselines(total_runs DESC);

CREATE TABLE IF NOT EXISTS audit_log (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts              TEXT NOT NULL,
  actor           TEXT NOT NULL,         -- 'agent' | 'user'
  action          TEXT NOT NULL,         -- e.g. 'retry_refresh'
  target          TEXT,
  dry_run         INTEGER NOT NULL,      -- 0/1
  payload         TEXT,                  -- JSON
  result          TEXT,
  autonomy_level  INTEGER                -- 1-4; null on legacy rows
);

CREATE TABLE IF NOT EXISTS incidents (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  target_name           TEXT NOT NULL,
  signal                TEXT NOT NULL,         -- 'failure_streak' | 'schedule_drift' | 'missing_embed' | 'overdue' | 'manual'
  severity              TEXT NOT NULL,         -- 'info' | 'warn' | 'critical'
  status                TEXT NOT NULL,         -- 'open' | 'triaging' | 'fix_proposed' | 'applying' | 'resolved' | 'abandoned'
  opened_at             TEXT NOT NULL,
  updated_at            TEXT NOT NULL,
  closed_at             TEXT,
  resolution_audit_id   INTEGER,                -- FK to audit_log.id of the closing event
  notes                 TEXT
);
CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status, opened_at DESC);
CREATE INDEX IF NOT EXISTS idx_incidents_target ON incidents(target_name, opened_at DESC);

CREATE TABLE IF NOT EXISTS target_schedule_baselines (
  target_name           TEXT PRIMARY KEY,
  typical_hour_utc      INTEGER,         -- mode of hour-of-day across completed_at
  typical_interval_sec  REAL,            -- median inter-arrival
  hour_stdev            REAL,            -- spread of hour-of-day
  sample_size           INTEGER,
  computed_at           TEXT NOT NULL,
  approved_at           TEXT,
  approved_by           TEXT
);
"""


def connect() -> sqlite3.Connection:
    if os.environ.get("NODE_ENV") == "production":
        raise PermissionError("Direct SQLite access is disabled in production. Use Neon-backed runtime paths.")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        # Idempotent migration for older audit_log without autonomy_level
        cols = {row[1] for row in conn.execute("PRAGMA table_info(audit_log)").fetchall()}
        if "autonomy_level" not in cols:
            conn.execute("ALTER TABLE audit_log ADD COLUMN autonomy_level INTEGER")


def upsert_job(row: dict) -> None:
    """Insert OR enrich an existing row.

    Important: on conflict we COALESCE rather than overwrite — different code
    paths supply different subsets of fields (Pager has ended_at + started_at;
    get_by_id adds finish_code + datasource_id + completed_at + notes), so we
    never want a sparse follow-up call to wipe out richer data already stored.
    pulled_at and target_name are exceptions: a fresh value should win when
    supplied, since they're meaningful "latest known" markers.
    """
    cols = ",".join(row.keys())
    placeholders = ",".join(f":{k}" for k in row.keys())

    OVERWRITE_FIELDS = {"pulled_at", "target_name", "target_id", "target_type"}
    update_clauses = []
    for k in row.keys():
        if k == "job_id":
            continue
        if k in OVERWRITE_FIELDS:
            update_clauses.append(f"{k} = COALESCE(excluded.{k}, {k})")  # latest non-null wins
        else:
            update_clauses.append(f"{k} = COALESCE(excluded.{k}, {k})")  # never wipe with NULL
    updates = ",".join(update_clauses)
    sql = (
        f"INSERT INTO refresh_history ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(job_id) DO UPDATE SET {updates}"
    )
    with connect() as conn:
        conn.execute(sql, row)


def jobs_needing_hydration(limit: int = 200) -> list[str]:
    """Returns job_ids that are missing finish_code or target_name — candidates
    for a get_by_id pass. Newest started_at first."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT job_id FROM refresh_history "
            "WHERE finish_code IS NULL OR target_name IS NULL "
            "ORDER BY started_at DESC NULLS LAST "
            "LIMIT ?",
            (limit,),
        ).fetchall()
    return [r[0] for r in rows]


def count_jobs() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM refresh_history").fetchone()[0]


def existing_job_ids() -> set[str]:
    with connect() as conn:
        return {r[0] for r in conn.execute("SELECT job_id FROM refresh_history")}


def reset_refresh_data() -> None:
    with connect() as conn:
        conn.execute("DELETE FROM refresh_history")
        conn.execute("DELETE FROM target_baselines")


def upsert_target(row: dict) -> None:
    cols = ",".join(row.keys())
    placeholders = ",".join(f":{k}" for k in row.keys())
    updates = ",".join(f"{k}=excluded.{k}" for k in row.keys() if k != "task_id")
    sql = (
        f"INSERT INTO targets ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(task_id) DO UPDATE SET {updates}"
    )
    with connect() as conn:
        conn.execute(sql, row)


def count_targets() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM targets").fetchone()[0]


def current_failure_streaks(limit: int = 20, min_streak: int = 1) -> list[dict]:
    """Per target_name, count consecutive failures going backwards from most
    recent run. Cloud doesn't expose consecutive_failed_count, so we derive it
    from refresh_history."""
    with connect() as conn:
        rows = conn.execute("""
            SELECT target_name, finish_code, completed_at
            FROM refresh_history
            WHERE target_name IS NOT NULL AND completed_at IS NOT NULL
            ORDER BY target_name, completed_at DESC
        """).fetchall()

    streaks: dict[str, dict] = {}
    last_target = None
    for r in rows:
        name = r["target_name"]
        if name != last_target:
            streaks[name] = {"target_name": name, "streak": 0, "last_failure_at": None, "last_run_at": r["completed_at"]}
            last_target = name
        s = streaks[name]
        if s["streak"] == s.get("streak_locked", -1):
            continue
        if r["finish_code"] != 0:
            s["streak"] += 1
            s["last_failure_at"] = r["completed_at"]
        else:
            s["streak_locked"] = s["streak"]  # success terminates the streak

    out = [s for s in streaks.values() if s["streak"] >= min_streak]
    out.sort(key=lambda x: (-x["streak"], x["last_run_at"] or ""))
    return out[:limit]


def overdue_targets(limit: int = 20, overdue_factor: float = 2.0) -> list[dict]:
    """Per target_name, compute median inter-arrival time from history; flag
    targets whose time-since-last-run exceeds median * overdue_factor."""
    with connect() as conn:
        rows = conn.execute("""
            SELECT target_name, completed_at
            FROM refresh_history
            WHERE target_name IS NOT NULL AND completed_at IS NOT NULL
            ORDER BY target_name, completed_at
        """).fetchall()
        now_iso = conn.execute("SELECT datetime('now')").fetchone()[0]

    by_target: dict[str, list[str]] = {}
    for r in rows:
        by_target.setdefault(r["target_name"], []).append(r["completed_at"])

    now_dt = datetime.fromisoformat(now_iso.replace(" ", "T")).replace(tzinfo=timezone.utc)
    out = []
    for name, times in by_target.items():
        if len(times) < 3:
            continue
        intervals = []
        for a, b in zip(times, times[1:]):
            try:
                ai = datetime.fromisoformat(a.replace("Z", "+00:00"))
                bi = datetime.fromisoformat(b.replace("Z", "+00:00"))
                intervals.append((bi - ai).total_seconds())
            except (ValueError, AttributeError):
                continue
        if not intervals:
            continue
        median_interval = statistics.median(intervals)
        try:
            last_dt = datetime.fromisoformat(times[-1].replace("Z", "+00:00"))
        except ValueError:
            continue
        gap_sec = (now_dt - last_dt).total_seconds()
        if gap_sec > median_interval * overdue_factor:
            out.append({
                "target_name":          name,
                "last_run_at":          times[-1],
                "median_interval_sec":  median_interval,
                "gap_sec":              gap_sec,
                "overdue_ratio":        gap_sec / median_interval if median_interval else None,
                "run_count":            len(times),
            })
    out.sort(key=lambda x: -x["overdue_ratio"])
    return out[:limit]


# ---------------------------------------------------------------------------
# Schedule baselines + drift detection
# ---------------------------------------------------------------------------
def _hour_of_iso(ts: str) -> int | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).hour
    except (ValueError, AttributeError):
        return None


def _mode(values: list[int]) -> int | None:
    if not values:
        return None
    counts: dict[int, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return max(counts.items(), key=lambda kv: (kv[1], -kv[0]))[0]


def compute_schedule_baselines(min_runs: int = 5) -> int:
    """For each target with >= min_runs successful completions, compute the
    typical hour-of-day (mode) + typical inter-arrival (median seconds)
    + hour stdev. Writes target_schedule_baselines table.

    OSS Scout caps the learning window at the last 7 days. Persistent
    cross-month learning is a paid TableauOps Autopilot feature.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT target_name, completed_at FROM refresh_history "
            "WHERE target_name IS NOT NULL AND completed_at IS NOT NULL AND finish_code = 0 "
            "  AND completed_at >= datetime('now', '-7 days') "
            "ORDER BY target_name, completed_at"
        ).fetchall()

    by_target: dict[str, list[str]] = {}
    for r in rows:
        by_target.setdefault(r["target_name"], []).append(r["completed_at"])

    computed_at = datetime.now(timezone.utc).isoformat()
    written = 0
    with connect() as conn:
        for name, times in by_target.items():
            if len(times) < min_runs:
                continue
            hours = [h for h in (_hour_of_iso(t) for t in times) if h is not None]
            if len(hours) < min_runs:
                continue
            typical_hour = _mode(hours)
            hour_stdev   = statistics.pstdev(hours) if len(hours) > 1 else 0.0

            intervals = []
            for a, b in zip(times, times[1:]):
                try:
                    ai = datetime.fromisoformat(a.replace("Z", "+00:00"))
                    bi = datetime.fromisoformat(b.replace("Z", "+00:00"))
                    intervals.append((bi - ai).total_seconds())
                except ValueError:
                    continue
            typical_interval = statistics.median(intervals) if intervals else None

            conn.execute(
                "INSERT INTO target_schedule_baselines "
                "(target_name, typical_hour_utc, typical_interval_sec, hour_stdev, sample_size, computed_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(target_name) DO UPDATE SET "
                "  typical_hour_utc=excluded.typical_hour_utc, "
                "  typical_interval_sec=excluded.typical_interval_sec, "
                "  hour_stdev=excluded.hour_stdev, "
                "  sample_size=excluded.sample_size, "
                "  computed_at=excluded.computed_at",
                (name, typical_hour, typical_interval, hour_stdev, len(hours), computed_at),
            )
            written += 1
    return written


def get_schedule_baseline(target_name: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM target_schedule_baselines WHERE target_name = ?",
            (target_name,),
        ).fetchone()
    return dict(row) if row else None


def approve_schedule_baseline(target_name: str, approved_by: str) -> bool:
    """Mark the current baseline as approved (the drift IS the new normal).
    Recomputes the baseline first so the approval reflects recent runs."""
    compute_schedule_baselines(min_runs=3)  # force a recompute that pulls in recent drift
    with connect() as conn:
        cur = conn.execute(
            "UPDATE target_schedule_baselines "
            "SET approved_at = ?, approved_by = ? WHERE target_name = ?",
            (datetime.now(timezone.utc).isoformat(), approved_by, target_name),
        )
        return cur.rowcount > 0


def schedule_drift_candidates(
    limit: int = 10,
    recent_n: int = 5,
    hour_drift_threshold: float = 2.0,
    interval_drift_threshold: float = 0.5,
) -> list[dict]:
    """Compare the last `recent_n` runs of each target against its stored
    baseline. Flag if recent hour-of-day differs from baseline by more than
    `hour_drift_threshold` hours, OR if the recent median inter-arrival
    differs from baseline by more than `interval_drift_threshold` (relative).

    Returns rows: {target_name, baseline_hour, recent_hour, baseline_interval_h,
                   recent_interval_h, drift_kind, sample_size}.
    """
    with connect() as conn:
        baselines = {
            r["target_name"]: dict(r)
            for r in conn.execute("SELECT * FROM target_schedule_baselines").fetchall()
        }
        if not baselines:
            return []
        rows = conn.execute(
            "SELECT target_name, completed_at FROM refresh_history "
            "WHERE target_name IS NOT NULL AND completed_at IS NOT NULL AND finish_code = 0 "
            "ORDER BY target_name, completed_at DESC"
        ).fetchall()

    out: list[dict] = []
    seen: dict[str, list[str]] = {}
    for r in rows:
        name = r["target_name"]
        if name not in baselines:
            continue
        if seen.get(name, []) and len(seen[name]) >= recent_n:
            continue
        seen.setdefault(name, []).append(r["completed_at"])

    for name, times in seen.items():
        if len(times) < min(3, recent_n):
            continue
        bl = baselines[name]
        if bl["approved_at"]:
            continue   # owner already approved current pattern → not drift

        recent_hours = [h for h in (_hour_of_iso(t) for t in times) if h is not None]
        recent_hour = _mode(recent_hours) if recent_hours else None

        baseline_hour = bl["typical_hour_utc"]
        hour_drift = None
        if baseline_hour is not None and recent_hour is not None:
            # circular distance on 24h clock
            d = abs(baseline_hour - recent_hour)
            hour_drift = min(d, 24 - d)

        # recent inter-arrival (oldest-first within the recent slice)
        times_asc = list(reversed(times))
        intervals = []
        for a, b in zip(times_asc, times_asc[1:]):
            try:
                ai = datetime.fromisoformat(a.replace("Z", "+00:00"))
                bi = datetime.fromisoformat(b.replace("Z", "+00:00"))
                intervals.append((bi - ai).total_seconds())
            except ValueError:
                continue
        recent_interval = statistics.median(intervals) if intervals else None

        baseline_interval = bl["typical_interval_sec"]
        interval_drift_ratio = None
        if baseline_interval and recent_interval is not None and baseline_interval > 0:
            interval_drift_ratio = abs(recent_interval - baseline_interval) / baseline_interval

        kinds = []
        if hour_drift is not None and hour_drift > hour_drift_threshold:
            kinds.append("hour")
        if interval_drift_ratio is not None and interval_drift_ratio > interval_drift_threshold:
            kinds.append("interval")
        if not kinds:
            continue

        out.append({
            "target_name":            name,
            "baseline_hour":          baseline_hour,
            "recent_hour":            recent_hour,
            "baseline_interval_h":    (baseline_interval / 3600) if baseline_interval else None,
            "recent_interval_h":      (recent_interval / 3600) if recent_interval is not None else None,
            "hour_drift":             hour_drift,
            "interval_drift_ratio":   interval_drift_ratio,
            "drift_kind":             "+".join(kinds),
            "sample_size":            len(times),
            "baseline_sample_size":   bl["sample_size"],
        })

    out.sort(key=lambda x: ((x["hour_drift"] or 0) + (x["interval_drift_ratio"] or 0)), reverse=True)
    return out[:limit]


def count_failures(since_iso: str | None = None) -> int:
    sql = "SELECT COUNT(*) FROM refresh_history WHERE finish_code != 0"
    args: tuple = ()
    if since_iso:
        sql += " AND completed_at >= ?"
        args = (since_iso,)
    with connect() as conn:
        return conn.execute(sql, args).fetchone()[0]


def get_recent_failures(window_h: int = 24, limit: int = 25) -> list[dict]:
    """Hydrated failure rows within a recent window. Most recent first.

    Why this exists: current_failure_streaks() only surfaces *consecutive*
    failures from each target's most recent run. A target that fails once
    every few hours but auto-recovers shows zero streak and is invisible to
    the agent's failure-list view. This query enumerates those scattered
    one-off failures so the agent can pick a job_id and pull notes via
    get_failed_job_details.

    Notes are truncated to ~200 chars for context efficiency; the agent can
    fetch the full string per-job via get_failed_job_details(job_id).
    """
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_h)).isoformat()
    with connect() as conn:
        rows = conn.execute("""
            SELECT
              job_id,
              target_name,
              completed_at,
              duration_sec,
              finish_code,
              SUBSTR(notes, 1, 200) AS notes_excerpt
            FROM refresh_history
            WHERE finish_code IS NOT NULL
              AND finish_code != 0
              AND completed_at >= ?
            ORDER BY completed_at DESC
            LIMIT ?
        """, (cutoff, limit)).fetchall()
    return [dict(r) for r in rows]


def health_summary() -> dict:
    """Health stats restricted to jobs with a known outcome (finish_code IS NOT NULL).

    Why: TSC.Pager returns sparse parent/container rows with NULL finish_code that
    inflate the denominator and crater success_rate. The signal we want is over
    rows we have outcome data for.
    """
    with connect() as conn:
        all_count = conn.execute("SELECT COUNT(*) FROM refresh_history").fetchone()[0] or 0

        row = conn.execute("""
            SELECT
              COUNT(*)                                          AS total,
              SUM(CASE WHEN finish_code = 0 THEN 1 ELSE 0 END)  AS successes,
              SUM(CASE WHEN finish_code != 0 THEN 1 ELSE 0 END) AS failures,
              AVG(duration_sec)                                 AS avg_duration_sec,
              MAX(duration_sec)                                 AS max_duration_sec,
              MIN(completed_at)                                 AS earliest,
              MAX(completed_at)                                 AS latest
            FROM refresh_history
            WHERE finish_code IS NOT NULL
        """).fetchone()

        median_row = conn.execute("""
            SELECT duration_sec FROM refresh_history
            WHERE finish_code IS NOT NULL AND duration_sec IS NOT NULL
            ORDER BY duration_sec
            LIMIT 1 OFFSET (
              SELECT COUNT(*) / 2 FROM refresh_history
              WHERE finish_code IS NOT NULL AND duration_sec IS NOT NULL
            )
        """).fetchone()

        last_24h = conn.execute("""
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN finish_code != 0 THEN 1 ELSE 0 END) AS failures
            FROM refresh_history
            WHERE finish_code IS NOT NULL
              AND completed_at >= datetime('now', '-1 day')
        """).fetchone()

        last_7d = conn.execute("""
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN finish_code != 0 THEN 1 ELSE 0 END) AS failures
            FROM refresh_history
            WHERE finish_code IS NOT NULL
              AND completed_at >= datetime('now', '-7 days')
        """).fetchone()

        by_type = conn.execute("""
            SELECT
              job_type,
              COUNT(*) AS total,
              SUM(CASE WHEN finish_code != 0 THEN 1 ELSE 0 END) AS failed,
              AVG(duration_sec) AS avg_sec
            FROM refresh_history
            WHERE finish_code IS NOT NULL
            GROUP BY job_type
            ORDER BY total DESC
        """).fetchall()

    total      = row["total"] or 0
    successes  = row["successes"] or 0
    return {
        "total":               total,
        "all_rows":            all_count,
        "unhydrated":          all_count - total,
        "successes":           successes,
        "failures":            row["failures"] or 0,
        "success_rate":        (successes / total) if total else 0,
        "avg_duration_sec":    row["avg_duration_sec"],
        "median_duration_sec": median_row["duration_sec"] if median_row else None,
        "max_duration_sec":    row["max_duration_sec"],
        "earliest":            row["earliest"],
        "latest":              row["latest"],
        "last_24h_total":      last_24h["total"] or 0,
        "last_24h_failures":   last_24h["failures"] or 0,
        "last_7d_total":       last_7d["total"] or 0,
        "last_7d_failures":    last_7d["failures"] or 0,
        "by_job_type":         [dict(r) for r in by_type],
    }


def _percentile(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def compute_baselines(min_runs: int = 3) -> int:
    """Recompute baselines for every target_name with at least `min_runs` runs.
    Duration stats use ONLY successful runs (failures often have anomalous durations).

    OSS Scout caps the learning window at the last 7 days. Persistent
    cross-month learning is a paid TableauOps Autopilot feature.
    """
    with connect() as conn:
        rows = conn.execute("""
            SELECT target_name, finish_code, duration_sec, completed_at
            FROM refresh_history
            WHERE target_name IS NOT NULL
              AND completed_at >= datetime('now', '-7 days')
        """).fetchall()

    by_target: dict[str, list[dict]] = {}
    for r in rows:
        by_target.setdefault(r["target_name"], []).append(dict(r))

    computed_at = datetime.now(timezone.utc).isoformat()
    written = 0
    with connect() as conn:
        conn.execute("DELETE FROM target_baselines")
        for name, runs in by_target.items():
            total = len(runs)
            if total < min_runs:
                continue

            successes = [r for r in runs if r["finish_code"] == 0]
            failures  = [r for r in runs if r["finish_code"] not in (0, None)]

            success_durations = sorted(
                r["duration_sec"] for r in successes
                if r["duration_sec"] is not None and r["duration_sec"] >= 0
            )

            if success_durations:
                median = statistics.median(success_durations)
                mean   = statistics.fmean(success_durations)
                stdev  = statistics.pstdev(success_durations) if len(success_durations) > 1 else 0.0
                p95    = _percentile(success_durations, 0.95)
            else:
                median = mean = stdev = p95 = None

            completed_times = sorted(r["completed_at"] for r in runs if r["completed_at"])
            first_seen = completed_times[0]  if completed_times else None
            last_seen  = completed_times[-1] if completed_times else None

            conn.execute("""
                INSERT INTO target_baselines (
                    target_name, total_runs, success_runs, failure_runs, success_rate,
                    median_duration_sec, p95_duration_sec, mean_duration_sec, stdev_duration_sec,
                    first_seen, last_seen, computed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                name, total, len(successes), len(failures), len(successes) / total,
                median, p95, mean, stdev,
                first_seen, last_seen, computed_at,
            ))
            written += 1
    return written


def get_baselines(limit: int = 20, order_by: str = "total_runs DESC") -> list[dict]:
    allowed = {
        "total_runs DESC",
        "success_rate ASC",
        "p95_duration_sec DESC",
        "median_duration_sec DESC",
        "last_seen DESC",
    }
    if order_by not in allowed:
        order_by = "total_runs DESC"
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM target_baselines ORDER BY {order_by} LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_target_history(target_name: str, limit: int = 20) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("""
            SELECT job_id, completed_at, finish_code, duration_sec, notes
            FROM refresh_history
            WHERE target_name = ?
            ORDER BY completed_at DESC
            LIMIT ?
        """, (target_name, limit)).fetchall()
    return [dict(r) for r in rows]


def get_baseline_for_target(target_name: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM target_baselines WHERE target_name = ?",
            (target_name,),
        ).fetchone()
    return dict(row) if row else None


def api_cache_get(cache_key: str) -> str | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT response FROM api_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
    return row["response"] if row else None


def api_cache_put(cache_key: str, source: str, request: str, response: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO api_cache (cache_key, source, request, response, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (cache_key, source, request, response, datetime.now(timezone.utc).isoformat()),
        )


def audit_write(
    actor: str,
    action: str,
    target: str | None,
    dry_run: bool,
    payload: dict,
    autonomy_level: int | None = None,
    result: str | None = None,
) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO audit_log (ts, actor, action, target, dry_run, payload, result, autonomy_level) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                actor,
                action,
                target,
                1 if dry_run else 0,
                json.dumps(payload, default=str),
                result,
                autonomy_level,
            ),
        )
        return cur.lastrowid


# ---------------------------------------------------------------------------
# Incident FSM
#
# An incident represents a single ops "case" Scout is working: a target with a
# failure streak, a drifted schedule, a missing-embed credential issue, etc.
# States: open → triaging → fix_proposed → applying → resolved | abandoned.
#
# `incidents` is the current-state view (UPDATE-able). Every status change is
# also logged to `audit_log` (append-only) via audit_write — so the audit log
# remains the immutable source of truth for the transition history.
# ---------------------------------------------------------------------------
INCIDENT_STATES = {"open", "triaging", "fix_proposed", "applying", "resolved", "abandoned"}


def get_open_incident_for_target(target_name: str, signal: str | None = None) -> dict | None:
    """Returns the most recent non-closed incident for this target (optionally
    filtered by signal), or None."""
    sql = (
        "SELECT * FROM incidents "
        "WHERE target_name = ? AND status NOT IN ('resolved','abandoned') "
    )
    args: tuple = (target_name,)
    if signal:
        sql += " AND signal = ?"
        args = (target_name, signal)
    sql += " ORDER BY id DESC LIMIT 1"
    with connect() as conn:
        row = conn.execute(sql, args).fetchone()
    return dict(row) if row else None


def open_incident(
    target_name: str,
    signal: str,
    severity: str = "warn",
    notes: str | None = None,
) -> int:
    """Open a new incident, OR return the existing open incident's id if one
    already exists for this target+signal. Idempotent."""
    existing = get_open_incident_for_target(target_name, signal=signal)
    if existing:
        return existing["id"]
    now = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO incidents (target_name, signal, severity, status, opened_at, updated_at, notes) "
            "VALUES (?, ?, ?, 'open', ?, ?, ?)",
            (target_name, signal, severity, now, now, notes),
        )
        return cur.lastrowid


def set_incident_status(incident_id: int, status: str, notes: str | None = None) -> bool:
    """Transition an incident to a new state. No-op if status invalid."""
    if status not in INCIDENT_STATES:
        return False
    now = datetime.now(timezone.utc).isoformat()
    sql = "UPDATE incidents SET status = ?, updated_at = ?"
    args: list = [status, now]
    if status in ("resolved", "abandoned"):
        sql += ", closed_at = ?"
        args.append(now)
    if notes is not None:
        sql += ", notes = COALESCE(notes || char(10), '') || ?"
        args.append(notes)
    sql += " WHERE id = ?"
    args.append(incident_id)
    with connect() as conn:
        cur = conn.execute(sql, args)
        return cur.rowcount > 0


def close_incident(incident_id: int, resolution_audit_id: int | None, status: str = "resolved", notes: str | None = None) -> bool:
    """Close an incident, linking the audit row that closed it."""
    if status not in ("resolved", "abandoned"):
        return False
    now = datetime.now(timezone.utc).isoformat()
    sql = "UPDATE incidents SET status = ?, updated_at = ?, closed_at = ?, resolution_audit_id = ?"
    args: list = [status, now, now, resolution_audit_id]
    if notes is not None:
        sql += ", notes = COALESCE(notes || char(10), '') || ?"
        args.append(notes)
    sql += " WHERE id = ?"
    args.append(incident_id)
    with connect() as conn:
        cur = conn.execute(sql, args)
        return cur.rowcount > 0


def get_open_incidents(limit: int = 20) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM incidents "
            "WHERE status NOT IN ('resolved','abandoned') "
            "ORDER BY "
            "  CASE severity WHEN 'critical' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END, "
            "  opened_at DESC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_recent_incidents(limit: int = 20) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM incidents ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def audit_update_result(row_id: int, result: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE audit_log SET result = ? WHERE id = ?", (result, row_id))


def get_recent_audit(limit: int = 20) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def api_cache_stats() -> dict:
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM api_cache").fetchone()[0]
        by_source = conn.execute(
            "SELECT source, COUNT(*) AS n FROM api_cache GROUP BY source"
        ).fetchall()
    return {"total": total, "by_source": [dict(r) for r in by_source]}


def top_failing_targets(limit: int = 10) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("""
            SELECT
              target_name,
              COUNT(*) AS runs,
              SUM(CASE WHEN finish_code != 0 THEN 1 ELSE 0 END) AS failed
            FROM refresh_history
            WHERE target_name IS NOT NULL
            GROUP BY target_name
            HAVING failed > 0
            ORDER BY failed DESC, runs DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]

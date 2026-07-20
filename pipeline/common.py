"""Shared utilities for the niche-scanner pipeline.

Windows 11 compatible: pathlib throughout, explicit utf-8 encoding on every
file read/write, no POSIX-only calls (no os.fork, no unix sockets, etc).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
REPORTS_DIR = BASE_DIR / "reports"
SCRIPTS_DIR = BASE_DIR / "scripts"
PROMPTS_DIR = BASE_DIR / "prompts"

NICHES_PATH = BASE_DIR / "niches.yaml"
RPM_TABLE_PATH = BASE_DIR / "rpm_table.yaml"
CONFIG_PATH = BASE_DIR / "config.yaml"
BUILD_LOG_PATH = BASE_DIR / "BUILD_LOG.yaml"


def today_str() -> str:
    return date.today().isoformat()


def parse_dt(s: str) -> datetime:
    # YouTube API timestamps are RFC3339, e.g. 2025-03-01T12:00:00Z. Shared
    # by score.py (channel/video age) and scan_trending.py (video velocity)
    # so both parse YouTube timestamps identically.
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def run_dir(run_date: str) -> Path:
    d = DATA_DIR / run_date
    d.mkdir(parents=True, exist_ok=True)
    return d


def source_dir(run_date: str, source: str) -> Path:
    """e.g. data/2026-07-15/youtube"""
    d = run_dir(run_date) / source
    d.mkdir(parents=True, exist_ok=True)
    return d


def niche_source_dir(run_date: str, source: str, slug: str) -> Path:
    """e.g. data/2026-07-15/youtube/music-theory-shorts/"""
    d = source_dir(run_date, source) / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path(run_date: str) -> Path:
    return run_dir(run_date) / "scanner.db"


def load_yaml(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_niches(filter_slugs: list[str] | None = None) -> list[dict]:
    niches = load_yaml(NICHES_PATH) or []
    if filter_slugs:
        wanted = set(filter_slugs)
        niches = [n for n in niches if n["slug"] in wanted]
        found = {n["slug"] for n in niches}
        missing = wanted - found
        if missing:
            raise ValueError(f"Unknown niche slug(s) not in niches.yaml: {sorted(missing)}")
    return niches


def load_rpm_table() -> dict:
    return load_yaml(RPM_TABLE_PATH) or {}


def load_config() -> dict:
    return load_yaml(CONFIG_PATH) or {}


def load_build_log() -> list[dict]:
    """Real, chronological (oldest first) project history: bugs found,
    fixes made, milestones reached, each with a real commit hash. Exists
    so anything that wants genuine narrative material -- with actual
    stakes, not an aggregate metric -- has somewhere real to pull it from.
    See produce_script.py, which grounds generated scripts in the most
    recent entries alongside score.py/scan_trending.py data."""
    return load_yaml(BUILD_LOG_PATH) or []


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def read_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


SCHEMA = """
CREATE TABLE IF NOT EXISTS niches (
    slug TEXT PRIMARY KEY,
    label TEXT,
    rpm_category TEXT,
    my_expertise TEXT
);

CREATE TABLE IF NOT EXISTS channels (
    channel_id TEXT NOT NULL,
    niche_slug TEXT NOT NULL,
    title TEXT,
    published_at TEXT,
    subscriber_count INTEGER,
    video_count INTEGER,
    view_count INTEGER,
    sampled_total_views INTEGER,
    PRIMARY KEY (channel_id, niche_slug)
);

CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    niche_slug TEXT NOT NULL,
    title TEXT,
    published_at TEXT,
    view_count INTEGER,
    description TEXT,
    PRIMARY KEY (video_id, niche_slug)
);

CREATE TABLE IF NOT EXISTS trends (
    niche_slug TEXT NOT NULL,
    term TEXT NOT NULL,
    date TEXT NOT NULL,
    interest REAL,
    PRIMARY KEY (niche_slug, term, date)
);

CREATE TABLE IF NOT EXISTS reddit (
    niche_slug TEXT PRIMARY KEY,
    subreddit TEXT,
    subscribers INTEGER,
    posts_per_day REAL
);

CREATE TABLE IF NOT EXISTS scores (
    niche_slug TEXT PRIMARY KEY,
    breakout_rate REAL,
    capture_index REAL,
    trend_slope REAL,
    velocity REAL,
    sponsor_density REAL,
    rpm_low REAL,
    rpm_high REAL,
    upload_burden REAL,
    policy_risk REAL,
    composite REAL,
    rank INTEGER,
    is_negative_control INTEGER,
    computed_at TEXT
);

CREATE TABLE IF NOT EXISTS quota_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT,
    source TEXT,
    niche_slug TEXT,
    units INTEGER,
    method TEXT,
    logged_at TEXT,
    real_date TEXT
);

CREATE TABLE IF NOT EXISTS run_meta (
    run_date TEXT PRIMARY KEY,
    status TEXT,
    note TEXT,
    updated_at TEXT
);
"""


def connect_db(run_date: str) -> sqlite3.Connection:
    """Open (and idempotently initialize) scanner.db for a given run date."""
    conn = sqlite3.connect(db_path(run_date))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    _migrate_quota_log_method(conn)
    _migrate_quota_log_real_date(conn)
    return conn


def _migrate_quota_log_method(conn: sqlite3.Connection) -> None:
    """quota_log predates the method column (added when YouTube's two-bucket
    quota model -- search.list on its own 100-calls/day cap, separate from
    the general 10k-unit pool -- required tracking search.list calls apart
    from everything else). Add the column to older DBs, and backfill rows
    logged before it existed: every search.list call was logged as its own
    row with units == COST_SEARCH_LIST (100), a value no other call type
    produces (channels/playlistItems/videos batches never rack up that many
    units in one logged row), so units == 100 reliably identifies a
    pre-migration search row."""
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(quota_log)").fetchall()]
    if "method" not in cols:
        conn.execute("ALTER TABLE quota_log ADD COLUMN method TEXT")
        conn.commit()
    conn.execute("UPDATE quota_log SET method = 'search' WHERE method IS NULL AND units = 100")
    conn.execute("UPDATE quota_log SET method = 'general' WHERE method IS NULL")
    conn.commit()


def _migrate_quota_log_real_date(conn: sqlite3.Connection) -> None:
    """quota_log predates the real_date column, added once it became clear
    that Google's actual 100-calls/day cap resets on real calendar time --
    not on this project's run_date label, which is a data-folder identifier
    deliberately decoupled from real time (e.g. a batch ingest resumed
    across several real days under one frozen run_date, or a lighter scan
    script run on a genuinely different real day while pointed at an older
    run_date for data continuity). Add the column to older DBs, and
    backfill existing rows from logged_at's date portion -- logged_at is
    already a real UTC timestamp recorded at insert time, unlike run_date --
    rather than from run_date, since backfilling from run_date would just
    recreate the exact bug this migration exists to fix."""
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(quota_log)").fetchall()]
    if "real_date" not in cols:
        conn.execute("ALTER TABLE quota_log ADD COLUMN real_date TEXT")
        conn.commit()
    conn.execute("UPDATE quota_log SET real_date = substr(logged_at, 1, 10) WHERE real_date IS NULL AND logged_at IS NOT NULL")
    conn.commit()


def log_quota(conn: sqlite3.Connection, run_date: str, source: str, niche_slug: str, units: int, method: str) -> None:
    """run_date is the data-folder label this call is attributed to (which
    niche batch/continuity run it belongs to). real_date is the actual
    system date at logging time -- independent of run_date, and NOT taken
    from the run_date parameter -- because that's what quota enforcement
    (check_youtube_quota / count_quota_calls / sum_quota) keys on: Google's
    real 100-calls/day cap resets on real calendar time, not on this
    project's internal run_date labeling."""
    import datetime as _dt
    now = _dt.datetime.utcnow()
    conn.execute(
        "INSERT INTO quota_log (run_date, source, niche_slug, units, method, logged_at, real_date) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (run_date, source, niche_slug, units, method, now.isoformat(), now.date().isoformat()),
    )
    conn.commit()


def sum_quota(conn: sqlite3.Connection, real_date: str, source: str, exclude_method: str | None = None) -> int:
    """Total units already logged for this real_date + source (e.g. prior
    ingest runs earlier today, in real calendar time), so a fresh estimate
    can be checked against the threshold as a running daily total rather
    than in isolation. Filters on real_date, not run_date -- Google's quota
    resets on real calendar time regardless of which data-folder label a
    call happened to be attributed to. Pass exclude_method to omit a bucket
    (e.g. 'search') that's tracked against its own separate cap rather than
    this unit total."""
    if exclude_method is None:
        row = conn.execute(
            "SELECT COALESCE(SUM(units), 0) AS total FROM quota_log WHERE real_date = ? AND source = ?",
            (real_date, source),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COALESCE(SUM(units), 0) AS total FROM quota_log "
            "WHERE real_date = ? AND source = ? AND method IS NOT ?",
            (real_date, source, exclude_method),
        ).fetchone()
    return row["total"]


def count_quota_calls(conn: sqlite3.Connection, real_date: str, source: str, method: str) -> int:
    """Number of logged calls (rows, not units) for this real_date + source +
    method -- used for caps that limit call count rather than unit spend
    (search.list's separate 100-calls/day cap). Filters on real_date, not
    run_date, for the same reason as sum_quota above."""
    row = conn.execute(
        "SELECT COUNT(*) AS total FROM quota_log WHERE real_date = ? AND source = ? AND method = ?",
        (real_date, source, method),
    ).fetchone()
    return row["total"]


def check_youtube_quota(
    conn: sqlite3.Connection,
    run_date: str,
    cfg: dict,
    search_calls_needed: int,
    general_units_needed: int,
    confirm: bool,
    log: logging.Logger,
) -> bool:
    """Shared gate for YouTube's two independent quota buckets: search.list's
    own 100-calls/day cap (call-counted) and the general channels/
    playlistItems/videos.list unit pool (quota_abort_threshold). Originally
    lived inline in ingest_youtube.py's main(); extracted here so every
    script that spends YouTube quota (ingest_youtube.py, scan_trending.py)
    calls this one function, instead of each keeping its own copy of the
    threshold logic that could silently drift out of sync with the other.

    The check is keyed on today's real calendar date (real_date), NOT on
    run_date -- run_date is accepted only so it can be included in the log
    message for context (which data-folder batch this call belongs to).
    Google's 100-calls/day cap resets on real calendar time regardless of
    this project's internal run_date labeling, so a search.list call made
    by either script today counts against the same daily cap the other
    checks today, even if they're pointed at different (or the same,
    reused-across-many-real-days) run_date folders.

    Returns True if the run should proceed, False if it should abort (the
    caller decides how to abort -- sys.exit, raise, etc).
    """
    real_date = date.today().isoformat()
    already_search_calls = count_quota_calls(conn, real_date, "youtube", "search")
    already_general_units = sum_quota(conn, real_date, "youtube", exclude_method="search")

    projected_search_calls = search_calls_needed + already_search_calls
    projected_general_units = general_units_needed + already_general_units

    search_cap = cfg["youtube"]["search_list_daily_cap"]
    general_threshold = cfg["youtube"]["quota_abort_threshold"]

    log.info(
        "Projected for this run (run_date=%s, real calendar date=%s): %d search.list calls "
        "(%d already used today, cap %d/day) | ~%d general-pool units (%d already logged "
        "today, running total ~%d, threshold %d)",
        run_date,
        real_date,
        search_calls_needed,
        already_search_calls,
        search_cap,
        general_units_needed,
        already_general_units,
        projected_general_units,
        general_threshold,
    )

    ok = True

    # Primary gate: search.list's own 100-calls/day cap. Independent of the
    # general unit pool below and, in practice, the one that actually binds.
    if projected_search_calls > search_cap and not confirm:
        log.error(
            "search.list cap exceeded: %d new + %d already used today = %d calls, "
            "over the %d/day cap. This does not share the general unit pool -- "
            "narrowing scope or waiting for the cap to reset is the only fix "
            "short of --confirm.",
            search_calls_needed,
            already_search_calls,
            projected_search_calls,
            search_cap,
        )
        ok = False

    # Secondary gate: the general unit pool (channels/playlistItems/videos).
    if projected_general_units > general_threshold and not confirm:
        log.error(
            "General-pool quota (~%d new + %d already logged today = ~%d) exceeds threshold (%d). "
            "Re-run with --confirm to proceed, or narrow the run's scope.",
            general_units_needed,
            already_general_units,
            projected_general_units,
            general_threshold,
        )
        ok = False

    return ok


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[%(asctime)s] %(name)s %(levelname)s: %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger

"""Shared utilities for the niche-scanner pipeline.

Windows 11 compatible: pathlib throughout, explicit utf-8 encoding on every
file read/write, no POSIX-only calls (no os.fork, no unix sockets, etc).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
REPORTS_DIR = BASE_DIR / "reports"
PROMPTS_DIR = BASE_DIR / "prompts"

NICHES_PATH = BASE_DIR / "niches.yaml"
RPM_TABLE_PATH = BASE_DIR / "rpm_table.yaml"
CONFIG_PATH = BASE_DIR / "config.yaml"


def today_str() -> str:
    return date.today().isoformat()


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
    logged_at TEXT
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


def log_quota(conn: sqlite3.Connection, run_date: str, source: str, niche_slug: str, units: int, method: str) -> None:
    import datetime as _dt
    conn.execute(
        "INSERT INTO quota_log (run_date, source, niche_slug, units, method, logged_at) VALUES (?, ?, ?, ?, ?, ?)",
        (run_date, source, niche_slug, units, method, _dt.datetime.utcnow().isoformat()),
    )
    conn.commit()


def sum_quota(conn: sqlite3.Connection, run_date: str, source: str, exclude_method: str | None = None) -> int:
    """Total units already logged for this run_date + source (e.g. prior
    ingest runs today), so a fresh estimate can be checked against the
    threshold as a running daily total rather than in isolation. Pass
    exclude_method to omit a bucket (e.g. 'search') that's tracked against
    its own separate cap rather than this unit total."""
    if exclude_method is None:
        row = conn.execute(
            "SELECT COALESCE(SUM(units), 0) AS total FROM quota_log WHERE run_date = ? AND source = ?",
            (run_date, source),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COALESCE(SUM(units), 0) AS total FROM quota_log "
            "WHERE run_date = ? AND source = ? AND method IS NOT ?",
            (run_date, source, exclude_method),
        ).fetchone()
    return row["total"]


def count_quota_calls(conn: sqlite3.Connection, run_date: str, source: str, method: str) -> int:
    """Number of logged calls (rows, not units) for this run_date + source +
    method -- used for caps that limit call count rather than unit spend
    (search.list's separate 100-calls/day cap)."""
    row = conn.execute(
        "SELECT COUNT(*) AS total FROM quota_log WHERE run_date = ? AND source = ? AND method = ?",
        (run_date, source, method),
    ).fetchone()
    return row["total"]


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[%(asctime)s] %(name)s %(levelname)s: %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger

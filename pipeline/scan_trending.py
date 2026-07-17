"""Ad hoc -- trend scan.

Finds videos breaking out *right now* within a small, hand-picked set of
niches (default: config.yaml's trend_scan.default_niches, the AI/automation-
adjacent ones), as opposed to score.py's full 23-niche, 12-month
breakout_rate metric. This is deliberately NOT a full re-score: no
channels.list, no playlistItems.list, no composite scoring -- just
search.list (order=date) per search term, videos.list for stats on the
results, and a robust per-niche outlier check on views/hour velocity.

Reuses ingest_youtube.py's low-level HTTP/retry/relevance-filter helpers
and common.py's shared quota gate rather than duplicating either. This
script draws down the exact same YOUTUBE_API_KEY quota (search.list's
100-calls/day cap, plus the general channels/playlistItems/videos.list unit
pool) as ingest_youtube.py, logged against the same scanner.db for the
given run_date -- so the two scripts share one real daily quota picture
instead of each keeping a separate counter that could drift out of sync
with the actual limit.

No resumability/--force here (unlike ingest_youtube.py): every invocation
spends fresh search.list + videos.list quota, by design -- this is meant to
be triggered deliberately, not re-run blindly.

Output: reports/trend-scan-<run_date>.md -- a shortlist table (built
directly in Python, not by the LLM, same principle as report.py's league
table) plus a single Claude synthesis call for concrete adaptation
suggestions, via report.py's own call_sonnet() so the fixed
thinking-disabled/max_tokens behavior is shared, not re-implemented.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import datetime, timezone
from statistics import median

import requests

from pipeline import common
from pipeline import ingest_youtube as iy
from pipeline.report import call_sonnet, estimate_tokens

log = common.get_logger("scan_trending")


def fmt_num(x, decimals=0):
    if x is None:
        return "n/a"
    return f"{x:.{decimals}f}"


def _md_escape(s: str | None) -> str:
    """Video titles/channel names can contain '|' or newlines, which would
    otherwise break the markdown table."""
    if not s:
        return ""
    return s.replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def estimate_quota(niches: list[dict], cfg: dict) -> dict:
    """Pre-run estimate for this script's two quota buckets. Unlike
    ingest_youtube.py's estimate_quota() (relevance + date search per term,
    plus channels.list/playlistItems.list/videos.list), this script only
    calls search.list once per term -- order=date, since recency is the
    point, not authority -- and videos.list for stats on the results. No
    channels.list or playlistItems.list calls at all."""
    tcfg = cfg["trend_scan"]
    videos_per_term = tcfg["videos_per_term"]

    total_search_calls = 0
    total_general_units = 0
    for n in niches:
        terms = n.get("search_terms", [])
        total_search_calls += len(terms)
        # Upper bound: no dedup assumed across a niche's terms, since the
        # true unique-video count is only known after searching.
        est_videos = len(terms) * videos_per_term
        total_general_units += math.ceil(est_videos / 50) * iy.COST_VIDEOS_LIST

    return {"total_search_calls": total_search_calls, "total_general_units": total_general_units}


def _hours_since(published_at: str, now: datetime) -> float:
    pub = common.parse_dt(published_at)
    return (now - pub).total_seconds() / 3600.0


def flag_outliers(candidates: list[dict], tcfg: dict) -> list[dict]:
    """Robust (median + MAD) outlier detection on views/hour velocity,
    scoped to this niche's own candidate pool only -- never compared
    across niches.

    MAD instead of mean/stdev for the same reason score.py's velocity
    metric moved to a log1p transform: a single freak viral video would
    otherwise inflate the "typical" baseline (mean) and blow out the spread
    (stdev), silently hiding itself -- and everything else -- as not-quite-
    an-outlier. Median and MAD barely move when one value is 50x the rest,
    so the freak video registers as the extreme outlier it actually is
    instead of resetting what "normal" looks like for the niche.

    A minimum absolute velocity floor also applies: a niche with almost no
    traffic can produce a video that's a robust-z outlier relative to its
    own near-zero baseline without being remotely "trending" in any real
    sense. And the whole shortlist is capped per niche so one freak video
    can't dominate the cross-niche picture fed to the synthesis call.
    """
    if not candidates:
        return []

    if len(candidates) < tcfg["min_sample_for_stats"]:
        # Too few recent videos for median/MAD to mean anything -- fall
        # back to a stricter absolute-only bar rather than asserting a
        # statistical signal the sample can't support.
        floor = tcfg["min_absolute_velocity"] * 3
        outliers = [dict(c, robust_z=None) for c in candidates if c["velocity"] >= floor]
        outliers.sort(key=lambda c: c["velocity"], reverse=True)
        return outliers[: tcfg["max_shortlist_per_niche"]]

    velocities = [c["velocity"] for c in candidates]
    med = median(velocities)
    mad = median([abs(v - med) for v in velocities])
    robust_scale = mad * 1.4826  # normal-consistent MAD scaling, comparable to a standard z-score

    scored = []
    for c in candidates:
        z = (c["velocity"] - med) / robust_scale if robust_scale > 0 else 0.0
        scored.append(dict(c, robust_z=z))

    outliers = [
        c for c in scored
        if c["robust_z"] >= tcfg["velocity_outlier_z"] and c["velocity"] >= tcfg["min_absolute_velocity"]
    ]
    outliers.sort(key=lambda c: c["velocity"], reverse=True)
    return outliers[: tcfg["max_shortlist_per_niche"]]


def collect_candidates(session: requests.Session, api_key: str, niche: dict, cfg: dict, run_date: str, conn) -> list[dict]:
    """search.list (order=date) per search term -> dedup video ids ->
    videos.list for stats -> per-niche robust-outlier detection."""
    slug = niche["slug"]
    tcfg = cfg["trend_scan"]
    now = datetime.now(timezone.utc)

    seen: dict[str, dict] = {}  # video_id -> snippet, deduped across this niche's terms
    for term in niche.get("search_terms", []):
        data = iy._get(
            session, "search",
            {
                "part": "snippet", "q": term, "type": "video", "order": "date",
                "maxResults": tcfg["videos_per_term"],
            },
            api_key,
        )
        common.log_quota(conn, run_date, "youtube", slug, iy.COST_SEARCH_LIST, method="search")
        for item in data.get("items", []):
            vid = item.get("id", {}).get("videoId")
            snippet = item.get("snippet", {})
            if not vid or vid in seen:
                continue
            if not iy._term_relevant(term, snippet.get("title", ""), snippet.get("description", "")):
                continue
            seen[vid] = snippet

    if not seen:
        return []

    video_ids = list(seen.keys())
    videos = iy.fetch_videos(session, api_key, video_ids)
    common.log_quota(
        conn, run_date, "youtube", slug,
        math.ceil(len(video_ids) / 50) * iy.COST_VIDEOS_LIST, method="videos",
    )

    window_hours = tcfg["recent_window_days"] * 24
    min_age_hours = tcfg["min_age_hours"]

    candidates = []
    for v in videos:
        snippet = v.get("snippet", {})
        pub = snippet.get("publishedAt")
        if not pub:
            continue
        age_hours = _hours_since(pub, now)
        if age_hours < min_age_hours or age_hours > window_hours:
            continue
        view_count = int(v.get("statistics", {}).get("viewCount", 0))
        candidates.append({
            "video_id": v.get("id"),
            "title": snippet.get("title"),
            "channel_title": snippet.get("channelTitle"),
            "published_at": pub,
            "age_hours": age_hours,
            "view_count": view_count,
            "velocity": view_count / age_hours,
        })

    return flag_outliers(candidates, tcfg)


def build_shortlist_table(niche_results: list[dict]) -> str:
    header = (
        "| Niche | Title | Channel | Views | Age (h) | Velocity (views/h) | Robust Z |\n"
        "|---|---|---|---|---|---|---|\n"
    )
    lines = []
    for nr in niche_results:
        if not nr["candidates"]:
            lines.append(f"| {nr['label']} | *(no breakout candidates this run)* | | | | | |")
            continue
        for c in nr["candidates"]:
            z = fmt_num(c["robust_z"], 2) if c["robust_z"] is not None else "n/a (small sample)"
            lines.append(
                f"| {nr['label']} | {_md_escape(c['title'])} | {_md_escape(c['channel_title'])} | "
                f"{fmt_num(c['view_count'])} | {fmt_num(c['age_hours'], 1)} | "
                f"{fmt_num(c['velocity'], 1)} | {z} |"
            )
    return header + "\n".join(lines)


def build_shortlist_summary_for_llm(niche_results: list[dict]) -> str:
    lines = []
    for nr in niche_results:
        lines.append(f"### {nr['label']} (slug: {nr['slug']})")
        if not nr["candidates"]:
            lines.append("No breakout candidates found in this run's window.")
            lines.append("")
            continue
        for c in nr["candidates"]:
            z = fmt_num(c["robust_z"], 2) if c["robust_z"] is not None else "n/a (sample too small for robust stats)"
            lines.append(
                f"- \"{c['title']}\" by {c['channel_title']} -- {fmt_num(c['view_count'])} views, "
                f"published {fmt_num(c['age_hours'], 1)}h ago, velocity={fmt_num(c['velocity'], 1)} views/hour, "
                f"robust_z={z}"
            )
        lines.append("")
    return "\n".join(lines)


def run(run_date: str, niches_filter: list[str] | None, confirm: bool) -> str:
    cfg = common.load_config()
    tcfg = cfg["trend_scan"]

    api_key = os.environ.get(cfg["youtube"]["api_key_env"])
    if not api_key:
        log.error("Set the %s environment variable before running.", cfg["youtube"]["api_key_env"])
        sys.exit(1)

    slugs = niches_filter or tcfg["default_niches"]
    niches = common.load_niches(slugs)

    conn = common.connect_db(run_date)

    estimate = estimate_quota(niches, cfg)
    ok = common.check_youtube_quota(
        conn, run_date, cfg,
        estimate["total_search_calls"], estimate["total_general_units"],
        confirm, log,
    )
    if not ok:
        conn.close()
        sys.exit(1)

    session = requests.Session()
    niche_results = []
    for niche in niches:
        try:
            candidates = collect_candidates(session, api_key, niche, cfg, run_date, conn)
        except iy.YouTubeError as e:
            log.error("Aborting on '%s': %s", niche["slug"], e)
            conn.close()
            sys.exit(1)
        niche_results.append({"slug": niche["slug"], "label": niche["label"], "candidates": candidates})
        log.info("  -> %s: %d breakout candidate(s)", niche["slug"], len(candidates))

    conn.close()

    total_candidates = sum(len(nr["candidates"]) for nr in niche_results)
    table = build_shortlist_table(niche_results)

    header = (
        f"# Trend Scan -- {run_date}\n\n"
        f"Scanned {len(niches)} niche(s): {', '.join(n['slug'] for n in niches)}. "
        f"{total_candidates} breakout candidate(s) found within the last "
        f"{tcfg['recent_window_days']} day(s)."
    )

    if total_candidates == 0:
        log.info("No breakout candidates found across any scanned niche -- skipping synthesis call.")
        report_md = (
            f"{header}\n\n"
            f"## Shortlist\n\n{table}\n\n"
            "No videos cleared the breakout-velocity bar in any scanned niche this run. "
            "Nothing to synthesize -- try again later, widen --niches, or loosen "
            "trend_scan.velocity_outlier_z / min_absolute_velocity in config.yaml if this "
            "keeps happening.\n"
        )
    else:
        system_prompt = (common.PROMPTS_DIR / "trend_synthesis.md").read_text(encoding="utf-8")
        summary = build_shortlist_summary_for_llm(niche_results)
        user_content = (
            f"Run date: {run_date}\n"
            f"Methodology: candidates are recent videos (published within "
            f"{tcfg['recent_window_days']} days, at least {tcfg['min_age_hours']} hours old) whose "
            f"views/hour velocity is a robust outlier (median + MAD, z >= {tcfg['velocity_outlier_z']}) "
            f"relative to other recent videos in the same niche, subject to a "
            f"{tcfg['min_absolute_velocity']} views/hour floor. Capped at "
            f"{tcfg['max_shortlist_per_niche']} candidates per niche.\n\n"
            f"Candidates by niche:\n{summary}"
        )

        est = estimate_tokens(system_prompt) + estimate_tokens(user_content)
        log.info("Estimated input tokens for the synthesis call: ~%d", est)

        log.info("Calling %s for trend synthesis...", cfg["anthropic"]["model"])
        narrative = call_sonnet(system_prompt, user_content, cfg)

        report_md = (
            f"{header}\n\n"
            f"## Shortlist\n\n{table}\n\n"
            f"{narrative.strip()}\n"
        )

    out_path = common.REPORTS_DIR / f"trend-scan-{run_date}.md"
    common.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report_md)
    log.info("Trend scan report written to %s", out_path)
    return str(out_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan for videos breaking out right now within a small niche set")
    parser.add_argument("--run-date", default=common.today_str())
    parser.add_argument(
        "--niches", default=None,
        help="Comma-separated slugs; defaults to config.yaml's trend_scan.default_niches",
    )
    parser.add_argument("--confirm", action="store_true", help="Proceed even if projected quota exceeds threshold")
    args = parser.parse_args()

    filter_slugs = args.niches.split(",") if args.niches else None
    run(args.run_date, filter_slugs, args.confirm)


if __name__ == "__main__":
    main()

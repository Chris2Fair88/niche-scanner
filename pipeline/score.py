"""Phase 2 -- Scoring. Pure computation, no LLM calls here.

Loads raw ingestion JSON (youtube/trends/reddit) into scanner.db, computes
the eight metrics from the task brief per niche, normalizes them across the
niche set, applies the configured weights + policy_risk multiplier, and
writes everything (raw + normalized + composite + rank) back to scanner.db.

Missing data (a niche with no channels found, no trends terms, no mapped
subreddit, etc.) is NOT silently averaged away: any missing scoring
component is treated as the worst case (normalized 0) for that niche, and
listed in the printed/report caveats, so an incomplete-data niche can never
float to a misleadingly good rank just because it has fewer numbers pulling
it down.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from datetime import date, datetime
from statistics import median

import numpy as np

from pipeline import common

log = common.get_logger("score")

SPONSOR_MARKERS = [
    "sponsored", "use code", "affiliate", "discount code", "#ad",
    "promo code", "promocode", "paid partnership", "link in description",
]
SYNTHETIC_VOICE_MARKERS = [
    "text to speech", "text-to-speech", "ai voice", "ai-generated voice",
    "ai generated voice", "voiceover generated", "synthetic voice",
]


def months_between(d1: datetime, d2: date) -> float:
    return (d2.year - d1.year) * 12 + (d2.month - d1.month) + (d2.day - d1.day) / 30.44


def load_niche_youtube(run_date: str, slug: str):
    d = common.niche_source_dir(run_date, "youtube", slug)
    channels_path, videos_path = d / "channels.json", d / "videos.json"
    if not channels_path.exists() or not videos_path.exists():
        return None, None
    return common.read_json(channels_path), common.read_json(videos_path)


def load_niche_trends(run_date: str, slug: str):
    p = common.source_dir(run_date, "trends") / f"{slug}.json"
    return common.read_json(p) if p.exists() else None


def load_niche_reddit(run_date: str, slug: str):
    p = common.source_dir(run_date, "reddit") / f"{slug}.json"
    return common.read_json(p) if p.exists() else None


def persist_raw(conn, slug: str, channels_json, videos_by_channel, trends_json, reddit_json):
    if channels_json:
        for ch in channels_json.get("top_channels", []):
            stats = ch.get("statistics", {})
            snip = ch.get("snippet", {})
            vids = (videos_by_channel or {}).get(ch["id"], [])
            sampled_total = sum(int(v.get("statistics", {}).get("viewCount", 0)) for v in vids)
            conn.execute(
                """INSERT OR REPLACE INTO channels
                   (channel_id, niche_slug, title, published_at, subscriber_count,
                    video_count, view_count, sampled_total_views)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ch["id"], slug, snip.get("title"), snip.get("publishedAt"),
                    int(stats.get("subscriberCount", 0)) if stats.get("subscriberCount") else None,
                    int(stats.get("videoCount", 0)) if stats.get("videoCount") else None,
                    int(stats.get("viewCount", 0)) if stats.get("viewCount") else None,
                    sampled_total,
                ),
            )
    if videos_by_channel:
        for cid, vids in videos_by_channel.items():
            for v in vids:
                snip = v.get("snippet", {})
                stats = v.get("statistics", {})
                conn.execute(
                    """INSERT OR REPLACE INTO videos
                       (video_id, channel_id, niche_slug, title, published_at, view_count, description)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        v["id"], cid, slug, snip.get("title"), snip.get("publishedAt"),
                        int(stats.get("viewCount", 0)) if stats.get("viewCount") else None,
                        snip.get("description"),
                    ),
                )
    if trends_json:
        for term, series in trends_json.items():
            for point in series:
                conn.execute(
                    "INSERT OR REPLACE INTO trends (niche_slug, term, date, interest) VALUES (?, ?, ?, ?)",
                    (slug, term, point["date"], point["interest"]),
                )
    if reddit_json:
        conn.execute(
            """INSERT OR REPLACE INTO reddit (niche_slug, subreddit, subscribers, posts_per_day)
               VALUES (?, ?, ?, ?)""",
            (slug, reddit_json.get("subreddit"), reddit_json.get("subscribers"), reddit_json.get("posts_per_day")),
        )
    conn.commit()


def compute_breakout_and_burden(channels_json, videos_by_channel, run_date: str, cfg: dict):
    if not channels_json or not channels_json.get("top_channels"):
        return None, None, []
    top_channels = channels_json["top_channels"]
    now = date.fromisoformat(run_date)
    window_months = cfg["youtube"]["breakout_window_months"]
    threshold = cfg["youtube"]["breakout_view_threshold"]
    # Diagnosed against real data (2026-07-15 run): every channel that ever
    # qualified as "breakout" across all 23 niches fell into one of two
    # clean clusters -- either 2 videos barely clearing the raw threshold
    # (1.2x-1.7x) or 7-8 videos dramatically clearing it (6x-42x), with
    # nothing in between. A qualifying-video-count threshold can't separate
    # these (both marginal cases already have 2 videos, not 1), but a
    # magnitude bar does, cleanly, on the full dataset -- not just the one
    # negative control it was diagnosed against.
    qualifying_multiplier = cfg["youtube"]["breakout_qualifying_multiplier"]
    qualifying_threshold = threshold * qualifying_multiplier

    breakout_count = 0
    successful_new_ids = []
    for ch in top_channels:
        published = ch.get("snippet", {}).get("publishedAt")
        if not published:
            continue
        age_months = months_between(common.parse_dt(published), now)
        is_new = age_months <= window_months
        vids = (videos_by_channel or {}).get(ch["id"], [])
        has_breakout = any(int(v.get("statistics", {}).get("viewCount", 0)) >= qualifying_threshold for v in vids)
        if is_new and has_breakout:
            breakout_count += 1
            successful_new_ids.append(ch["id"])

    breakout_rate = breakout_count / len(top_channels)

    # upload_burden: median uploads/month among those successful new channels
    rates = []
    for cid in successful_new_ids:
        vids = (videos_by_channel or {}).get(cid, [])
        dates = sorted(common.parse_dt(v["snippet"]["publishedAt"]) for v in vids if v.get("snippet", {}).get("publishedAt"))
        if len(dates) < 2:
            continue
        span_days = (dates[-1] - dates[0]).days
        span_months = span_days / 30.44
        if span_months <= 0:
            continue
        rates.append(len(dates) / span_months)
    upload_burden = median(rates) if rates else None

    return breakout_rate, upload_burden, successful_new_ids


def compute_capture_index(channels_json, videos_by_channel):
    if not channels_json or not channels_json.get("top_channels"):
        return None
    totals = []
    for ch in channels_json["top_channels"]:
        vids = (videos_by_channel or {}).get(ch["id"], [])
        totals.append(sum(int(v.get("statistics", {}).get("viewCount", 0)) for v in vids))
    grand_total = sum(totals)
    if grand_total == 0:
        return None
    top3 = sum(sorted(totals, reverse=True)[:3])
    return top3 / grand_total


def compute_velocity(videos_by_channel, run_date: str, cfg: dict):
    if not videos_by_channel:
        return None
    now = date.fromisoformat(run_date)
    window_days = cfg["youtube"]["velocity_window_days"]
    recent = []
    for vids in videos_by_channel.values():
        for v in vids:
            pub = v.get("snippet", {}).get("publishedAt")
            if not pub:
                continue
            if (now - common.parse_dt(pub).date()).days <= window_days:
                recent.append(int(v.get("statistics", {}).get("viewCount", 0)))
    return median(recent) if recent else None


def compute_sponsor_density(videos_by_channel):
    if not videos_by_channel:
        return None
    total = flagged = 0
    for vids in videos_by_channel.values():
        for v in vids:
            total += 1
            desc = (v.get("snippet", {}).get("description") or "").lower()
            if any(m in desc for m in SPONSOR_MARKERS):
                flagged += 1
    return flagged / total if total else None


def compute_policy_risk(videos_by_channel):
    if not videos_by_channel:
        return None
    all_videos = []
    titles_by_channel = defaultdict(list)
    for cid, vids in videos_by_channel.items():
        for v in vids:
            title = v.get("snippet", {}).get("title") or ""
            desc = v.get("snippet", {}).get("description") or ""
            all_videos.append((cid, title, desc))
            titles_by_channel[cid].append(title)

    if not all_videos:
        return None

    templated_channels = set()
    for cid, titles in titles_by_channel.items():
        if len(titles) < 3:
            continue
        normalized = [re.sub(r"\d+", "#", t.lower()).strip() for t in titles]
        unique_ratio = len(set(normalized)) / len(normalized)
        if unique_ratio <= 0.5:
            templated_channels.add(cid)

    flagged = 0
    for cid, title, desc in all_videos:
        is_synth = any(m in desc.lower() for m in SYNTHETIC_VOICE_MARKERS)
        if is_synth or cid in templated_channels:
            flagged += 1
    return flagged / len(all_videos)


def compute_trend_slope(trends_json, cfg: dict):
    if not trends_json:
        return None
    window_months = cfg["trends"]["slope_window_months"]
    monthly = defaultdict(list)
    for term, series in (trends_json or {}).items():
        for point in series:
            d = datetime.fromisoformat(point["date"])
            monthly[(d.year, d.month)].append(point["interest"])
    if not monthly:
        return None
    months_sorted = sorted(monthly.keys())
    avg_by_month = [sum(monthly[k]) / len(monthly[k]) for k in months_sorted]
    trailing = avg_by_month[-window_months:] if len(avg_by_month) > window_months else avg_by_month
    if len(trailing) < 2:
        return None
    xs = np.arange(len(trailing), dtype=float)
    ys = np.array(trailing, dtype=float)
    slope = float(np.polyfit(xs, ys, 1)[0])
    mean_y = float(ys.mean())
    if mean_y == 0:
        return 0.0
    return slope / mean_y  # fractional change per month relative to mean level


def minmax_normalize(values: dict) -> dict:
    """values: {slug: float or None}. Returns {slug: 0..1}, with None -> 0.0
    (worst case; see module docstring)."""
    present = [v for v in values.values() if v is not None]
    if not present:
        return {slug: 0.0 for slug in values}
    lo, hi = min(present), max(present)
    out = {}
    for slug, v in values.items():
        if v is None:
            out[slug] = 0.0
        elif hi == lo:
            out[slug] = 1.0  # all niches tied on this metric -> no discriminating info, don't zero everyone out
        else:
            out[slug] = (v - lo) / (hi - lo)
    return out


def minmax_normalize_log(values: dict) -> dict:
    """Same contract as minmax_normalize, but log1p-transforms present values
    first. Velocity-specific: plain min-max let a single extreme outlier
    (one niche's median recent views ~3x the next-highest) normalize to
    ~1.0 and compress every other niche's relative standing toward 0,
    letting that one niche's composite look artificially strong. log1p
    keeps "higher velocity is better" while shrinking the gap a single
    outlier can open up, without touching the other five metrics."""
    transformed = {slug: (np.log1p(v) if v is not None else None) for slug, v in values.items()}
    return minmax_normalize(transformed)


def policy_multiplier(risk_pct, cfg: dict) -> float:
    lo = cfg["policy_risk_multiplier"]["min"]
    hi = cfg["policy_risk_multiplier"]["max"]
    if risk_pct is None:
        return lo  # unknown risk -> treat conservatively, don't reward missing data
    risk_pct = max(0.0, min(1.0, risk_pct))
    return hi - risk_pct * (hi - lo)


def capture_multiplier(capture_index_pct, cfg: dict) -> float:
    """Same shape as policy_multiplier: a niche that's essentially owned by
    its top 3 channels gets its composite discounted, on top of (not
    instead of) whatever breakout_rate/velocity/etc already earned it --
    otherwise a monopoly niche with one lucky breakout video or high
    incumbent-driven velocity can outscore a genuinely open one."""
    lo = cfg["capture_index_multiplier"]["min"]
    hi = cfg["capture_index_multiplier"]["max"]
    if capture_index_pct is None:
        return lo  # unknown capture -> treat conservatively, don't reward missing data
    capture_index_pct = max(0.0, min(1.0, capture_index_pct))
    return hi - capture_index_pct * (hi - lo)


def run(run_date: str, niches_filter: list[str] | None = None) -> str:
    cfg = common.load_config()
    rpm_table = common.load_rpm_table()
    niches = common.load_niches(niches_filter)
    conn = common.connect_db(run_date)

    for n in niches:
        conn.execute(
            "INSERT OR REPLACE INTO niches (slug, label, rpm_category, my_expertise) VALUES (?, ?, ?, ?)",
            (n["slug"], n["label"], n["rpm_category"], n.get("my_expertise", "none")),
        )
    conn.commit()

    raw = {}
    warnings = defaultdict(list)
    for n in niches:
        slug = n["slug"]
        channels_json, videos_by_channel = load_niche_youtube(run_date, slug)
        trends_json = load_niche_trends(run_date, slug)
        reddit_json = load_niche_reddit(run_date, slug)
        persist_raw(conn, slug, channels_json, videos_by_channel, trends_json, reddit_json)

        if channels_json is None:
            warnings[slug].append("no YouTube data ingested")
        if trends_json is None:
            warnings[slug].append("no Trends data ingested")
        if not n.get("subreddits"):
            pass  # expected/optional per brief, not a warning
        elif reddit_json is None:
            warnings[slug].append("Reddit data not ingested despite mapped subreddit")

        breakout_rate, upload_burden, successful_new_ids = compute_breakout_and_burden(
            channels_json, videos_by_channel, run_date, cfg
        )
        capture_index = compute_capture_index(channels_json, videos_by_channel)
        velocity = compute_velocity(videos_by_channel, run_date, cfg)
        sponsor_density = compute_sponsor_density(videos_by_channel)
        policy_risk = compute_policy_risk(videos_by_channel)
        trend_slope = compute_trend_slope(trends_json, cfg)
        rpm_low, rpm_high = rpm_table.get(n["rpm_category"], rpm_table.get("_default", [1, 4]))

        for metric_name, val in [
            ("breakout_rate", breakout_rate), ("velocity", velocity),
            ("sponsor_density", sponsor_density), ("trend_slope", trend_slope),
            ("upload_burden", upload_burden),
        ]:
            if val is None:
                warnings[slug].append(f"missing metric: {metric_name}")

        raw[slug] = dict(
            breakout_rate=breakout_rate, capture_index=capture_index, trend_slope=trend_slope,
            velocity=velocity, sponsor_density=sponsor_density, rpm_low=rpm_low, rpm_high=rpm_high,
            upload_burden=upload_burden, policy_risk=policy_risk,
        )

    # --- normalize across the niche set ---
    norm_breakout = minmax_normalize({s: r["breakout_rate"] for s, r in raw.items()})
    norm_trend = minmax_normalize({s: r["trend_slope"] for s, r in raw.items()})
    norm_velocity = minmax_normalize_log({s: r["velocity"] for s, r in raw.items()})
    norm_sponsor = minmax_normalize({s: r["sponsor_density"] for s, r in raw.items()})
    norm_rpm = minmax_normalize({s: (r["rpm_low"] + r["rpm_high"]) / 2 for s, r in raw.items()})
    norm_upload_raw = minmax_normalize({s: r["upload_burden"] for s, r in raw.items()})
    # invert: lower upload_burden (more passive) should score higher. A niche
    # with no burden data got 0.0 from minmax_normalize already (worst case);
    # inverting a real 0.0 (lowest burden in the set) would wrongly reward it
    # with 1.0, so guard on the raw value being present before inverting.
    norm_upload = {
        s: (1.0 - norm_upload_raw[s]) if raw[s]["upload_burden"] is not None else 0.0
        for s in raw
    }

    w = cfg["weights"]
    composite = {}
    for slug in raw:
        pm = policy_multiplier(raw[slug]["policy_risk"], cfg)
        cm = capture_multiplier(raw[slug]["capture_index"], cfg)
        base = (
            w["breakout_rate"] * norm_breakout[slug]
            + w["trend_slope"] * norm_trend[slug]
            + w["velocity"] * norm_velocity[slug]
            + w["sponsor_density"] * norm_sponsor[slug]
            + w["rpm_range"] * norm_rpm[slug]
            + w["upload_burden"] * norm_upload[slug]
        )
        composite[slug] = base * pm * cm

    ranked = sorted(composite.keys(), key=lambda s: composite[s], reverse=True)
    rank_of = {slug: i + 1 for i, slug in enumerate(ranked)}

    negative_controls = set(cfg.get("negative_controls", []))
    n_total = len(ranked)
    half = n_total / 2
    offenders = [s for s in negative_controls if s in rank_of and rank_of[s] <= half]
    # Negative controls that aren't in `raw` at all weren't scored this run
    # (e.g. excluded by --niches, or dropped from niches.yaml) -- silently
    # skipping them in `offenders` would let the gate report "ok" having
    # never actually checked them, which is a false pass, not a real one.
    missing_controls = sorted(s for s in negative_controls if s not in raw)

    now_iso = datetime.utcnow().isoformat()
    for slug, r in raw.items():
        conn.execute(
            """INSERT OR REPLACE INTO scores
               (niche_slug, breakout_rate, capture_index, trend_slope, velocity, sponsor_density,
                rpm_low, rpm_high, upload_burden, policy_risk, composite, rank, is_negative_control, computed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                slug, r["breakout_rate"], r["capture_index"], r["trend_slope"], r["velocity"],
                r["sponsor_density"], r["rpm_low"], r["rpm_high"], r["upload_burden"], r["policy_risk"],
                composite[slug], rank_of[slug], 1 if slug in negative_controls else 0, now_iso,
            ),
        )

    if offenders:
        status = "suspect"
        note = f"Negative control(s) ranked in top half: {', '.join(offenders)}"
        log.warning("=" * 70)
        log.warning("VALIDATION GATE FAILED: %s", note)
        log.warning("This means the scorer is not distinguishing known-dead niches")
        log.warning("from viable ones. Do not trust this run's league table.")
        log.warning("=" * 70)
    else:
        status = "ok"
        note = "All negative controls ranked in the bottom half."
        log.info("Validation gate passed: %s", note)

    if missing_controls:
        incomplete_note = f"Negative control(s) never ingested/scored: {', '.join(missing_controls)}"
        log.warning("=" * 70)
        log.warning("VALIDATION GATE INCOMPLETE: %s", incomplete_note)
        log.warning("This is distinct from a FAILED gate -- it means these controls")
        log.warning("couldn't be checked at all this run, so the status above only")
        log.warning("reflects whichever negative controls WERE actually scored.")
        log.warning("=" * 70)
        if status == "ok":
            status = "incomplete"
            note = incomplete_note
        else:
            note = f"{note} | {incomplete_note}"

    if warnings:
        log.warning("Data completeness warnings:")
        for slug, msgs in warnings.items():
            for m in msgs:
                log.warning("  %s: %s", slug, m)

    conn.execute(
        "INSERT OR REPLACE INTO run_meta (run_date, status, note, updated_at) VALUES (?, ?, ?, ?)",
        (run_date, status, note, now_iso),
    )
    conn.commit()
    conn.close()
    log.info("Scoring complete for %d niches. Status: %s", n_total, status)
    return status


def main() -> None:
    parser = argparse.ArgumentParser(description="Score ingested niche data into scanner.db")
    parser.add_argument("--run-date", default=common.today_str())
    parser.add_argument("--niches", default=None, help="Comma-separated slugs to limit scoring")
    args = parser.parse_args()
    filter_slugs = args.niches.split(",") if args.niches else None
    status = run(args.run_date, filter_slugs)
    if status == "suspect":
        sys.exit(2)


if __name__ == "__main__":
    main()

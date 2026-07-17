"""Phase 1a -- YouTube ingestion.

For each niche: search.list (relevance + date) per search_term -> unique
channel ids -> channels.list for stats -> take top N channels -> pull each
channel's recent uploads via playlistItems.list -> videos.list for stats +
descriptions.

Raw JSON only, written to disk under data/{run_date}/youtube/{slug}/.
This script never writes to scanner.db except to log quota spend --
parsing JSON into the DB tables is score.py's job.

Uses the plain REST API via `requests` (no google-api-python-client
dependency). Quota costs below are the documented YouTube Data API v3 costs
as of this writing; Anthropic/Google may change them, so the estimate here
is a planning aid, not a guarantee -- the real usage is whatever the API
console reports.
"""
from __future__ import annotations

import argparse
import itertools
import math
import os
import re
import sys
import time
from typing import Any

import requests

from pipeline import common

log = common.get_logger("ingest_youtube")

API_BASE = "https://www.googleapis.com/youtube/v3"
COST_SEARCH_LIST = 100
COST_CHANNELS_LIST = 1
COST_PLAYLISTITEMS_LIST = 1
COST_VIDEOS_LIST = 1


class YouTubeError(RuntimeError):
    pass


_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "how",
    "in", "is", "it", "of", "on", "or", "that", "the", "this", "to", "why",
    "with",
}

# Generic YouTube title/format words -- cross-niche, not tied to any one
# search term. These describe *how* a video is packaged (a listicle, a
# tutorial, a "beginners" guide) rather than *what* it's about, so they
# shouldn't count toward the topical keyword match.
_FILLER_WORDS = {
    "tips", "tip", "guide", "beginners", "beginner", "explained", "tutorial",
    "revealed", "hacks", "hack", "tricks", "trick", "review", "reviewed",
    "best", "top", "ultimate", "basics", "secrets", "secret", "mistakes",
    "mistake", "ideas", "idea",
}

_PREFIX_LEN = 6


def _keyword_in_text(keyword: str, text: str) -> bool:
    """Prefix match (first ~6 chars) rather than exact substring, so word-form
    differences (budget/budgeting, tip/tips) don't break an otherwise-valid
    match."""
    return keyword[:_PREFIX_LEN] in text


def _term_relevant(term: str, title: str, description: str) -> bool:
    """True if the search term -- or its significant keywords -- appear in
    the result's title/description, case-insensitive. Guards against
    search.list surfacing tangentially-related authority channels (e.g. a
    generic science channel ranking for a niche multi-word phrase) whose
    videos only happen to share one generic word (e.g. "music", "chord")
    with the term rather than actually being about the topic.

    Stopwords and generic format/filler words (tips, beginners, explained,
    etc.) are stripped before requiring "all keywords present," since real
    creators paraphrase the packaging but keep the topic words. If that
    stripping leaves 0 or 1 significant word, "require all" would just be a
    single mandatory keyword again (the exact false-positive risk this is
    meant to guard against) -- so that case falls back to a direct match on
    the word(s) that remain instead of an all-of-N gate.
    """
    text = f"{title} {description}".lower()
    term_l = term.lower()
    if term_l in text:
        return True

    words = [w for w in re.findall(r"[a-z0-9']+", term_l) if len(w) > 2]
    significant = [w for w in words if w not in _STOPWORDS and w not in _FILLER_WORDS]

    if len(significant) >= 2:
        return all(_keyword_in_text(kw, text) for kw in significant)

    fallback = significant or [w for w in words if w not in _STOPWORDS]
    if not fallback:
        return False
    return any(_keyword_in_text(kw, text) for kw in fallback)


MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 2.0


def _get(session: requests.Session, endpoint: str, params: dict, api_key: str) -> dict:
    """Google returns quota-exhausted as EITHER 403 or 429 depending on which
    quota metric is hit -- confirmed live: search.list's daily cap
    (defaultSearchListPerDayPerProject) comes back as 429 with "quota" in the
    body, not 403. That's genuinely exhausted and retrying won't help, so it
    must be checked before the generic 429-retry branch below, which is for
    actual transient rate limiting (no "quota" in the body) and backs off
    instead of failing fast."""
    params = dict(params)
    params["key"] = api_key
    for attempt in range(MAX_RETRIES + 1):
        resp = session.get(f"{API_BASE}/{endpoint}", params=params, timeout=30)
        if resp.status_code in (403, 429) and "quota" in resp.text.lower():
            raise YouTubeError(f"Quota exceeded (or key restricted): {resp.text[:300]}")
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == MAX_RETRIES:
                resp.raise_for_status()
            retry_after = resp.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after is not None else None
            except ValueError:
                delay = None
            if delay is None:
                delay = BACKOFF_BASE_SECONDS * (2 ** attempt)
            log.warning(
                "Got HTTP %d from %s (attempt %d/%d) -- backing off %.1fs before retrying",
                resp.status_code, endpoint, attempt + 1, MAX_RETRIES + 1, delay,
            )
            time.sleep(delay)
            continue
        resp.raise_for_status()
        return resp.json()


def estimate_quota(niches: list[dict], cfg: dict) -> dict:
    """Rough pre-run estimate, split into the two independent buckets per
    Google's quota model: search.list draws down its own 100-calls/day cap
    (counted by call, not unit -- each call is exactly 1 unit against that
    cap regardless of COST_SEARCH_LIST's 100-unit cost under the *general*
    accounting), while channels/playlistItems/videos.list share the general
    10k-unit pool. Search call counts are exact (we know term counts up
    front); general-pool call counts are estimated assuming moderate dedup
    overlap across search terms, since the true unique channel count is only
    known after searching."""
    top_n = cfg["youtube"]["top_channels_sampled"]
    vids_per_channel = cfg["youtube"]["videos_per_channel"]

    per_niche = {}
    total_search_calls = 0
    total_general_units = 0
    for n in niches:
        terms = n.get("search_terms", [])
        search_calls = len(terms) * 2  # relevance + date

        # Assume ~2x top_n unique channels surface across all search calls
        # for this niche -- conservative-ish middle ground.
        est_unique_channels = max(top_n, min(top_n * 2, len(terms) * 50))
        channels_units = math.ceil(est_unique_channels / 50) * COST_CHANNELS_LIST

        playlist_units = top_n * COST_PLAYLISTITEMS_LIST
        videos_units = math.ceil((top_n * vids_per_channel) / 50) * COST_VIDEOS_LIST
        general_units = channels_units + playlist_units + videos_units

        per_niche[n["slug"]] = {
            "search_calls": search_calls,
            "channels_units_est": channels_units,
            "playlist_units": playlist_units,
            "videos_units_est": videos_units,
            "general_units_est": general_units,
        }
        total_search_calls += search_calls
        total_general_units += general_units

    return {
        "per_niche": per_niche,
        "total_search_calls": total_search_calls,
        "total_general_units": total_general_units,
    }


def already_ingested(run_date: str, slug: str) -> bool:
    d = common.niche_source_dir(run_date, "youtube", slug)
    return (d / "videos.json").exists()


def search_channel_ids(session: requests.Session, api_key: str, term: str, order: str, max_results: int) -> tuple[list[str], list[dict]]:
    data = _get(
        session,
        "search",
        {"part": "snippet", "q": term, "type": "video", "order": order, "maxResults": max_results},
        api_key,
    )
    items = data.get("items", [])
    channel_ids = [
        it["snippet"]["channelId"]
        for it in items
        if "snippet" in it
        and _term_relevant(term, it["snippet"].get("title", ""), it["snippet"].get("description", ""))
    ]
    return channel_ids, items


def fetch_channels(session: requests.Session, api_key: str, channel_ids: list[str]) -> list[dict]:
    out = []
    unique = list(dict.fromkeys(channel_ids))  # de-dupe, preserve order
    for i in range(0, len(unique), 50):
        batch = unique[i : i + 50]
        data = _get(
            session,
            "channels",
            {"part": "snippet,statistics,contentDetails", "id": ",".join(batch)},
            api_key,
        )
        out.extend(data.get("items", []))
    return out


def fetch_uploads_playlist_video_ids(session: requests.Session, api_key: str, uploads_playlist_id: str, max_results: int) -> list[str]:
    data = _get(
        session,
        "playlistItems",
        {"part": "contentDetails", "playlistId": uploads_playlist_id, "maxResults": max_results},
        api_key,
    )
    return [it["contentDetails"]["videoId"] for it in data.get("items", []) if "contentDetails" in it]


def fetch_videos(session: requests.Session, api_key: str, video_ids: list[str]) -> list[dict]:
    out = []
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        data = _get(session, "videos", {"part": "snippet,statistics", "id": ",".join(batch)}, api_key)
        out.extend(data.get("items", []))
    return out


def ingest_niche(session: requests.Session, api_key: str, niche: dict, cfg: dict, run_date: str, conn) -> None:
    slug = niche["slug"]
    yt_cfg = cfg["youtube"]
    log.info("Ingesting YouTube data for '%s'", slug)

    all_channel_ids: list[str] = []
    search_results_raw = []
    for term in niche.get("search_terms", []):
        for order in ("relevance", "date"):
            ids, items = search_channel_ids(session, api_key, term, order, yt_cfg["search_results_per_call"])
            all_channel_ids.extend(ids)
            search_results_raw.append({"term": term, "order": order, "items": items})
            common.log_quota(conn, run_date, "youtube", slug, COST_SEARCH_LIST, method="search")
            time.sleep(0.2)  # be polite even though quota, not rate limit, is the constraint

    channels = fetch_channels(session, api_key, all_channel_ids)
    n_channel_calls = math.ceil(len(set(all_channel_ids)) / 50) if all_channel_ids else 0
    if n_channel_calls:
        common.log_quota(conn, run_date, "youtube", slug, n_channel_calls * COST_CHANNELS_LIST, method="channels")

    # Rank by subscriberCount (fallback to viewCount) and take the top N.
    def sub_count(ch):
        try:
            return int(ch["statistics"].get("subscriberCount", 0))
        except (KeyError, ValueError):
            return 0

    channels_sorted = sorted(channels, key=sub_count, reverse=True)
    top_channels = channels_sorted[: yt_cfg["top_channels_sampled"]]

    videos_by_channel: dict[str, list[dict]] = {}
    for ch in top_channels:
        try:
            uploads_pl = ch["contentDetails"]["relatedPlaylists"]["uploads"]
        except KeyError:
            continue
        vid_ids = fetch_uploads_playlist_video_ids(session, api_key, uploads_pl, yt_cfg["videos_per_channel"])
        common.log_quota(conn, run_date, "youtube", slug, COST_PLAYLISTITEMS_LIST, method="playlistItems")
        if not vid_ids:
            continue
        vids = fetch_videos(session, api_key, vid_ids)
        common.log_quota(conn, run_date, "youtube", slug, math.ceil(len(vid_ids) / 50) * COST_VIDEOS_LIST, method="videos")
        videos_by_channel[ch["id"]] = vids
        time.sleep(0.2)

    out_dir = common.niche_source_dir(run_date, "youtube", slug)
    common.write_json(out_dir / "search_results.json", search_results_raw)
    common.write_json(out_dir / "channels.json", {"all_channels": channels, "top_channels": top_channels})
    common.write_json(out_dir / "videos.json", videos_by_channel)
    log.info(
        "  -> %s: %d channels found, %d sampled, %d videos across sampled channels",
        slug,
        len(channels),
        len(top_channels),
        sum(len(v) for v in videos_by_channel.values()),
    )


def main() -> None:
    cfg = common.load_config()
    parser = argparse.ArgumentParser(description="Ingest YouTube data for niche-scanner")
    parser.add_argument("--run-date", default=common.today_str())
    parser.add_argument("--niches", default=None, help="Comma-separated slugs to limit the run")
    parser.add_argument("--force", action="store_true", help="Re-ingest even if output already exists")
    parser.add_argument("--confirm", action="store_true", help="Proceed even if projected quota exceeds threshold")
    args = parser.parse_args()

    api_key = os.environ.get(cfg["youtube"]["api_key_env"])
    if not api_key:
        log.error("Set the %s environment variable before running.", cfg["youtube"]["api_key_env"])
        sys.exit(1)

    filter_slugs = args.niches.split(",") if args.niches else None
    niches = common.load_niches(filter_slugs)

    if not args.force:
        pending = [n for n in niches if not already_ingested(args.run_date, n["slug"])]
        skipped = len(niches) - len(pending)
        if skipped:
            log.info("Skipping %d already-ingested niche(s) (use --force to redo)", skipped)
        niches = pending

    if not niches:
        log.info("Nothing to do.")
        return

    conn = common.connect_db(args.run_date)

    estimate = estimate_quota(niches, cfg)
    ok = common.check_youtube_quota(
        conn, args.run_date, cfg,
        estimate["total_search_calls"], estimate["total_general_units"],
        args.confirm, log,
    )
    if not ok:
        sys.exit(1)

    session = requests.Session()
    for niche in niches:
        try:
            ingest_niche(session, api_key, niche, cfg, args.run_date, conn)
        except YouTubeError as e:
            log.error("Aborting on '%s': %s", niche["slug"], e)
            sys.exit(1)
    conn.close()
    log.info("Done.")


if __name__ == "__main__":
    main()

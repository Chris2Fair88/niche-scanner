"""Phase 1c -- Reddit ingestion (optional).

Per niche subreddit: subscriber count, plus posts/day estimated by sampling
r/<sub>/new and dividing the sample size by the time span it covers. A niche
with no mapped subreddit in niches.yaml is skipped entirely and just gets a
null demand-community score downstream in score.py -- that's expected, not
an error.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from pipeline import common

log = common.get_logger("ingest_reddit")


def already_ingested(run_date: str, slug: str) -> bool:
    return (common.source_dir(run_date, "reddit") / f"{slug}.json").exists()


def posts_per_day(reddit, subreddit_name: str, sample_size: int) -> tuple[int, float | None]:
    sub = reddit.subreddit(subreddit_name)
    subscribers = sub.subscribers
    timestamps = [post.created_utc for post in sub.new(limit=sample_size)]
    if len(timestamps) < 2:
        return subscribers, None
    span_seconds = max(timestamps) - min(timestamps)
    span_days = span_seconds / 86400.0
    if span_days <= 0:
        return subscribers, None
    return subscribers, len(timestamps) / span_days


def ingest_niche(reddit, niche: dict, cfg: dict, run_date: str) -> None:
    slug = niche["slug"]
    subs = niche.get("subreddits") or []
    if not subs:
        common.write_json(common.source_dir(run_date, "reddit") / f"{slug}.json", None)
        log.info("  -> %s: no mapped subreddit, writing null", slug)
        return

    # Use the first mapped subreddit as the primary demand signal; niches.yaml
    # can list more than one but v1 keeps this simple.
    primary = subs[0]
    log.info("Fetching Reddit stats for r/%s (%s)", primary, slug)
    subscribers, rate = posts_per_day(reddit, primary, cfg["reddit"]["new_posts_sample"])
    out = {
        "subreddit": primary,
        "subscribers": subscribers,
        "posts_per_day": rate,
        "sampled_at": datetime.now(timezone.utc).isoformat(),
    }
    common.write_json(common.source_dir(run_date, "reddit") / f"{slug}.json", out)
    log.info("  -> %s: %s subscribers, ~%.1f posts/day", slug, subscribers, rate or 0.0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest Reddit data for niche-scanner")
    parser.add_argument("--run-date", default=common.today_str())
    parser.add_argument("--niches", default=None, help="Comma-separated slugs to limit the run")
    parser.add_argument("--force", action="store_true", help="Re-ingest even if output already exists")
    args = parser.parse_args()

    try:
        import praw
    except ImportError:
        log.error("praw is not installed. Run: pip install praw")
        sys.exit(1)

    cfg = common.load_config()

    client_id = os.environ.get(cfg["reddit"]["client_id_env"])
    client_secret = os.environ.get(cfg["reddit"]["client_secret_env"])
    if not client_id or not client_secret:
        log.error(
            "Set %s and %s environment variables before running.",
            cfg["reddit"]["client_id_env"],
            cfg["reddit"]["client_secret_env"],
        )
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

    reddit = praw.Reddit(client_id=client_id, client_secret=client_secret, user_agent=cfg["reddit"]["user_agent"])

    for niche in niches:
        try:
            ingest_niche(reddit, niche, cfg, args.run_date)
        except Exception as e:
            log.warning("Skipping '%s' after error (not fatal -- Reddit is optional): %s", niche["slug"], e)
    log.info("Done.")


if __name__ == "__main__":
    main()

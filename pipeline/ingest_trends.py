"""Phase 1b -- Google Trends ingestion.

pytrends is an unofficial client scraping the Trends front-end, not a
supported Google API, so it gets rate-limited hard and unpredictably.
We sleep with jitter between requests and back off exponentially on 429s.
Resumable per-niche: a niche is skipped if its output file already exists.
"""
from __future__ import annotations

import argparse
import random
import sys
import time

from pipeline import common

log = common.get_logger("ingest_trends")


def already_ingested(run_date: str, slug: str) -> bool:
    return (common.source_dir(run_date, "trends") / f"{slug}.json").exists()


def fetch_term(pytrends, term: str, timeframe: str, cfg: dict) -> list[dict]:
    max_retries = cfg["trends"]["max_retries"]
    max_backoff = cfg["trends"]["max_backoff_seconds"]
    backoff = 5.0

    for attempt in range(1, max_retries + 1):
        try:
            pytrends.build_payload([term], timeframe=timeframe)
            df = pytrends.interest_over_time()
            if df is None or df.empty:
                return []
            series = []
            for idx, row in df.iterrows():
                series.append({"date": idx.strftime("%Y-%m-%d"), "interest": float(row[term])})
            return series
        except Exception as e:  # pytrends raises assorted requests/exceptions on 429s etc.
            msg = str(e)
            is_rate_limit = "429" in msg or "TooManyRequests" in msg
            if attempt == max_retries:
                log.error("Giving up on term '%s' after %d attempts: %s", term, attempt, msg)
                raise
            wait = min(backoff * (2 ** (attempt - 1)), max_backoff)
            if is_rate_limit:
                log.warning("Rate-limited on '%s' (attempt %d/%d), backing off %.0fs", term, attempt, max_retries, wait)
            else:
                log.warning("Error on '%s' (attempt %d/%d): %s -- retrying in %.0fs", term, attempt, max_retries, msg, wait)
            time.sleep(wait)
    return []


def ingest_niche(pytrends, niche: dict, cfg: dict, run_date: str) -> None:
    slug = niche["slug"]
    timeframe = cfg["trends"]["timeframe"]
    out = {}
    for term in niche.get("trends_terms", []):
        log.info("Fetching Trends series for '%s' (%s)", term, slug)
        out[term] = fetch_term(pytrends, term, timeframe, cfg)
        sleep_s = random.uniform(cfg["trends"]["sleep_min_seconds"], cfg["trends"]["sleep_max_seconds"])
        time.sleep(sleep_s)
    common.write_json(common.source_dir(run_date, "trends") / f"{slug}.json", out)
    log.info("  -> %s: %d term(s) captured", slug, len(out))


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest Google Trends data for niche-scanner")
    parser.add_argument("--run-date", default=common.today_str())
    parser.add_argument("--niches", default=None, help="Comma-separated slugs to limit the run")
    parser.add_argument("--force", action="store_true", help="Re-ingest even if output already exists")
    args = parser.parse_args()

    try:
        from pytrends.request import TrendReq
    except ImportError:
        log.error("pytrends is not installed. Run: pip install pytrends")
        sys.exit(1)

    cfg = common.load_config()

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

    pytrends = TrendReq(hl="en-US", tz=0)
    for niche in niches:
        try:
            ingest_niche(pytrends, niche, cfg, args.run_date)
        except Exception as e:
            log.error("Aborting on '%s' after retries exhausted: %s. Re-run later to resume.", niche["slug"], e)
            sys.exit(1)
    log.info("Done.")


if __name__ == "__main__":
    main()

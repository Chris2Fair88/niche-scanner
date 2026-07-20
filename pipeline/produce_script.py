"""Phase 4 (ad hoc) -- individual video script generation.

Generates a shot-list-style script (hook / setup / turn / payoff / CTA,
each with a voiceover line, on-screen caption, and visual/shot note) for a
single target niche, grounded in whatever real data the rest of the
pipeline has already produced for it: score.py's league-table metrics
(scanner.db's `scores` table), scan_trending.py's structured shortlist of
currently-breaking-out videos, and BUILD_LOG.yaml's real project
history -- bugs actually found and fixed, milestones actually reached.

The build-log source exists because a manual-vs-generated script
comparison found the manual script won largely on narrative: it told the
true story of the validation gate failing twice and real bugs getting
fixed, while a script grounded only in aggregate metrics had nothing with
comparable stakes to draw on. BUILD_LOG.yaml gives future generations that
same kind of real, verifiable material -- not niche-specific, but real.

This deliberately does NOT support free-text topics with no corresponding
niche in niches.yaml: the entire point is grounding in real, current
numbers rather than a generic prompt with the topic swapped, and there's
no real data to ground on for a niche this pipeline has never scored or
scanned. --topic resolves against niches.yaml's slugs/labels; if neither
score.py nor scan_trending.py has anything for the resolved niche, this
refuses to generate rather than quietly writing an ungrounded script.
(BUILD_LOG.yaml alone never satisfies this check -- it's supplementary
color, not a substitute for niche-specific data.)

Reuses report.py's call_sonnet() (thinking disabled, adequate max_tokens)
directly rather than reimplementing it -- that's the exact bug already
found and fixed once in report.py itself. Also reuses report.py's
fetch_rows()/fmt_num()/fmt_pct() rather than re-querying scanner.db or
re-formatting metrics from scratch.

Output: scripts/<niche-slug>-<run_date>.md, tracked in git the same way
reports/ and its contents are.
"""
from __future__ import annotations

import argparse
import re
import sys

from pipeline import common
from pipeline.report import call_sonnet, estimate_tokens, fetch_rows, fmt_num, fmt_pct

log = common.get_logger("produce_script")

# Phrases that plausibly acknowledge "this is one data point, not a
# trend" -- used by verify_generated_script()'s single-candidate overclaim
# check. Deliberately a heuristic substring list, not a semantic check:
# pure string matching can't verify the script actually reasons about
# sample size, only that it uses language consistent with doing so.
SINGLE_CANDIDATE_QUALIFIERS = [
    "one data point", "single data point", "one video", "single video",
    "one clip", "single clip", "one candidate", "not a trend",
    "not the new normal", "isolated", "outlier, not", "one example",
    "just one", "small sample", "one instance", "a single",
]


def resolve_niche_slug(topic: str | None, conn) -> tuple[str, str]:
    """No topic -> current rank-1 niche from score.py's output for this
    run_date (reusing report.fetch_rows(), already ordered by rank ASC).
    A topic -> resolved against niches.yaml by slug or label (exact match
    first, then a softer label-substring match), since real grounding data
    only exists for niches score.py/scan_trending.py actually track. A
    topic that doesn't resolve is a hard error, not a fallback to a
    generic script -- see the module docstring."""
    if topic is None:
        rows = fetch_rows(conn)
        if not rows:
            log.error("No scores found for this run-date. Run score.py first, or pass --topic.")
            sys.exit(1)
        top = rows[0]
        return top["niche_slug"], top["label"]

    niches = common.load_niches()
    topic_l = topic.strip().lower()
    for n in niches:
        if n["slug"].lower() == topic_l or n["label"].lower() == topic_l:
            return n["slug"], n["label"]
    for n in niches:
        if topic_l in n["label"].lower():
            return n["slug"], n["label"]

    log.error(
        "'%s' doesn't match any niche slug or label in niches.yaml. "
        "produce_script.py only generates scripts for niches this pipeline "
        "actually tracks -- pass an exact slug or label from niches.yaml.",
        topic,
    )
    sys.exit(1)


def get_score_row(conn, niche_slug: str) -> dict | None:
    for r in fetch_rows(conn):
        if r["niche_slug"] == niche_slug:
            return r
    return None


def load_trend_candidates(niche_slug: str, trend_date: str) -> list[dict] | None:
    """scan_trending.py's structured shortlist for trend_date, if that
    script has been run on that day and found candidates for this niche.
    trend_date is deliberately independent of run_date (the score.py data
    batch): trending activity is always "as of now", not tied to whichever
    run_date the composite scores happen to be frozen at."""
    path = common.source_dir(trend_date, "trend_scan") / "shortlist.json"
    if not path.exists():
        return None
    niche_results = common.read_json(path)
    for nr in niche_results:
        if nr["slug"] == niche_slug:
            return nr["candidates"] or None
    return None


def filter_safe_trend_candidates(candidates: list[dict] | None, cfg: dict) -> list[dict] | None:
    """Structural (code, not prompt-only) content-safety filter, applied
    between load_trend_candidates() and build_grounding_block() -- matches
    are excluded entirely, never passed through with a caveat, since a
    prompt-level ask alone ("don't cite adult-platform content") didn't
    reliably hold on its own.

    Two independent rules, either of which excludes a candidate:
      (a) config.yaml's content_safety.denylist_terms, case-insensitive
          substring match against title/channel_title. Handles/@mentions
          embedded in a title (e.g. "...@FanvueAIAcademy") are already
          covered by this, since they're part of the same title string.
      (b) content_safety.min_view_count_for_citation -- a candidate can be
          a genuine statistical outlier under scan_trending.py's robust_z
          (correctly flagged relative to its own niche's recent baseline)
          while still being too small in absolute terms to be worth
          building a video's hook/CTA around.

    Every exclusion is logged with which rule triggered it and the
    candidate's title -- a real record of what got filtered and why, not
    a silent skip. Returns None/[] straight through if there was nothing
    to filter, so callers that already handle "no trend data available"
    (score.py-only grounding) handle a fully-filtered list identically --
    this never errors out on its own."""
    if not candidates:
        return candidates

    scfg = cfg.get("content_safety", {})
    denylist = [t.lower() for t in scfg.get("denylist_terms", [])]
    min_views = scfg.get("min_view_count_for_citation", 0)

    safe = []
    for c in candidates:
        title = c.get("title") or ""
        channel = c.get("channel_title") or ""
        haystack = f"{title} {channel}".lower()

        hit = next((term for term in denylist if term in haystack), None)
        if hit:
            log.warning(
                "Excluding trend candidate [denylist term '%s']: \"%s\" by %s",
                hit, title, channel,
            )
            continue

        view_count = c.get("view_count", 0)
        if view_count < min_views:
            log.warning(
                "Excluding trend candidate [below min_view_count_for_citation=%d, "
                "had %d views]: \"%s\" by %s",
                min_views, view_count, title, channel,
            )
            continue

        safe.append(c)

    return safe


def _extract_cta_section(script_body: str) -> str:
    """Isolates just the CTA beat's own text (Voiceover/Caption/Visual),
    stopping before the trailing '**Data grounding used:**' line -- that
    line legitimately names cited candidates, so including it here would
    false-positive the CTA-mention check below."""
    match = re.search(
        r"##\s*CTA(.*?)(?:\*\*Data grounding used:\*\*|\Z)",
        script_body, re.DOTALL | re.IGNORECASE,
    )
    return match.group(1) if match else ""


def verify_generated_script(script_body: str, trend_candidates: list[dict] | None, cfg: dict) -> list[str]:
    """Deterministic post-generation checks -- code, not a prompt
    instruction alone, run after call_sonnet() returns but before writing
    anything to disk. Returns a list of failure reasons; empty means every
    check passed. trend_candidates here is the already-filtered (safe)
    list actually used in the grounding data, not the raw scan_trending.py
    output.

    Three checks:
      1. The CTA section never mentions a cited candidate's channel_title
         or title -- the CTA must point toward this channel, never send
         the viewer to a third-party channel/video.
      2. If grounding included exactly one trend candidate, the generated
         text acknowledges that (heuristic substring match against
         SINGLE_CANDIDATE_QUALIFIERS) -- guards against overclaiming a
         trend from a single data point. Deliberately still checked even
         though it's fuzzier than check 1; a soft heuristic beats no check.
      3. No content_safety.denylist_terms term appears anywhere in the
         final generated text -- independent of the filter in
         filter_safe_trend_candidates(), since that filter only covers
         the source material fed in, and the model could in theory
         reference something denylisted from elsewhere in its training.
    """
    failures = []
    scfg = cfg.get("content_safety", {})
    denylist = scfg.get("denylist_terms", [])

    cta_text = _extract_cta_section(script_body).lower()
    if trend_candidates:
        for c in trend_candidates:
            for field in ("channel_title", "title"):
                val = c.get(field)
                if val and val.lower() in cta_text:
                    failures.append(
                        f"CTA section mentions trend candidate {field}={val!r} -- "
                        f"the CTA must point toward this channel, never a third-party "
                        f"channel/video."
                    )

    if trend_candidates and len(trend_candidates) == 1:
        body_lower = script_body.lower()
        if not any(p in body_lower for p in SINGLE_CANDIDATE_QUALIFIERS):
            failures.append(
                "Exactly one trend candidate was in the grounding data, but the "
                "generated script doesn't contain language acknowledging it's a "
                "single data point (e.g. 'one video', 'not a trend', 'small "
                "sample') -- risks overclaiming a trend from one example."
            )

    body_lower_full = script_body.lower()
    hit = next((term for term in denylist if term.lower() in body_lower_full), None)
    if hit:
        failures.append(
            f"Generated text contains denylisted term {hit!r} -- independent "
            f"check of the final output, not just the source grounding data."
        )

    return failures
    return None


def get_recent_build_log_entries(cfg: dict) -> list[dict]:
    """The most recent N entries from BUILD_LOG.yaml (chronological,
    oldest first, so the tail is the most recent), per config.yaml's
    build_log.recent_entries_for_grounding. Unlike score_row/trend
    candidates, this isn't niche-specific -- it's real project history any
    script can draw on for narrative material with actual stakes."""
    entries = common.load_build_log()
    n = cfg.get("build_log", {}).get("recent_entries_for_grounding", 5)
    return entries[-n:] if n else entries


def build_grounding_block(
    score_row: dict | None, run_date: str,
    trend_candidates: list[dict] | None, trend_date: str,
    build_log_entries: list[dict] | None = None,
) -> str:
    """Deterministic, Python-generated summary of exactly what real data
    this run is grounded in -- same principle as report.py's league table
    being generated in Python rather than by the LLM. Embedded verbatim in
    both the prompt sent to Claude and the output file, so the two always
    match and grounding is auditable at a glance."""
    lines = []
    if score_row:
        lines.append(
            f"- **score.py metrics** (run_date={run_date}): rank {score_row['rank']}, "
            f"composite {fmt_num(score_row['composite'], 3)}, "
            f"breakout_rate {fmt_pct(score_row['breakout_rate'])}, "
            f"trend_slope {fmt_num(score_row['trend_slope'], 3)}, "
            f"velocity_median_views {fmt_num(score_row['velocity'])}, "
            f"capture_index {fmt_pct(score_row['capture_index'])}, "
            f"sponsor_density {fmt_pct(score_row['sponsor_density'])}, "
            f"policy_risk {fmt_num(score_row['policy_risk'], 2) if score_row['policy_risk'] is not None else 'n/a'}"
        )
    else:
        lines.append(f"- **score.py metrics**: none available for this niche at run_date={run_date}.")

    if trend_candidates:
        lines.append(f"- **scan_trending.py shortlist** (trend_date={trend_date}, {len(trend_candidates)} candidate(s)):")
        for c in trend_candidates:
            z = fmt_num(c.get("robust_z"), 2) if c.get("robust_z") is not None else "n/a (small sample)"
            lines.append(
                f"  - \"{c['title']}\" by {c['channel_title']} -- {fmt_num(c['view_count'])} views, "
                f"{fmt_num(c['age_hours'], 1)}h old, velocity {fmt_num(c['velocity'], 1)} views/hour, robust_z={z}"
            )
    else:
        lines.append(f"- **scan_trending.py shortlist**: none available for this niche as of trend_date={trend_date}.")

    if build_log_entries:
        lines.append(f"- **BUILD_LOG.yaml recent project history** ({len(build_log_entries)} entries, not niche-specific -- real events from building this pipeline itself):")
        for e in build_log_entries:
            lines.append(
                f"  - [{e['date']}, commit {e['commit']}, {e['type']}] {e['summary'].strip()} "
                f"Why it mattered: {e['why'].strip()}"
            )
    else:
        lines.append("- **BUILD_LOG.yaml**: no entries recorded yet.")

    return "\n".join(lines)


def run(run_date: str, topic: str | None, trend_date: str, force: bool) -> str:
    cfg = common.load_config()
    conn = common.connect_db(run_date)

    niche_slug, niche_label = resolve_niche_slug(topic, conn)

    # Check for an existing script BEFORE spending an API call on one that
    # would just be refused -- this used to overwrite silently (confirmed:
    # re-running for the same niche+run_date clobbered the existing file
    # with a fresh, non-identical generation and no warning at all).
    out_path = common.SCRIPTS_DIR / f"{niche_slug}-{run_date}.md"
    if out_path.exists() and not force:
        conn.close()
        log.error(
            "Refusing to overwrite existing script: %s already exists for "
            "niche '%s' at run_date=%s. Pass --force to regenerate and "
            "overwrite it, or use a different --run-date if this is meant "
            "to be a distinct version.",
            out_path, niche_slug, run_date,
        )
        sys.exit(1)

    score_row = get_score_row(conn, niche_slug)
    conn.close()

    trend_candidates = load_trend_candidates(niche_slug, trend_date)
    trend_candidates = filter_safe_trend_candidates(trend_candidates, cfg)

    if score_row is None and not trend_candidates:
        log.error(
            "No real grounding data available for '%s' -- score.py has no row for "
            "run_date=%s and scan_trending.py has no shortlist for trend_date=%s. "
            "Run score.py and/or scan_trending.py for this niche first.",
            niche_slug, run_date, trend_date,
        )
        sys.exit(1)

    build_log_entries = get_recent_build_log_entries(cfg)
    grounding_block = build_grounding_block(score_row, run_date, trend_candidates, trend_date, build_log_entries)

    system_prompt = (common.PROMPTS_DIR / "produce_script.md").read_text(encoding="utf-8")
    user_content = (
        f"Target niche: {niche_label} (slug: {niche_slug})\n\n"
        f"Real data available for this niche:\n{grounding_block}\n"
    )

    est = estimate_tokens(system_prompt) + estimate_tokens(user_content)
    log.info("Estimated input tokens for the synthesis call: ~%d", est)

    log.info("Calling %s to draft a script for '%s'...", cfg["anthropic"]["model"], niche_slug)
    script_body = call_sonnet(system_prompt, user_content, cfg)

    # Deterministic self-verification BEFORE writing anything to disk --
    # same "refuse rather than silently produce something wrong" pattern
    # as the overwrite guard above. Never auto-retries/regenerates
    # silently; a failure surfaces to whoever ran the command.
    failures = verify_generated_script(script_body, trend_candidates, cfg)
    if failures:
        log.error("Generated script failed self-verification for '%s' -- refusing to write %s:", niche_slug, out_path)
        for f in failures:
            log.error("  - %s", f)
        sys.exit(1)

    header = (
        f"# Video Script -- {niche_label} -- {run_date}\n\n"
        f"**Target niche:** {niche_label} (slug: `{niche_slug}`)\n\n"
        f"**Grounding data used:**\n{grounding_block}\n"
    )

    script_md = f"{header}\n## Script\n\n{script_body.strip()}\n"

    common.SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(script_md)
    log.info("Script written to %s", out_path)
    return str(out_path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a shot-list-style video script for a niche, grounded in real pipeline data",
    )
    parser.add_argument(
        "--run-date", default=common.today_str(),
        help="Which data/{run_date}/scanner.db to pull score.py metrics from",
    )
    parser.add_argument(
        "--topic", default=None,
        help="Niche slug or label from niches.yaml; defaults to the current rank-1 niche for --run-date",
    )
    parser.add_argument(
        "--trend-date", default=common.today_str(),
        help="Which data/{trend_date}/trend_scan/shortlist.json to check for fresh scan_trending.py "
             "grounding -- independent of --run-date, since trending data is always as-of-now",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing script file for this niche+run_date (refused by default)",
    )
    args = parser.parse_args()
    run(args.run_date, args.topic, args.trend_date, args.force)


if __name__ == "__main__":
    main()

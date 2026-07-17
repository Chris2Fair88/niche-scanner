"""Phase 3 -- Report generation.

The league table is built directly from SQLite in Python, NOT by the LLM --
that's what guarantees "league table matches SQLite scores exactly" from
the definition of done. The single Sonnet call only writes the four
narrative sections (verdicts, fit analysis, honesty check, caveats) from a
compact per-niche metrics summary; it never sees raw ingestion JSON, and
its own report is not permitted to restate the table itself.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

from pipeline import common

log = common.get_logger("report")

# Fixed, non-LLM-generated addendum to the Data Caveats section. This is a
# limitation of the ingestion pipeline itself (ingest_youtube.py's relevance
# filter can only see the title/description of whichever video search.list
# happened to surface), not something derived from a given run's metrics, so
# it's appended verbatim rather than left to the model to decide to mention.
RELEVANCE_FILTER_CAVEAT = (
    "The YouTube relevance filter can only evaluate the text of whichever "
    "video actually surfaced in search -- a genuinely on-topic channel whose "
    "sampled video doesn't restate the niche's search terms may be "
    "undercounted or excluded. Sampled channel lists are worth a manual "
    "skim, not blind trust."
)


def fmt_pct(x):
    return f"{x * 100:.1f}%" if x is not None else "n/a"


def fmt_num(x, decimals=0):
    if x is None:
        return "n/a"
    return f"{x:.{decimals}f}"


def build_league_table(rows: list[dict]) -> str:
    header = (
        "| Rank | Niche | Composite | Breakout % | Trend Slope | Velocity (med. views) | "
        "Sponsor % | RPM range | Upload burden (/mo) | Policy risk | Capture idx | Expertise |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|---|\n"
    )
    lines = []
    for r in rows:
        lines.append(
            f"| {r['rank']} | {r['label']} | {fmt_num(r['composite'], 3)} | "
            f"{fmt_pct(r['breakout_rate'])} | {fmt_num(r['trend_slope'], 3)} | "
            f"{fmt_num(r['velocity'])} | {fmt_pct(r['sponsor_density'])} | "
            f"${fmt_num(r['rpm_low'])}-${fmt_num(r['rpm_high'])} | "
            f"{fmt_num(r['upload_burden'], 1)} | {fmt_num(r['policy_risk'], 2) if r['policy_risk'] is not None else 'n/a'} | "
            f"{fmt_pct(r['capture_index'])} | {r['my_expertise']} |"
        )
    return header + "\n".join(lines)


def build_metrics_summary_for_llm(rows: list[dict]) -> str:
    lines = []
    for r in rows:
        lines.append(
            f"- {r['label']} (slug: {r['niche_slug']}, rank {r['rank']}, composite {fmt_num(r['composite'], 3)}, "
            f"expertise: {r['my_expertise']}): breakout_rate={fmt_pct(r['breakout_rate'])}, "
            f"capture_index={fmt_pct(r['capture_index'])}, trend_slope={fmt_num(r['trend_slope'], 3)}, "
            f"velocity_median_views={fmt_num(r['velocity'])}, sponsor_density={fmt_pct(r['sponsor_density'])}, "
            f"rpm_range=${fmt_num(r['rpm_low'])}-${fmt_num(r['rpm_high'])}, "
            f"upload_burden_per_month={fmt_num(r['upload_burden'], 1)}, "
            f"policy_risk={fmt_num(r['policy_risk'], 2) if r['policy_risk'] is not None else 'n/a'}"
            f"{' [NEGATIVE CONTROL]' if r['is_negative_control'] else ''}"
        )
    return "\n".join(lines)


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)  # rough heuristic, ~4 chars/token in English


def fetch_rows(conn) -> list[dict]:
    cur = conn.execute(
        """SELECT s.*, n.label, n.my_expertise
           FROM scores s JOIN niches n ON s.niche_slug = n.slug
           ORDER BY s.rank ASC"""
    )
    return [dict(row) for row in cur.fetchall()]


def fetch_run_meta(conn, run_date: str) -> dict | None:
    cur = conn.execute("SELECT * FROM run_meta WHERE run_date = ?", (run_date,))
    row = cur.fetchone()
    return dict(row) if row else None


def call_sonnet(system_prompt: str, user_content: str, cfg: dict) -> str:
    try:
        import anthropic
    except ImportError:
        log.error("The 'anthropic' package is not installed. Run: pip install anthropic")
        sys.exit(1)

    api_key = os.environ.get(cfg["anthropic"]["api_key_env"])
    if not api_key:
        log.error("Set the %s environment variable before running.", cfg["anthropic"]["api_key_env"])
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=cfg["anthropic"]["model"],
        max_tokens=cfg["anthropic"]["max_tokens"],
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )
    return "".join(block.text for block in resp.content if getattr(block, "type", None) == "text")


def run(run_date: str) -> None:
    cfg = common.load_config()
    conn = common.connect_db(run_date)
    rows = fetch_rows(conn)
    if not rows:
        log.error("No scores found for run-date %s. Run score.py first.", run_date)
        sys.exit(1)
    meta = fetch_run_meta(conn, run_date)
    conn.close()

    league_table = build_league_table(rows)
    metrics_summary = build_metrics_summary_for_llm(rows)

    system_prompt = (common.PROMPTS_DIR / "synthesize.md").read_text(encoding="utf-8")
    status_line = f"Validation gate status: {meta['status']!r} -- {meta['note']}" if meta else "Validation gate status: unknown"
    user_content = f"{status_line}\n\nPer-niche metrics:\n{metrics_summary}\n"

    est = estimate_tokens(system_prompt) + estimate_tokens(user_content)
    log.info("Estimated input tokens for the synthesis call: ~%d", est)

    log.info("Calling %s for narrative synthesis...", cfg["anthropic"]["model"])
    narrative = call_sonnet(system_prompt, user_content, cfg)

    header_lines = [f"# Niche Viability Report -- {run_date}"]
    if meta and meta["status"] == "suspect":
        header_lines.append("")
        header_lines.append(
            f"> **VALIDATION GATE FAILED** -- {meta['note']} "
            "Treat this run's rankings with suspicion until the scorer is fixed."
        )
    header = "\n".join(header_lines)

    report_md = (
        f"{header}\n\n"
        f"## League Table\n\n{league_table}\n\n"
        f"{narrative.strip()}\n\n"
        f"{RELEVANCE_FILTER_CAVEAT}\n"
    )

    out_path = common.REPORTS_DIR / f"niche-report-{run_date}.md"
    common.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report_md)
    log.info("Report written to %s", out_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the niche-scanner report")
    parser.add_argument("--run-date", default=common.today_str())
    args = parser.parse_args()
    run(args.run_date)


if __name__ == "__main__":
    main()

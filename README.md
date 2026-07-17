# niche-scanner

Local pipeline that scores candidate content niches for semi-passive income
viability from primary platform signals (YouTube, Google Trends, Reddit) --
never from "top niches" listicles or guru content.

Windows 11 / 10 compatible: `pathlib` throughout, explicit `encoding="utf-8"`
on every file read/write, no POSIX-only calls. Runs the same on Windows,
macOS, or Linux.

## Setup

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Set environment variables (PowerShell: `$env:YOUTUBE_API_KEY = "..."`, or use
a `.env` loader of your choice -- this pipeline reads them directly from
`os.environ`, it never accepts keys as CLI args or config values):

- `YOUTUBE_API_KEY` -- YouTube Data API v3 key
- `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` -- Reddit script-app credentials (optional; niches without a mapped subreddit just get a null Reddit signal)
- `ANTHROPIC_API_KEY` -- for the Phase 3 synthesis call only

## Run order

```powershell
python -m pipeline.ingest_youtube --niches music-theory-shorts,guitar-lessons-beginner
python -m pipeline.ingest_trends
python -m pipeline.ingest_reddit
python -m pipeline.score
python -m pipeline.report
```

Every stage defaults `--run-date` to today and is resumable: re-running
skips niches that already have output on disk for that run-date, unless you
pass `--force`. `--niches slug1,slug2` limits any stage to a subset.

### Quota reality check

`ingest_youtube.py` estimates YouTube quota cost before it does anything and
refuses to proceed past 8,000 units without `--confirm` (both configurable
in `config.yaml`). **Running the full ~23-niche seed list in one shot costs
~10,100 units** -- over the default free daily quota of 10,000. That's not a
bug in the estimate; it's the reason the brief calls for scanning in
batches:

```powershell
python -m pipeline.ingest_youtube --niches music-theory-shorts,guitar-lessons-beginner,ukulele-covers-tutorials,home-recording-production,dev-tutorials-webdev,ai-assisted-coding
# next day
python -m pipeline.ingest_youtube --niches personal-finance-tips,notion-productivity-templates,ai-tool-reviews,faceless-automation-channels,dropshipping-ecom,stoic-philosophy-shorts,dark-psychology-facts,reddit-story-narration
# next day
python -m pipeline.ingest_youtube --niches minimalist-lifestyle,crypto-trading-signals,ai-agent-news,book-summary-channels,home-workout-no-equipment,language-learning-shorts,generic-motivation-compilations,luxury-lifestyle-flexing,clickbait-net-worth-shorts
```

Each batch call only re-estimates and re-confirms for the niches you pass;
already-ingested niches from a prior batch are skipped automatically since
they're resumable per-niche-per-source.

## What's been validated vs. what hasn't

I don't have a YouTube/Reddit/Trends API key in this environment, so the
three ingestion scripts are syntax-checked and structurally reviewed but
**not** exercised against live APIs -- run a small `--niches` batch first and
watch the logs before trusting a full run.

The scoring engine (`score.py`) -- the part the brief says must be validated
before anything else -- I tested end-to-end against synthetic fixtures
standing in for real ingestion output:

- A realistic "good" niche and an "owned by incumbents" niche both scored
  above a sabotaged negative control.
- The negative control was deliberately tuned in stages to see the gate
  behave correctly in *both* directions: it stays quiet when the control
  correctly loses, and trips (`status: suspect`, exit code 2) once the
  control was pushed hard enough to actually contend for a top-half rank --
  confirming the gate isn't a dead code path.
- A niche with zero ingested data got flagged with explicit "missing
  metric" warnings rather than silently defaulting to a misleading score
  (see `score.py`'s module docstring for the exact policy: missing data is
  scored as worst-case, never as neutral/average).
- `report.py`'s league table is built directly from SQLite in Python, not
  transcribed by the LLM, which is what the "league table matches SQLite
  scores exactly" definition-of-done item actually requires -- I caught and
  fixed a real bug here (a column-name mismatch) during testing.

I did not spend a real Sonnet call testing Phase 3's prose output; I did
confirm it fails cleanly (clear message, exit code 1) when `ANTHROPIC_API_KEY`
is unset rather than throwing a raw stack trace.

## Notes on deviations from the original brief

- **Model string updated**: the brief specified `claude-sonnet-4-6`, which
  is a superseded model string. `config.yaml` now defaults to
  `claude-sonnet-5`, Anthropic's current mid-tier model as of July 2026.
  Double-check https://docs.claude.com/en/docs/about-claude/models/overview
  before a real run if it's been a while -- this changes often.
- Everything else follows the brief as written, including the exact
  weighting scheme, the eight metrics, and the five-section report
  structure.

## Companion tool

`creator-audit`'s extracted niches can be appended directly to `niches.yaml`
using the same schema -- see the file's header comment.

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

`ingest_youtube.py` tracks YouTube quota against **two independent buckets**,
not one shared pool:

- **`search_list_daily_cap`** (default 100) -- `search.list` has its own
  separate cap of 100 calls/day, counted by call, not by unit. This is the
  primary, more binding gate: each niche's search terms are queried twice
  (relevance + date), so search-call count scales directly with the seed
  list's term count.
- **`quota_abort_threshold`** (default 8,000 units) -- a local confirm
  threshold over the general pool covering `channels.list`,
  `playlistItems.list`, and `videos.list`. This is not a hard daily API
  quota; it's a safety check the script applies before spending, and
  proceeding past it just requires `--confirm`.

Both are configurable in `config.yaml`. The script estimates both buckets
before doing anything and refuses to proceed past either without
`--confirm`. For the current 23-niche seed list (`niches.yaml`), a full
single-shot run estimates to **~92 of the 100 daily `search.list` calls**
and **~943 of the 8,000-unit general pool** -- so a full run currently fits
in one day under both caps, with the `search.list` cap as the tighter
constraint (only ~8 calls of headroom). Adding niches or search terms to the
seed list narrows that headroom fastest, since it's the call-count cap that
binds first. If you do need to spread ingestion across days -- a larger
seed list, or added search terms per niche -- batch by `--niches`:

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

That synthetic-fixture pass was the *first* validation, done before any real
ingestion data existed. It was later superseded by real-data validation, and
this file was never updated to say so until now -- stated explicitly:

- Commit `bf94ccd` (2026-07-17) fixed the validation gate against **real**
  ingested data from three real run dates (2026-07-15, 2026-07-16,
  2026-07-17), not fixtures: the gate was failing on that real data (needed
  to discount monopoly niches and tame a velocity outlier) before that fix.
- `niches.yaml` defines three real negative-control niches that go through
  the full real pipeline like any other niche: `clickbait-net-worth-shorts`,
  `luxury-lifestyle-flexing`, and `generic-motivation-compilations`.
- The real 2026-07-15 run (`reports/niche-report-2026-07-15.md`) reports
  `status: "ok"` from the validation gate, with all three of those real
  negative controls ranking in the bottom half against real candidate
  niches -- not a synthetic run.

I did not spend a real Sonnet call testing Phase 3's prose output; I did
confirm it fails cleanly (clear message, exit code 1) when `ANTHROPIC_API_KEY`
is unset rather than throwing a raw stack trace.

`produce_voiceover.py` is now live-verified against the real ElevenLabs API
(it wasn't at the commit that introduced it). Stated explicitly, with the
actual values from that run against the `faceless-automation-channels`
(2026-07-15) script:

- Resolved voice: `hpp4J3VqNfWAUOO0d1Us` -- "Bella - Professional, Bright,
  Warm" (premade), picked up automatically from `GET /v2/voices` since
  `elevenlabs.voice_id` is unset in `config.yaml`.
- All five beats generated via the with-timestamps endpoint, 637 characters
  total (Hook 64, Setup 161, Turn 200, Payoff 118, CTA 94):
  `01-hook.mp3` (71,515 bytes), `02-setup.mp3` (185,199 bytes),
  `03-turn.mp3` (225,324 bytes), `04-payoff.mp3` (108,713 bytes),
  `05-cta.mp3` (94,084 bytes) -- 42.5s of audio combined.
- Each mp3 has a matching `<beat>.alignment.json` with real
  character-level `character_start_times_seconds` /
  `character_end_times_seconds` arrays (confirmed non-empty and
  monotonically increasing, e.g. Hook's last character ends at 4.412s,
  matching its own reported duration) and a `manifest.json` tying beat
  order to files and durations.
- No unhandled errors -- confirming the earlier key-missing/401 checks
  generalize to a real 200 response, not just clean failure paths.

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

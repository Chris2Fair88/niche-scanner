# Video Script -- Faceless YouTube automation channels (meta-niche) -- 2026-07-15

**Target niche:** Faceless YouTube automation channels (meta-niche) (slug: `faceless-automation-channels`)

**Grounding data used:**
- **score.py metrics** (run_date=2026-07-15): rank 1, composite 0.528, breakout_rate 3.3%, trend_slope 0.094, velocity_median_views 11834, capture_index 58.3%, sponsor_density 17.0%, policy_risk 0.02
- **scan_trending.py shortlist** (trend_date=2026-07-20, 1 candidate(s)):
  - "The 1% YouTube Automation Secret 👀#america #usa #viral #shorts #viralvideo @FanvueAIAcademy" by Leo Grundström Clips -- 754 views, 8.5h old, velocity 89.2 views/hour, robust_z=35.16
- **BUILD_LOG.yaml recent project history** (5 entries, not niche-specific -- real events from building this pipeline itself):
  - [2026-07-17, commit 95cf871, bug_fix] report.py silently returned an empty or truncated narrative -- confirmed nondeterministic, since a second identical call failed a different way. Why it mattered: Claude Sonnet 5 runs adaptive thinking by default when the thinking parameter is omitted, and max_tokens caps thinking plus visible output combined -- so thinking alone could consume the entire budget and leave nothing for the actual report. Fixed by explicitly disabling thinking for this deterministic-synthesis task and raising max_tokens to give the (also larger, newer-tokenizer) output real headroom.
  - [2026-07-17, commit 910b693, bug_fix] Building scan_trending.py surfaced a real cross-day quota bug: quota_log was keyed by run_date, a data-folder label deliberately decoupled from real calendar time -- but Google's 100-calls/day search.list cap resets on real time, not on that label. Why it mattered: A script reused against an older, frozen run_date on a genuinely different real day would have seen stale usage from unrelated days counted as "today's" spend. Added a real_date column, set from the system clock at logging time and never from run_date, and switched quota enforcement to key on it instead -- verified by mocking a future real date under the same run_date and confirming it saw zero stale usage.
  - [2026-07-17, commit e6e15be, bug_fix] reddit-story-narration surfaced only 12 of a targeted 30 channels during ingestion, but its metrics (95.8% capture_index, 352 median velocity) were presented in the report as plain fact with no indication the sample was unusually thin. Why it mattered: Every one of those 12 channels was taken forward with zero filtering headroom, making metrics like capture_index far more sensitive to any single channel than in a fully-sampled niche. Added a caveat that triggers on any niche whose sampled channel count falls under half its target -- not hardcoded to this one niche -- so it catches the same problem automatically in the future.
  - [2026-07-20, commit 2e4d415, feature] Added produce_script.py: generates a shot-list-style video script for a niche, grounded in whatever real score.py metrics and scan_trending.py trending data actually exist for it. Why it mattered: The whole point was avoiding a templated, easily-flagged pattern -- a generic prompt with the topic swapped in. The script is required to cite at least one specific, real data point, enforced in the prompt and, when no real data exists for a niche at all, by refusing to generate rather than quietly producing something ungrounded.
  - [2026-07-20, commit 04ec622, bug_fix] produce_script.py was found to silently overwrite an existing generated script for the same niche+run_date -- no warning, no --force requirement, and the regeneration wasn't even deterministic (a re-run against identical grounding data produced a different script every time). Why it mattered: That meant a second run could destroy a committed script with zero trace of what was lost. Added a guard that refuses by default and requires an explicit --force, checked before any API call is made so a refusal costs nothing.

## Script

## Hook

**Voiceover:** One video hit a robust_z of 35 — that's not viral, that's an outlier explosion.
**Caption:** robust_z = 35.16 👀
**Visual:** Screen recording zooming into the scan_trending.py output, the number "35.16" highlighted in red against a table of normal-looking rows.

## Setup

**Voiceover:** It's called "The 1% YouTube Automation Secret," posted by a clips channel, barely 8 hours old. Faceless automation content is rank 1 in its niche league table right now — but breakout rate is only 3.3%.
**Caption:** Rank #1. Breakout rate: 3.3%.
**Visual:** Split screen — left side shows the trending title/channel name, right side shows the score.py composite score (0.528) and rank (1) as a scoreboard graphic.

## Turn

**Voiceover:** Here's the twist: we almost shipped a broken version of this exact video. Our script generator was silently overwriting old scripts, no warning, different output every re-run on the same data.
**Caption:** Same data. Different script. Every time.
**Visual:** Terminal footage of a diff command showing two different script outputs from identical inputs, then a red "OVERWRITE BLOCKED" guard message appearing.

## Payoff

**Voiceover:** We fixed it — now it refuses to regenerate without an explicit force flag. That's the same discipline this niche needs: high capture at 58.3%, but sponsor density's already at 17%.
**Caption:** Capture 58.3%. Sponsor density 17%.
**Visual:** Bar chart animating capture_index and sponsor_density side by side, then a checkmark over the "--force" guard code.

## CTA

**Voiceover:** Go watch that Fanvue AI Academy clip before it stops being an outlier. That velocity won't stay 89 views an hour for long.
**Caption:** Go check the outlier. Now.
**Visual:** Video thumbnail of "The 1% YouTube Automation Secret" pinned on screen with a countdown-style clock overlay ticking.

**Data grounding used:** scan_trending.py's single shortlisted candidate ("The 1% YouTube Automation Secret 👀..." by Leo Grundström Clips, robust_z=35.16, velocity 89.2 views/hour) used in Hook and CTA; score.py metrics (rank 1, composite 0.528, breakout_rate 3.3%, capture_index 58.3%, sponsor_density 17.0%) used in Setup and Payoff; BUILD_LOG.yaml entry from 2026-07-20 (commit 04ec622, produce_script.py overwrite bug and --force guard fix) used in Turn.

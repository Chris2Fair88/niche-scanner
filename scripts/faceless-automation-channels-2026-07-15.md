# Video Script -- Faceless YouTube automation channels (meta-niche) -- 2026-07-15

**Target niche:** Faceless YouTube automation channels (meta-niche) (slug: `faceless-automation-channels`)

**Grounding data used:**
- **score.py metrics** (run_date=2026-07-15): rank 1, composite 0.528, breakout_rate 3.3%, trend_slope 0.094, velocity_median_views 11834, capture_index 58.3%, sponsor_density 17.0%, policy_risk 0.02
- **scan_trending.py shortlist**: none available for this niche as of trend_date=2026-07-20.
- **BUILD_LOG.yaml recent project history** (5 entries, not niche-specific -- real events from building this pipeline itself):
  - [2026-07-17, commit 95cf871, bug_fix] report.py silently returned an empty or truncated narrative -- confirmed nondeterministic, since a second identical call failed a different way. Why it mattered: Claude Sonnet 5 runs adaptive thinking by default when the thinking parameter is omitted, and max_tokens caps thinking plus visible output combined -- so thinking alone could consume the entire budget and leave nothing for the actual report. Fixed by explicitly disabling thinking for this deterministic-synthesis task and raising max_tokens to give the (also larger, newer-tokenizer) output real headroom.
  - [2026-07-17, commit 910b693, bug_fix] Building scan_trending.py surfaced a real cross-day quota bug: quota_log was keyed by run_date, a data-folder label deliberately decoupled from real calendar time -- but Google's 100-calls/day search.list cap resets on real time, not on that label. Why it mattered: A script reused against an older, frozen run_date on a genuinely different real day would have seen stale usage from unrelated days counted as "today's" spend. Added a real_date column, set from the system clock at logging time and never from run_date, and switched quota enforcement to key on it instead -- verified by mocking a future real date under the same run_date and confirming it saw zero stale usage.
  - [2026-07-17, commit e6e15be, bug_fix] reddit-story-narration surfaced only 12 of a targeted 30 channels during ingestion, but its metrics (95.8% capture_index, 352 median velocity) were presented in the report as plain fact with no indication the sample was unusually thin. Why it mattered: Every one of those 12 channels was taken forward with zero filtering headroom, making metrics like capture_index far more sensitive to any single channel than in a fully-sampled niche. Added a caveat that triggers on any niche whose sampled channel count falls under half its target -- not hardcoded to this one niche -- so it catches the same problem automatically in the future.
  - [2026-07-20, commit 2e4d415, feature] Added produce_script.py: generates a shot-list-style video script for a niche, grounded in whatever real score.py metrics and scan_trending.py trending data actually exist for it. Why it mattered: The whole point was avoiding a templated, easily-flagged pattern -- a generic prompt with the topic swapped in. The script is required to cite at least one specific, real data point, enforced in the prompt and, when no real data exists for a niche at all, by refusing to generate rather than quietly producing something ungrounded.
  - [2026-07-20, commit 04ec622, bug_fix] produce_script.py was found to silently overwrite an existing generated script for the same niche+run_date -- no warning, no --force requirement, and the regeneration wasn't even deterministic (a re-run against identical grounding data produced a different script every time). Why it mattered: That meant a second run could destroy a committed script with zero trace of what was lost. Added a guard that refuses by default and requires an explicit --force, checked before any API call is made so a refusal costs nothing.

## Script

## Hook

**Voiceover:** Rank 1 in this niche, but the breakout rate is just 3.3 percent.
**Caption:** #1 doesn't mean easy.
**Visual:** Bold on-screen number "3.3%" slams in over a stock clip of a faceless YouTube dashboard, view counters spinning.

## Setup

**Voiceover:** Faceless automation channels top the leaderboard right now — composite score 0.528, capture index over 58 percent. So why does almost nothing actually break out?
**Caption:** Top score. Low breakout odds.
**Visual:** Split screen: left side shows a composite score gauge filling to 0.528, right side shows a nearly empty "breakout" meter at 3.3%.

## Turn

**Voiceover:** Here's the twist — we found this same pattern building the tool that measures this stuff. One niche's report looked airtight, 95.8% capture, until we realized it only sampled 12 of 30 target channels.
**Caption:** The metric was real. The sample wasn't.
**Visual:** Screen recording style: a code diff scrolling, highlighting the "sampled channel count" caveat check, red warning text appearing.

## Payoff

**Voiceover:** A high score can hide a thin sample or a low breakout rate. Always check what's under the number, not just the number.
**Caption:** Check the sample size, not just the score.
**Visual:** Zoom out to a dashboard mockup with "composite 0.528 / breakout 3.3% / capture 58.3%" all displayed together, one flagged with a caution icon.

## CTA

**Voiceover:** Before you copy a "top-ranked" niche, ask what breakout rate is actually hiding underneath it.
**Caption:** Rank ≠ reliable. Ask the follow-up question.
**Visual:** End card: "Rank #1 ≠ Easy Win" with a arrow pointing to breakout_rate 3.3%.

**Data grounding used:** score.py metrics for faceless-automation-channels (rank 1, composite 0.528, breakout_rate 3.3%, capture_index 58.3%) used throughout Hook/Setup/Payoff/CTA; BUILD_LOG.yaml entry from 2026-07-17 (commit e6e15be) about the reddit-story-narration channel undersampling (12 of 30 channels, 95.8% capture_index) used in the Turn as the real narrative pivot. No scan_trending.py data was available for this niche, so none was used or fabricated.

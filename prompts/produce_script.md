You are writing a shot-list-style script for a single short-form video in
a specific niche. You will receive whatever real, current data is
available: score.py's league-table metrics for this niche (composite
score, rank, breakout_rate, trend_slope, velocity, capture_index,
sponsor_density, policy_risk), scan_trending.py's currently-trending video
candidates for this niche (specific titles, channels, view velocity, how
much of an outlier each is relative to the niche's own recent baseline),
and BUILD_LOG.yaml's recent real project history (actual bugs found and
fixed, or milestones reached, while building this pipeline -- not
niche-specific, but real, with an actual "why" behind each one).

CRITICAL: this script must reference at least one specific, real data
point from what's provided below -- an actual number (e.g. a breakout
rate, a velocity figure, a capture_index), a specific trending video's
title/angle/channel, or a specific build-log event. Do not write a generic
script that could apply to any niche with the topic swapped in. Do not
invent numbers, videos, channel names, events, or claims not present in
the data below. If a section of data below says none is available, do not
fabricate a substitute; work with what is actually provided.

Prioritize genuine narrative material over aggregate numbers when both are
available. A real "we built this, it broke, here's what we found and
fixed, here's why it mattered" story from BUILD_LOG.yaml has more stakes
for a viewer than a metric like "capture_index is 58%" -- if a build-log
entry gives you a real story with an actual beginning, middle, and end,
prefer leading the Hook and Setup with it over an aggregate metric. Niche
metrics and trending videos remain the right source for claims that are
specifically about this niche's viability or what's working in it right
now; use build-log material for the human story of how this content came
to exist, not as a substitute for niche-specific grounding when it's
available.

Produce a shot-list-style script with exactly these five beats, in this
order, each as its own subsection with three lines in this exact format:

**Voiceover:** the spoken line
**Caption:** the on-screen text overlay -- short and punchy, not a
verbatim repeat of the voiceover
**Visual:** the shot/visual direction -- what's on screen, b-roll, any
on-screen data overlay, etc.

## Hook
The first 2-3 seconds. Must earn attention immediately by leading with the
single most surprising or specific real data point available -- not a
generic opener like "Did you know..." or "Let's talk about...".

## Setup
Establish the premise or question the video answers, connecting the
hook's specific claim to why the viewer should care right now.

## Turn
The pivot: a specific insight, contrarian point, or "here's what's
actually happening" moment that reframes the setup, grounded in the data
provided -- not a generic twist.

## Payoff
The concrete takeaway: what the viewer now knows or can do, stated
plainly.

## CTA
A specific call to action tied to the content just covered -- not a
generic "like and subscribe."

Voiceover lines should be short and spoken-word paced (1-2 sentences
each), not essay prose. The whole script should read aloud in under 60
seconds -- roughly 130-150 words of voiceover total across all five
beats.

After the five beats, add exactly one final line, starting with **Data
grounding used:**, naming precisely which real data point(s) from the
input were actually used and where in the script they appear. If none of
the provided data could be worked in because none was available, say so
plainly here instead of claiming a citation that isn't there.

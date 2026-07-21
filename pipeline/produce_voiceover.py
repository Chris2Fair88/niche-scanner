"""Phase 5 (ad hoc) -- voiceover audio generation.

Converts an already-generated script (scripts/<niche-slug>-<run_date>.md,
produce_script.py's output) into real voiceover audio via ElevenLabs'
"convert with timestamps" endpoint
(POST /v1/text-to-speech/{voice_id}/with-timestamps). That endpoint
returns audio and character-level start/end timing in a single call --
used specifically because it sets up caption-sync as a near-free
follow-on later, without a second API call or a separate alignment step.

This does NOT generate new voiceover text -- it parses the five
already-reviewed Voiceover lines (Hook/Setup/Turn/Payoff/CTA) out of the
markdown script and converts each to speech as-is. Consuming the
already-reviewed script, not creating new content, is the point.

Each beat is its own API call, not one call for the whole script
concatenated together: partly to respect ElevenLabs' per-request
character limits, but mainly so a failure on one beat (network error, a
transient 5xx, a bad voice_id) doesn't lose every other beat's audio too.

Same niche/run_date resolution as produce_script.py -- reuses
resolve_niche_slug() directly rather than re-implementing it.

Output: audio/<niche-slug>-<run_date>/ -- one .mp3 + one
.alignment.json per beat, plus a manifest.json tying beat order to
files and durations. audio/ is gitignored (generated binary media, same
treatment as data/) -- the alignment JSON is the one thing in there
that's expensive to regenerate (it costs an API call), which is exactly
why it's saved alongside the audio rather than discarded after use.
"""
from __future__ import annotations

import argparse
import base64
import os
import re
import sys

import requests

from pipeline import common
from pipeline.produce_script import resolve_niche_slug

log = common.get_logger("produce_voiceover")

API_BASE = "https://api.elevenlabs.io"
BEATS = ["Hook", "Setup", "Turn", "Payoff", "CTA"]


class ElevenLabsError(RuntimeError):
    pass


def parse_voiceover_lines(script_md: str) -> dict[str, str]:
    """Extracts each beat's **Voiceover:** line from the markdown script.
    Splits on markdown '## ' headers first (keeping each beat's body
    text bounded by the next header) rather than searching the whole
    document per beat -- avoids a non-greedy regex accidentally matching
    across into a different beat's Voiceover line."""
    parts = re.split(r"^##\s+(.+?)\s*$", script_md, flags=re.MULTILINE)
    # re.split with a capturing group yields [pre, heading1, body1, heading2, body2, ...]
    lines: dict[str, str] = {}
    for i in range(1, len(parts), 2):
        heading = parts[i].strip()
        body = parts[i + 1] if i + 1 < len(parts) else ""
        if heading not in BEATS:
            continue
        m = re.search(r"\*\*Voiceover:\*\*\s*(.+)", body)
        if m:
            lines[heading] = m.group(1).strip()
    return lines


def resolve_default_voice(api_key: str) -> str:
    """No voice_id configured -- resolve one from the account's actually
    available voices via GET /v2/voices, rather than hardcoding a
    specific voice_id that might not exist on this account's tier.
    Free-tier accounts don't necessarily have access to every premade or
    cloned voice, so guessing an ID risks a 404 that has nothing to do
    with the rest of this script working correctly."""
    resp = requests.get(
        f"{API_BASE}/v2/voices",
        headers={"xi-api-key": api_key},
        params={"page_size": 1},
        timeout=30,
    )
    if resp.status_code != 200:
        raise ElevenLabsError(f"Could not list voices (HTTP {resp.status_code}): {resp.text[:300]}")
    voices = resp.json().get("voices", [])
    if not voices:
        raise ElevenLabsError(
            "Account has no available voices -- set elevenlabs.voice_id explicitly in config.yaml."
        )
    return voices[0]["voice_id"]


def call_elevenlabs_with_timestamps(api_key: str, voice_id: str, model_id: str, output_format: str, text: str) -> dict:
    resp = requests.post(
        f"{API_BASE}/v1/text-to-speech/{voice_id}/with-timestamps",
        headers={"xi-api-key": api_key, "Content-Type": "application/json", "Accept": "application/json"},
        params={"output_format": output_format},
        json={"text": text, "model_id": model_id},
        timeout=60,
    )
    if resp.status_code != 200:
        raise ElevenLabsError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def run(run_date: str, topic: str | None, voice_id_override: str | None) -> str:
    cfg = common.load_config()
    ecfg = cfg.get("elevenlabs", {})
    api_key_env = ecfg.get("api_key_env", "ELEVENLABS_API_KEY")

    api_key = os.environ.get(api_key_env)
    if not api_key:
        log.error("Set the %s environment variable before running.", api_key_env)
        sys.exit(1)

    conn = common.connect_db(run_date)
    niche_slug, niche_label = resolve_niche_slug(topic, conn)
    conn.close()

    script_path = common.SCRIPTS_DIR / f"{niche_slug}-{run_date}.md"
    if not script_path.exists():
        log.error(
            "No script found at %s -- run produce_script.py for this niche/run-date first.",
            script_path,
        )
        sys.exit(1)

    script_md = script_path.read_text(encoding="utf-8")
    voiceover_lines = parse_voiceover_lines(script_md)

    missing = [b for b in BEATS if b not in voiceover_lines]
    if missing:
        log.error(
            "Could not parse a Voiceover line for beat(s) %s from %s -- expected all five "
            "beats (%s). Refusing to generate partial/mismatched audio.",
            missing, script_path, BEATS,
        )
        sys.exit(1)

    voice_id = voice_id_override or ecfg.get("voice_id")
    if not voice_id:
        try:
            voice_id = resolve_default_voice(api_key)
        except ElevenLabsError as e:
            log.error("Could not resolve a default voice: %s", e)
            sys.exit(1)
        log.info("No voice_id configured -- resolved '%s' from the account's available voices.", voice_id)

    model_id = ecfg.get("model_id", "eleven_multilingual_v2")
    output_format = ecfg.get("output_format", "mp3_44100_128")
    ext = "mp3" if output_format.startswith("mp3") else output_format.split("_")[0]

    out_dir = common.AUDIO_DIR / f"{niche_slug}-{run_date}"
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "niche_slug": niche_slug,
        "niche_label": niche_label,
        "run_date": run_date,
        "voice_id": voice_id,
        "model_id": model_id,
        "output_format": output_format,
        "beats": [],
    }
    failed_beats = []

    for i, beat in enumerate(BEATS, start=1):
        text = voiceover_lines[beat]
        base_name = f"{i:02d}-{beat.lower()}"
        log.info("Generating voiceover for beat '%s' (%d chars)...", beat, len(text))
        try:
            result = call_elevenlabs_with_timestamps(api_key, voice_id, model_id, output_format, text)
        except ElevenLabsError as e:
            log.error("Beat '%s' failed: %s", beat, e)
            failed_beats.append(beat)
            continue

        audio_bytes = base64.b64decode(result["audio_base64"])
        audio_path = out_dir / f"{base_name}.{ext}"
        audio_path.write_bytes(audio_bytes)

        alignment_path = out_dir / f"{base_name}.alignment.json"
        common.write_json(alignment_path, {
            "alignment": result.get("alignment"),
            "normalized_alignment": result.get("normalized_alignment"),
        })

        end_times = (result.get("alignment") or {}).get("character_end_times_seconds") or [0.0]
        duration = end_times[-1]

        manifest["beats"].append({
            "beat": beat,
            "index": i,
            "text": text,
            "audio_file": audio_path.name,
            "alignment_file": alignment_path.name,
            "duration_seconds": duration,
        })
        log.info("  -> wrote %s (%.2fs, %d bytes)", audio_path.name, duration, len(audio_bytes))

    common.write_json(out_dir / "manifest.json", manifest)

    if failed_beats:
        log.error(
            "Voiceover generation incomplete -- failed beat(s): %s. Successfully generated "
            "beats' audio/alignment were still written to %s.",
            failed_beats, out_dir,
        )
        sys.exit(1)

    log.info("Voiceover generation complete: %s", out_dir)
    return str(out_dir)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate voiceover audio (with timing) for an existing produce_script.py script",
    )
    parser.add_argument(
        "--run-date", default=common.today_str(),
        help="Which data/{run_date}/scanner.db to resolve the default niche from, and which "
             "scripts/<slug>-<run_date>.md to read",
    )
    parser.add_argument(
        "--topic", default=None,
        help="Niche slug or label from niches.yaml; defaults to the current rank-1 niche for --run-date",
    )
    parser.add_argument(
        "--voice-id", default=None,
        help="Override config.yaml's elevenlabs.voice_id for this run",
    )
    args = parser.parse_args()
    run(args.run_date, args.topic, args.voice_id)


if __name__ == "__main__":
    main()

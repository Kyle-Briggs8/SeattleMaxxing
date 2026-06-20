"""
main.py — orchestrate the weekly digest.

    fetch_all → dedupe → filter (window + region) → categorize → email

Flags:
    --dry-run    print the digest to stdout instead of emailing
    --days N     lookahead window in days (default config.DEFAULT_LOOKAHEAD_DAYS)

Idempotent and safe to re-run. Designed to run unattended via GitHub Actions, so
nothing here is allowed to raise on a single bad source or a bad LLM reply —
those failure modes are swallowed and logged in their respective modules.
"""

import argparse
import logging
import re
import sys
from datetime import datetime, timedelta
from difflib import SequenceMatcher

import config
import sources
import categorize
import email_digest

# Windows consoles default to cp1252 and crash on em-dashes/emoji in our output.
# Force UTF-8 so local dry-runs and logs work everywhere (no-op on Linux CI).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-11s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")


# --------------------------------------------------------------------------- #
# Dedupe — same event cross-listed on two sites
# --------------------------------------------------------------------------- #
def _norm_title(title):
    """Lowercase, strip punctuation/extra space for fuzzy comparison."""
    return re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()


def dedupe(events, threshold=0.85):
    """Drop near-duplicate events (fuzzy title match on the SAME calendar day).

    Keeps the first occurrence. O(n^2) but n is small for a weekly digest.
    """
    kept = []
    for ev in events:
        nt = _norm_title(ev["title"])
        day = ev["start_datetime"].date()
        dup = False
        for k in kept:
            if k["start_datetime"].date() != day:
                continue
            if SequenceMatcher(None, nt, _norm_title(k["title"])).ratio() >= threshold:
                dup = True
                break
        if not dup:
            kept.append(ev)
    removed = len(events) - len(kept)
    if removed:
        log.info("dedupe: removed %d duplicate(s)", removed)
    return kept


# --------------------------------------------------------------------------- #
# Filter — lookahead window + Seattle region
# --------------------------------------------------------------------------- #
def _in_region(ev):
    haystack = f"{ev['title']} {ev['location']}".lower()
    return any(hint in haystack for hint in config.REGION_HINTS)


def filter_events(events, days):
    """Keep events that start within [now, now+days] AND look Seattle-area."""
    now = datetime.now()              # naive local; event datetimes are naive Pacific
    horizon = now + timedelta(days=days)
    # Allow events earlier today (since midnight) so "today" isn't half-dropped.
    floor = now.replace(hour=0, minute=0, second=0, microsecond=0)

    in_window = [e for e in events if floor <= e["start_datetime"] <= horizon]
    log.info("filter: %d/%d within %d-day window", len(in_window), len(events), days)

    in_region = [e for e in in_window if _in_region(e)]
    log.info("filter: %d/%d are Seattle-area", len(in_region), len(in_window))
    return in_region


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run(days, dry_run):
    log.info("=== Seattle digest run: days=%d dry_run=%s ===", days, dry_run)

    events = sources.fetch_all()
    log.info("fetched %d raw events", len(events))

    events = dedupe(events)
    events = filter_events(events, days)

    if not events:
        log.warning("no events after filtering — sending nothing")
        if dry_run:
            print("\n(no events matched the window/region — nothing to send)\n")
        return 0

    events = categorize.categorize(events)

    # Focused digest: drop anything the LLM couldn't place in an enabled
    # category, so the email is purely the categories we care about.
    if config.DROP_UNCATEGORIZED:
        kept = [e for e in events if e.get("primary") != "uncategorized"]
        if len(kept) != len(events):
            log.info("focus: dropped %d out-of-scope event(s)",
                     len(events) - len(kept))
        events = kept

    if not events:
        log.warning("no in-scope events after categorization — nothing to send")
        if dry_run:
            print("\n(no events matched your focus categories this week)\n")
        return 0

    email_digest.build_and_maybe_send(events, days, dry_run)

    log.info("=== done: %d events in digest ===", len(events))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Seattle weekly events digest")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the digest to stdout instead of emailing")
    parser.add_argument("--days", type=int, default=config.DEFAULT_LOOKAHEAD_DAYS,
                        help="lookahead window in days (default %(default)s)")
    args = parser.parse_args(argv)

    try:
        return run(args.days, args.dry_run)
    except Exception:  # noqa: BLE001 — last-ditch guard; never exit nonzero silently
        log.exception("unexpected fatal error")
        return 1


if __name__ == "__main__":
    sys.exit(main())

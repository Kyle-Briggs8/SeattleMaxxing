# CLAUDE.md — Seattle Weekly Events Digest

Project context and standing rules for Claude Code. Read this before editing.

## What this is
A fully hands-off weekly digest. Every Sunday a GitHub Actions cron finds events
happening in/around Seattle over the next 7 days, categorizes them with an LLM,
and emails me a formatted digest. Must run unattended with zero ongoing cost.

## Stack (do not substitute without asking)
- Python 3.11
- Scheduler: GitHub Actions cron — Sundays, `cron: '0 16 * * 0'` (16:00 UTC ≈ 9am PT)
- Categorizer: Google Gemini, model `gemini-2.0-flash` (free tier)
- Email: Resend API (free tier, 3000/mo)
- Secrets: GitHub Actions secrets in CI; python-dotenv locally
  (`GEMINI_API_KEY`, `RESEND_API_KEY`, `EMAIL_TO`, `EMAIL_FROM`)

## File layout
- `config.py` — categories, order, region hints, email/from, model. Single source of truth.
- `sources.py` — per-source fetchers + `fetch_all()`. Each returns normalized dicts.
- `categorize.py` — Gemini batch call + safe JSON parse. ISOLATED on purpose.
- `email_digest.py` — HTML+text builder, Resend send.
- `main.py` — orchestrate: fetch → dedupe → filter → categorize → email.
- `.github/workflows/weekly.yml` — cron + secrets + run.

## Normalized event shape (every source must return this)
```python
{"title": str, "url": str, "start_datetime": datetime,
 "location": str, "description": str, "source": str}
```

## Categories (9, config-driven)
tech, startup_vc, finance, nature, party, music, food_drink, arts, sports.
Each event gets a PRIMARY (used for grouping) + optional SECONDARY.
Categories must be toggleable in `config.py` without touching pipeline logic.

## CRITICAL: source realities — do not regress on these
- **Eventbrite's public event SEARCH API is dead.** It only returns an org's own
  events now. Never reintroduce it as a discovery source.
- **Meetup API is paid.** Skip unless I explicitly add a paid key.
- Working free sources are SCRAPED HTML and therefore FRAGILE:
  - Luma — `https://lu.ma/seattle` (tech / startup / finance / party)
  - EverOut (The Stranger) — `https://everout.com/seattle/events/` (music / arts / food / party)
  - Visit Seattle calendar (optional) — nature / festivals / family
- Selectors WILL break over time. That is expected, not a bug to "fix" by
  removing the source. Update the selector.

## Hard rules for unattended reliability
- Every source wrapped in try/except. One dead source must NEVER kill the run.
- Log how many events each source returned.
- Gemini prompt must demand JSON-only output. Parse defensively: strip ``` fences,
  fall back to `"uncategorized"` on parse failure. Never crash on a bad LLM reply.
- Idempotent and safe to re-run.
- Empty categories are omitted from the email, never rendered blank.

## Required flags (keep working)
- `--dry-run` → print digest to stdout instead of emailing (for testing).
- `--days N` → lookahead window, default 7.

## Filtering
- Keep only events whose `start_datetime` is within the lookahead window.
- Keep only Seattle-area events: match title/location against region hints
  (Seattle, Bellevue, Redmond, Kirkland, Tacoma, Capitol Hill, Fremont, Ballard,
  SLU/South Lake Union, U District).
- Dedupe by fuzzy title + date (same event cross-listed on two sites).

## Swappability
The categorizer is isolated so I can swap Gemini → Groq/Llama 3.3 by editing ONLY
`categorize.py`. Keep that boundary clean; no Gemini-specific code leaks elsewhere.

## Style
Readable, commented at decision points, concise. Don't over-engineer — this is a
once-a-week personal job, not a service. Prefer clarity over cleverness.
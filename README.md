# 🌧️ Seattle Weekly Events Digest

A fully hands-off weekly email digest of events happening in/around Seattle over
the next 7 days. Every Sunday a GitHub Actions cron scrapes a few free event
sources, categorizes everything with an LLM, and emails you a clean digest.
**Zero ongoing cost** — free tiers only.

## How it works

```
fetch_all → dedupe → filter (window + region) → categorize (Gemini) → email (Resend)
```

| File              | Responsibility                                            |
|-------------------|-----------------------------------------------------------|
| `config.py`       | Categories, toggles, order, region hints, email, model    |
| `sources.py`      | Luma / GeekWire / Meetup / Visit Seattle fetchers + `fetch_all()` |
| `categorize.py`   | Gemini batch call + safe JSON parse (**LLM isolated here**)|
| `email_digest.py` | HTML + plaintext builder, Resend send                     |
| `main.py`         | Orchestration + `--dry-run` / `--days` flags              |

## Setup

### 1. Get API keys (all free)
- **Gemini:** https://aistudio.google.com/apikey → `GEMINI_API_KEY`
- **Resend:** https://resend.com/api-keys → `RESEND_API_KEY`
- **Ticketmaster** *(optional, recommended)*: https://developer.ticketmaster.com/
  → create an app → Consumer Key → `TICKETMASTER_API_KEY`. Powers the
  music/arts/sports source. Leave blank and that source simply self-disables.

### 2. Sender address (Resend)
- **Testing:** use `onboarding@resend.dev` as `EMAIL_FROM` — works immediately,
  but can only send to *your own* Resend account email.
- **Production:** verify a domain in Resend (add the DNS records it gives you),
  then use e.g. `Seattle Digest <digest@yourdomain.com>`.

### 3. Run locally
```bash
pip install -r requirements.txt
cp .env.example .env          # fill in your keys
python main.py --dry-run      # prints the digest, sends nothing
python main.py --days 14      # widen the lookahead window
python main.py                # actually emails it
```

### 4. Schedule it (GitHub Actions)
Add these as **repository secrets** (Settings → Secrets and variables → Actions):
`GEMINI_API_KEY`, `RESEND_API_KEY`, `EMAIL_TO`, `EMAIL_FROM`, and optionally
`TICKETMASTER_API_KEY`.

The workflow in `.github/workflows/weekly.yml` then runs automatically. You can
also trigger it manually from the **Actions** tab (with an optional dry-run).

## Schedule / timezone
Cron is `0 16 * * 0` = **16:00 UTC every Sunday**, which is ~**9am Pacific** in
summer (PDT) and ~8am in winter (PST). GitHub cron always runs in UTC, so the
Pacific clock time drifts an hour across daylight saving — that's expected.

## Customizing

- **Turn a category off:** set `"enabled": False` in `config.CATEGORIES`. It
  disappears from both the LLM prompt and the email — no logic changes.
- **Turn a source off:** set `"enabled": False` in `config.SOURCES`.
- **Tune the region filter:** edit `config.REGION_HINTS`.
- **Swap the LLM (Gemini → Groq/Llama):** reimplement `_call_llm()` in
  `categorize.py`. Nothing else touches the provider.

## ⚠️ A note on the scrapers
The only free, working event sources are **scraped HTML**, which is inherently
fragile (Eventbrite's search API is dead; Meetup's is paid). Luma is parsed from
its embedded `__NEXT_DATA__` JSON and EverOut/Visit Seattle from JSON-LD where
available — both sturdier than CSS selectors — but **they will still break when
the sites redesign.** When a source returns 0 events, that's the signal to update
its selector/parser in `sources.py`. **Don't delete the source** — one dead
source never kills the run (each is wrapped in try/except and logged).

### Current source status (as of last test)
- **Luma** ✅ — parsed from `__NEXT_DATA__`, returning events reliably.
- **GeekWire** ✅ — Seattle tech-news events calendar via its WordPress "The
  Events Calendar" REST API (`/wp-json/tribe/events/v1/events`). Clean JSON, no
  key, no scraping. The strongest source for tech.
- **Meetup** ✅ — discovery via the find page (`/find/?keywords=…`), parsed from
  the embedded Apollo/Next.js data (the official API is paid). One request per
  keyword in `config.SOURCES["meetup"]["queries"]` — tuned for startup/VC/finance,
  which mostly live on Meetup. Edit those keywords to retarget what it pulls.
- **Ticketmaster** ✅ — official Discovery JSON API (not a scraper, so it won't
  break on redesigns). Covers concerts/arts/sports within 25mi of Seattle.
  Requires the free `TICKETMASTER_API_KEY`; self-disables without it.
- **Visit Seattle** ✅ — parsed from `.event` cards. Note its `/events/` page
  skews toward far-future *featured* events, so few land in a 7-day window.
- **EverOut** ❌ (disabled) — returns **HTTP 202 with a JS bot-challenge body**
  (AWS WAF). Plain `requests` can't pass it; defeating it would need a headless
  browser (e.g. Playwright), deliberately avoided to stay free/light. Replaced
  by Ticketmaster for music/arts. Left in `config.SOURCES` (disabled) so it can
  be re-enabled if the block is ever lifted.

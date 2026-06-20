"""
sources.py — per-source scrapers + fetch_all().

Every fetcher returns a list of normalized dicts:
    {"title": str, "url": str, "start_datetime": datetime (naive, Pacific),
     "location": str, "description": str, "source": str}

Hard rule (CLAUDE.md): one dead source must NEVER kill the run. fetch_all wraps
each enabled source in try/except and logs how many events it returned. Scrapers
are inherently fragile — when selectors break, UPDATE them, don't delete the
source.

Two extraction strategies are used because they degrade gracefully:
  1. __NEXT_DATA__ JSON   — Luma is a Next.js app; its events live in embedded
                            JSON, far sturdier than scraping rendered DOM.
  2. JSON-LD schema.org   — Many event/calendar sites embed <script
     "Event" objects        type="application/ld+json"> Event blocks. Robust and
                            cross-site. HTML-selector parsing is the fallback.
"""

import html
import json
import logging
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser

try:  # stdlib on 3.9+; America/Los_Angeles needs tzdata on Windows (in reqs)
    from zoneinfo import ZoneInfo
    _PACIFIC = ZoneInfo("America/Los_Angeles")
except Exception:  # pragma: no cover - defensive; fall back to naive UTC-ish
    _PACIFIC = None

import config

log = logging.getLogger("sources")


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _http_get(url):
    """GET with our UA + timeout. Raises on HTTP error so callers can log it."""
    resp = requests.get(
        url,
        headers={"User-Agent": config.HTTP_USER_AGENT,
                 "Accept-Language": "en-US,en;q=0.9"},
        timeout=config.HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.text


def _to_naive_pacific(dt):
    """Normalize any datetime to NAIVE Pacific so comparisons never mix tz.

    Aware datetimes are converted to America/Los_Angeles then stripped of tz.
    Naive datetimes are assumed already local and returned unchanged.
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        if _PACIFIC is not None:
            dt = dt.astimezone(_PACIFIC)
        return dt.replace(tzinfo=None)
    return dt


def _parse_dt(value):
    """Best-effort parse of a date/datetime from a string or epoch. None on fail."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            # Heuristic: ms vs s epoch.
            ts = value / 1000.0 if value > 1e11 else value
            return _to_naive_pacific(datetime.fromtimestamp(ts, tz=_PACIFIC) if _PACIFIC
                                     else datetime.fromtimestamp(ts))
        return _to_naive_pacific(dtparser.parse(str(value)))
    except (ValueError, OverflowError, TypeError):
        return None


def _clean(text):
    """Decode HTML entities + collapse whitespace into one tidy line."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", html.unescape(str(text))).strip()


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text):
    """Remove HTML tags (then _clean handles entities/whitespace)."""
    if not text:
        return ""
    return _clean(_TAG_RE.sub(" ", str(text)))


def _normalize(title, url, start_datetime, location, description, source):
    """Assemble the canonical event dict, or None if unusable (no title/date)."""
    title = _clean(title)
    if not title or start_datetime is None:
        return None
    return {
        "title": title,
        "url": (url or "").strip(),
        "start_datetime": start_datetime,
        "location": _clean(location),
        "description": _clean(description)[:300],  # one-liner; trim long blurbs
        "source": source,
    }


# --------------------------------------------------------------------------- #
# Generic JSON-LD schema.org Event extractor
# --------------------------------------------------------------------------- #
def _walk_jsonld(node, out):
    """Recursively collect dicts whose @type is (or includes) 'Event'."""
    if isinstance(node, dict):
        t = node.get("@type")
        types = t if isinstance(t, list) else [t]
        if any(isinstance(x, str) and "Event" in x for x in types if x):
            out.append(node)
        # @graph and nested containers
        for v in node.values():
            _walk_jsonld(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_jsonld(v, out)


def _jsonld_location(node):
    """Pull a human-readable location string out of a schema.org 'location'."""
    loc = node.get("location")
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if isinstance(loc, str):
        return loc
    if isinstance(loc, dict):
        name = loc.get("name", "")
        addr = loc.get("address", "")
        if isinstance(addr, dict):
            addr = " ".join(
                str(addr.get(k, "")) for k in
                ("streetAddress", "addressLocality", "addressRegion")
            )
        return _clean(f"{name} {addr}")
    return ""


def extract_jsonld_events(html, source, base_url):
    """Parse all JSON-LD Event blocks from an HTML page into normalized dicts."""
    soup = BeautifulSoup(html, "html.parser")
    raw = []
    for tag in soup.find_all("script", type="application/ld+json"):
        if not tag.string:
            continue
        try:
            data = json.loads(tag.string)
        except (json.JSONDecodeError, TypeError):
            continue
        _walk_jsonld(data, raw)

    events = []
    for node in raw:
        url = node.get("url") or base_url
        if url and url.startswith("/"):
            url = requests.compat.urljoin(base_url, url)
        ev = _normalize(
            title=node.get("name"),
            url=url,
            start_datetime=_parse_dt(node.get("startDate")),
            location=_jsonld_location(node),
            description=node.get("description"),
            source=source,
        )
        if ev:
            events.append(ev)
    return events


# --------------------------------------------------------------------------- #
# Source: Luma  (https://lu.ma/seattle)  — __NEXT_DATA__ JSON
# --------------------------------------------------------------------------- #
def _walk_luma_events(node, out):
    """Collect dicts that look like Luma events: have a name + start_at time."""
    if isinstance(node, dict):
        if node.get("name") and (node.get("start_at") or node.get("startAt")):
            out.append(node)
        for v in node.values():
            _walk_luma_events(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_luma_events(v, out)


def fetch_luma(url):
    html = _http_get(url)
    soup = BeautifulSoup(html, "html.parser")

    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag or not tag.string:
        log.warning("luma: __NEXT_DATA__ not found — page structure changed?")
        return []

    data = json.loads(tag.string)
    raw = []
    _walk_luma_events(data, raw)

    events, seen = [], set()
    for node in raw:
        api_id = node.get("api_id") or node.get("url")
        if api_id and api_id in seen:
            continue
        seen.add(api_id)

        slug = node.get("url") or ""
        event_url = (slug if slug.startswith("http")
                     else f"https://lu.ma/{slug}" if slug else url)

        # Location lives under varying keys depending on Luma's data version.
        geo = node.get("geo_address_info") or {}
        location = (geo.get("full_address") or geo.get("address")
                    or geo.get("city_state") or geo.get("city") or "")

        ev = _normalize(
            title=node.get("name"),
            url=event_url,
            start_datetime=_parse_dt(node.get("start_at") or node.get("startAt")),
            location=location,
            description=node.get("description") or node.get("one_liner") or "",
            source="luma",
        )
        if ev:
            events.append(ev)
    return events


# --------------------------------------------------------------------------- #
# Source: EverOut / The Stranger  (https://everout.com/seattle/events/)
# --------------------------------------------------------------------------- #
def fetch_everout(url):
    html = _http_get(url)

    # Primary: JSON-LD Event blocks (EverOut emits these per listing).
    events = extract_jsonld_events(html, "everout", url)
    if events:
        return events

    # Fallback: parse event cards from rendered HTML. SELECTORS WILL DRIFT —
    # update them here when EverOut redesigns; do not drop the source.
    log.info("everout: no JSON-LD, falling back to HTML selectors")
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for card in soup.select(".event, .event-list-item, article.calendar-item"):
        link = card.select_one("a[href]")
        if not link:
            continue
        title = link.get_text(strip=True) or card.get("data-title", "")
        href = link.get("href", "")
        if href.startswith("/"):
            href = requests.compat.urljoin(url, href)
        date_el = card.select_one("time, .date, .event-date")
        when = (date_el.get("datetime") if date_el and date_el.has_attr("datetime")
                else date_el.get_text(strip=True) if date_el else None)
        loc_el = card.select_one(".location, .venue, .event-location")
        ev = _normalize(
            title=title, url=href, start_datetime=_parse_dt(when),
            location=loc_el.get_text(strip=True) if loc_el else "",
            description=card.get("data-description", ""), source="everout",
        )
        if ev:
            out.append(ev)
    return out


# --------------------------------------------------------------------------- #
# Source: Visit Seattle  (optional — nature / festivals / family)
# --------------------------------------------------------------------------- #
_DATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")


def fetch_visit_seattle(url):
    html = _http_get(url)

    # Try JSON-LD first in case they add it; otherwise parse the .event cards.
    events = extract_jsonld_events(html, "visit_seattle", url)
    if events:
        return events

    soup = BeautifulSoup(html, "html.parser")
    out = []
    # Each listing is a `.event` card: h4>a = title/link, the two h6s are venue
    # then date (M/D/YYYY), p = blurb. SELECTORS WILL DRIFT — update here.
    for card in soup.select(".event"):
        link = card.select_one("h4 a[href], a[href]")
        if not link:
            continue
        href = link.get("href", "")
        if href.startswith("/"):
            href = requests.compat.urljoin(url, href)

        h6s = [h.get_text(strip=True) for h in card.select("h6")]
        location = next((t for t in h6s if not _DATE_RE.search(t)), "Seattle")
        date_txt = next((t for t in h6s if _DATE_RE.search(t)), None)
        desc_el = card.select_one("p")

        ev = _normalize(
            title=link.get_text(strip=True),
            url=href,
            start_datetime=_parse_dt(date_txt),
            location=location,
            description=desc_el.get_text(strip=True) if desc_el else "",
            source="visit_seattle",
        )
        if ev:
            out.append(ev)
    return out


# --------------------------------------------------------------------------- #
# Source: Ticketmaster Discovery API  (concerts / arts / sports)
# --------------------------------------------------------------------------- #
# Official JSON API, not a scraper — stable across site redesigns. Replaces the
# WAF-blocked EverOut for music/arts coverage and adds pro sports. Free tier.
def fetch_ticketmaster(url):
    if not config.TICKETMASTER_API_KEY:
        log.info("ticketmaster: no TICKETMASTER_API_KEY — source disabled")
        return []

    resp = requests.get(
        url,
        params={
            "apikey": config.TICKETMASTER_API_KEY,
            "city": "Seattle",          # radius pulls in Bellevue/Redmond/etc.
            "radius": "25", "unit": "miles",
            "countryCode": "US",
            "size": "100", "sort": "date,asc",
        },
        headers={"User-Agent": config.HTTP_USER_AGENT},
        timeout=config.HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    out = []
    for ev in data.get("_embedded", {}).get("events", []):
        dates = ev.get("dates", {}).get("start", {})
        # Prefer full dateTime (UTC); fall back to localDate (all-day listings).
        when = _parse_dt(dates.get("dateTime") or dates.get("localDate"))

        venue, city = "", ""
        venues = ev.get("_embedded", {}).get("venues", [])
        if venues:
            venue = venues[0].get("name", "")
            city = (venues[0].get("city", {}) or {}).get("name", "")
        location = _clean(f"{venue} {city}".strip()) or "Seattle"

        # Ticketmaster rarely gives prose; surface its segment/genre instead so
        # the categorizer (and reader) has a useful signal.
        cls = (ev.get("classifications") or [{}])[0]
        seg = (cls.get("segment") or {}).get("name", "")
        genre = (cls.get("genre") or {}).get("name", "")
        desc = ev.get("info") or _clean(f"{seg} {genre}".strip())

        norm = _normalize(
            title=ev.get("name"), url=ev.get("url", ""), start_datetime=when,
            location=location, description=desc, source="ticketmaster",
        )
        if norm:
            out.append(norm)
    return out


# --------------------------------------------------------------------------- #
# Source: GeekWire  (https://www.geekwire.com/events/)  — Tribe REST API
# --------------------------------------------------------------------------- #
# Seattle's tech-news events calendar — squarely tech/startup/finance, which is
# exactly this digest's focus. Runs the WordPress "The Events Calendar" plugin,
# whose REST API gives clean JSON (no key, no scraping). `url` in config points
# at the wp-json endpoint.
def fetch_geekwire(url):
    # Pull a generous upcoming window; main.py's filter narrows to --days.
    today = (datetime.now(_PACIFIC) if _PACIFIC else datetime.now()).strftime("%Y-%m-%d")
    resp = requests.get(
        url,
        params={"per_page": 50, "start_date": today},
        headers={"User-Agent": config.HTTP_USER_AGENT},
        timeout=config.HTTP_TIMEOUT,
    )
    resp.raise_for_status()

    out = []
    for e in resp.json().get("events", []):
        venue = e.get("venue") or {}
        location = _clean(f"{venue.get('venue', '')} {venue.get('city', '')}".strip())
        ev = _normalize(
            title=e.get("title"),
            url=e.get("url", ""),
            start_datetime=_parse_dt(e.get("start_date")),  # already Pacific-local
            location=location,
            description=_strip_html(e.get("description") or e.get("excerpt")),
            source="geekwire",
        )
        if ev:
            out.append(ev)
    return out


# --------------------------------------------------------------------------- #
# Source: Meetup  (discovery via find page — startup / VC / finance)
# --------------------------------------------------------------------------- #
# Meetup's official API is paid, but its find page is a Next.js app that ships an
# Apollo cache of search results in __NEXT_DATA__. We read that — one request per
# keyword — to discover the startup/finance events that mostly live on Meetup.
def _find_apollo_state(node):
    """Recursively locate the __APOLLO_STATE__ dict inside __NEXT_DATA__."""
    if isinstance(node, dict):
        if "__APOLLO_STATE__" in node:
            return node["__APOLLO_STATE__"]
        for v in node.values():
            found = _find_apollo_state(v)
            if found:
                return found
    elif isinstance(node, list):
        for v in node:
            found = _find_apollo_state(v)
            if found:
                return found
    return None


def _parse_meetup_events(html):
    """Yield (event_id, normalized_dict) from a Meetup find-page response."""
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag or not tag.string:
        return []

    apollo = _find_apollo_state(json.loads(tag.string)) or {}
    results = []
    for key, e in apollo.items():
        if not key.startswith("Event:") or not isinstance(e, dict):
            continue
        # Venue may be inline or an Apollo reference ({"__ref": "Venue:..."}).
        venue = e.get("venue") or {}
        if isinstance(venue, dict) and "__ref" in venue:
            venue = apollo.get(venue["__ref"], {})
        location = _clean(" ".join(str(venue.get(k, "")) for k in
                                   ("name", "address", "city"))) if isinstance(venue, dict) else ""

        ev = _normalize(
            title=e.get("title"),
            url=e.get("eventUrl", ""),
            start_datetime=_parse_dt(e.get("dateTime")),  # ISO w/ -07:00 offset
            location=location,
            description=e.get("description", ""),
            source="meetup",
        )
        if ev:
            results.append((e.get("id") or key, ev))
    return results


def fetch_meetup(url):
    cfg = config.SOURCES.get("meetup", {})
    location = cfg.get("location", "us--wa--Seattle")
    queries = cfg.get("queries", [])

    seen, out = set(), []
    for q in queries:
        try:
            resp = requests.get(
                url,
                params={"keywords": q, "source": "EVENTS", "location": location},
                headers={"User-Agent": config.HTTP_USER_AGENT},
                timeout=config.HTTP_TIMEOUT,
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 — one bad query ≠ dead source
            log.warning("meetup: query %r failed (%s)", q, exc)
            continue

        for eid, ev in _parse_meetup_events(resp.text):
            if eid in seen:        # same event surfaced by multiple keywords
                continue
            seen.add(eid)
            out.append(ev)
    return out


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
_FETCHERS = {
    "luma": fetch_luma,
    "geekwire": fetch_geekwire,
    "meetup": fetch_meetup,
    "ticketmaster": fetch_ticketmaster,
    "everout": fetch_everout,
    "visit_seattle": fetch_visit_seattle,
}


def fetch_all():
    """Run every enabled source. Returns merged list of normalized events.

    Each source is isolated: a failure is logged and skipped, never raised.
    """
    all_events = []
    for name, cfg in config.SOURCES.items():
        if not cfg.get("enabled"):
            log.info("source %-14s: disabled, skipping", name)
            continue
        fetcher = _FETCHERS.get(name)
        if fetcher is None:
            log.warning("source %-14s: no fetcher registered", name)
            continue
        try:
            events = fetcher(cfg["url"])
            log.info("source %-14s: %d events", name, len(events))
            all_events.extend(events)
        except Exception as exc:  # noqa: BLE001 — must never kill the run
            log.warning("source %-14s: FAILED (%s) — skipping", name, exc)
    return all_events

"""
config.py — single source of truth for the digest.

Everything tweakable lives here so the pipeline code (sources/categorize/email)
never needs editing to turn a category off, retune the region filter, or point
at a different model. Keep behavior knobs here, not scattered in logic.
"""

import os

from dotenv import load_dotenv

# Load .env for local runs. In GitHub Actions the secrets are injected as real
# environment variables, so load_dotenv() simply finds nothing and is a no-op.
load_dotenv()


# --------------------------------------------------------------------------- #
# Categories
# --------------------------------------------------------------------------- #
# Each category: human label + a short description that is fed verbatim to the
# LLM so it knows what belongs where. Flip "enabled" to False to drop a category
# from BOTH the categorizer prompt and the rendered email — no logic changes.
CATEGORIES = {
    "tech":       {"label": "Tech",            "enabled": True,
                   "description": "talks, hackathons, product launches, AI/ML meetups, developer events"},
    "startup_vc": {"label": "Startup & VC",    "enabled": True,
                   "description": "pitch nights, demo days, founder mixers, accelerator/VC events"},
    "finance":    {"label": "Finance",         "enabled": True,
                   "description": "fintech, investing talks, professional/finance networking"},
    "nature":     {"label": "Nature & Outdoors","enabled": True,
                   "description": "hikes, kayaking, trail events, park programming, climbing, cycling"},
    "party":      {"label": "Parties & Nightlife","enabled": True,
                   "description": "DJ sets, club nights, warehouse events, bar crawls, late-night"},
    "music":      {"label": "Music",           "enabled": True,
                   "description": "concerts, open mics, live shows, music festivals"},
    "food_drink": {"label": "Food & Drink",    "enabled": True,
                   "description": "tastings, breweries/distilleries, food festivals, pop-ups, dinners"},
    "arts":       {"label": "Arts & Culture",  "enabled": True,
                   "description": "galleries, theater, film screenings, museum nights, readings"},
    "sports":     {"label": "Sports",          "enabled": True,
                   "description": "run clubs, pickup leagues, races, Sounders/Mariners/Kraken games"},
}

# Fixed render + grouping order for the email. Anything not listed (or disabled)
# is skipped. "uncategorized" is handled separately and always rendered last.
CATEGORY_ORDER = [
    "tech", "startup_vc", "finance", "nature",
    "party", "music", "food_drink", "arts", "sports",
]


def enabled_categories():
    """Return {key: {...}} for enabled categories only, in CATEGORY_ORDER."""
    return {
        key: CATEGORIES[key]
        for key in CATEGORY_ORDER
        if key in CATEGORIES and CATEGORIES[key]["enabled"]
    }


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #
# Toggle individual scrapers. Each maps to a fetcher in sources.py.
# NOTE (see CLAUDE.md): Eventbrite search API is dead and Meetup is paid — the
# only working free sources are scraped HTML and are FRAGILE by nature.
SOURCES = {
    "luma":          {"enabled": True,  "url": "https://lu.ma/seattle"},
    # Ticketmaster Discovery API — official, stable JSON (concerts/arts/sports).
    # Free key required; without it this source self-disables (logs + returns 0).
    "ticketmaster":  {"enabled": True,  "url": "https://app.ticketmaster.com/discovery/v2/events.json"},
    "visit_seattle": {"enabled": True,  "url": "https://visitseattle.org/events/"},
    # EverOut sits behind an AWS WAF JS bot-challenge (HTTP 202, empty body) that
    # plain HTTP can't pass. Left here, disabled, so it resumes if unblocked.
    "everout":       {"enabled": False, "url": "https://everout.com/seattle/events/"},
}


# --------------------------------------------------------------------------- #
# Region filter
# --------------------------------------------------------------------------- #
# An event is kept only if its title or location mentions one of these. Keep
# lowercase; matching is case-insensitive substring. Add neighborhoods as needed.
REGION_HINTS = [
    "seattle", "bellevue", "redmond", "kirkland", "tacoma", "everett",
    "renton", "bothell", "issaquah", "shoreline",
    "capitol hill", "fremont", "ballard", "wallingford", "georgetown",
    "south lake union", "slu", "u district", "university district",
    "pioneer square", "belltown", "queen anne", "west seattle", "sodo",
    "columbia city", "greenwood", "ravenna", "magnolia",
]


# --------------------------------------------------------------------------- #
# LLM / categorizer
# --------------------------------------------------------------------------- #
# Only categorize.py should read these. Swapping providers = edit categorize.py.
GEMINI_MODEL = "gemini-2.0-flash"


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #
EMAIL_TO = os.getenv("EMAIL_TO", "")
# Resend requires a verified domain. Use onboarding@resend.dev for testing.
EMAIL_FROM = os.getenv("EMAIL_FROM", "Seattle Digest <onboarding@resend.dev>")
EMAIL_SUBJECT_PREFIX = "🌧️ Seattle This Week"

# Secrets (read here so the rest of the code imports from one place).
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
# Optional: free key from https://developer.ticketmaster.com/. Empty = source off.
TICKETMASTER_API_KEY = os.getenv("TICKETMASTER_API_KEY", "")


# --------------------------------------------------------------------------- #
# Misc
# --------------------------------------------------------------------------- #
DEFAULT_LOOKAHEAD_DAYS = 7
# Polite, browser-ish UA so scrapers don't get an instant 403.
HTTP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HTTP_TIMEOUT = 20  # seconds, per request

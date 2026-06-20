"""
categorize.py — LLM categorization. ISOLATED ON PURPOSE.

This is the ONLY file that knows about Gemini. To swap to Groq/Llama 3.3 later,
reimplement `_call_llm(prompt) -> str` and leave everything else untouched. No
Gemini-specific types or imports may leak past this module.

Contract:
    categorize(events) -> same list, each event gains:
        "primary"   : str  (a category key, or "uncategorized")
        "secondary" : str | None
        "confidence": float (0..1)

Reliability rule (CLAUDE.md): never crash on a bad reply. Strip markdown fences,
parse defensively, and fall back to "uncategorized" for anything we can't map.
"""

import json
import logging
import re

import config

log = logging.getLogger("categorize")


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #
def build_prompt(events, categories):
    """Build the batched categorization prompt.

    `events`     : list of normalized event dicts.
    `categories` : {key: {"label","description",...}} of ENABLED categories.
    """
    cat_lines = "\n".join(
        f"- {key}: {meta['description']}" for key, meta in categories.items()
    )

    event_lines = []
    for i, ev in enumerate(events):
        loc = f" | location: {ev['location']}" if ev["location"] else ""
        desc = f" | {ev['description']}" if ev["description"] else ""
        event_lines.append(f"{i}. {ev['title']}{loc}{desc}")
    event_block = "\n".join(event_lines)

    valid_keys = ", ".join(categories.keys())

    return f"""You are an event categorizer for a Seattle weekly events digest.

Assign each event ONE primary category and an OPTIONAL secondary category, \
chosen ONLY from this fixed list:
{cat_lines}

Rules:
- "primary" MUST be exactly one of these keys: {valid_keys}.
- "secondary" is a DIFFERENT key from the same list, or null if none fits.
- If an event clearly fits none, set "primary" to "uncategorized".
- "confidence" is your certainty in the primary label, a number from 0.0 to 1.0.
- Judge by the event's actual content, not just keywords in the title.

Here are the events, one per line, prefixed by their index:
{event_block}

Return ONLY a JSON array, one object per event, in the SAME ORDER, shaped:
[{{"index": 0, "primary": "<key>", "secondary": "<key or null>", "confidence": 0.0}}]

Output JSON ONLY. No prose, no explanation, no markdown code fences."""


# --------------------------------------------------------------------------- #
# LLM call  (Gemini — the ONLY provider-specific code)
# --------------------------------------------------------------------------- #
def _call_llm(prompt):
    """Send the prompt to Gemini and return raw text. Swap THIS to change LLMs."""
    from google import genai            # google-genai SDK (current; replaces the
    from google.genai import types      # deprecated google-generativeai package)

    client = genai.Client(api_key=config.GEMINI_API_KEY)
    resp = client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.0,             # deterministic classification
            response_mime_type="application/json",  # nudge JSON-only output
        ),
    )
    return resp.text or ""


# --------------------------------------------------------------------------- #
# Safe parsing
# --------------------------------------------------------------------------- #
def _strip_fences(text):
    """Remove ```json ... ``` (or bare ```) fences an LLM might wrap output in."""
    text = text.strip()
    if text.startswith("```"):
        # drop the opening fence line (``` or ```json) and any closing fence
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_response(text):
    """Parse the LLM reply into {index: {primary, secondary, confidence}}.

    Tolerant: strips fences, and if strict JSON fails, grabs the outermost
    [...] array. Returns {} on total failure so caller falls back cleanly.
    """
    cleaned = _strip_fences(text)
    data = None
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Last resort: extract the first bracketed array and retry.
        match = re.search(r"\[.*\]", cleaned, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                data = None

    if not isinstance(data, list):
        log.warning("categorize: could not parse LLM JSON; got %r", text[:200])
        return {}

    by_index = {}
    for item in data:
        if not isinstance(item, dict) or "index" not in item:
            continue
        try:
            idx = int(item["index"])
        except (ValueError, TypeError):
            continue
        by_index[idx] = item
    return by_index


# --------------------------------------------------------------------------- #
# Keyword fallback  (used only when the LLM is unavailable)
# --------------------------------------------------------------------------- #
def _assign(ev, primary="uncategorized", secondary=None, confidence=0.0):
    ev["primary"] = primary
    ev["secondary"] = secondary
    ev["confidence"] = confidence


# Pre-compile one word-boundary regex per category. Word boundaries matter:
# substring matching would tag "Paint & Sip" as tech because "ai" is in "paint".
_KEYWORD_RES = {
    key: re.compile(
        r"\b(?:" + "|".join(re.escape(kw.strip()) for kw in kws) + r")\b")
    for key, kws in config.CATEGORY_KEYWORDS.items() if kws
}


def _keyword_categorize(events, categories):
    """Crude word-boundary categorizer for when Gemini can't be reached.

    Tries categories in CATEGORY_ORDER and takes the first whose keywords match.
    Keeps the digest useful during a quota/outage instead of dropping to empty.
    """
    log.info("categorize: using keyword fallback for %d events", len(events))
    for ev in events:
        text = f"{ev['title']} {ev['description']}".lower()
        match = None
        for key in categories:  # already in CATEGORY_ORDER (tech first, etc.)
            pat = _KEYWORD_RES.get(key)
            if pat and pat.search(text):
                match = key
                break
        _assign(ev, match or "uncategorized", None, 0.3 if match else 0.0)
    return events


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def categorize(events):
    """Categorize all events in ONE batched call. Mutates + returns the list.

    Falls back to keyword matching (never empty) if the key is missing or the
    LLM call fails — so a free-tier quota hit doesn't wipe out the digest.
    """
    if not events:
        return events

    categories = config.enabled_categories()
    valid_keys = set(categories.keys())

    if not config.GEMINI_API_KEY:
        log.warning("categorize: GEMINI_API_KEY missing — keyword fallback")
        return _keyword_categorize(events, categories)

    prompt = build_prompt(events, categories)
    try:
        raw = _call_llm(prompt)
    except Exception as exc:  # noqa: BLE001 — bad LLM call must not kill the run
        log.warning("categorize: LLM call failed (%s) — keyword fallback", exc)
        return _keyword_categorize(events, categories)

    parsed = _parse_response(raw)
    if not parsed:  # unparseable reply → don't lose everything, fall back
        return _keyword_categorize(events, categories)

    for i, ev in enumerate(events):
        item = parsed.get(i, {})
        primary = item.get("primary")
        secondary = item.get("secondary")
        try:
            confidence = float(item.get("confidence", 0.0))
        except (ValueError, TypeError):
            confidence = 0.0

        # Validate against enabled categories; anything else → uncategorized.
        if primary not in valid_keys:
            primary = "uncategorized"
        if secondary not in valid_keys or secondary == primary:
            secondary = None

        _assign(ev, primary, secondary, confidence)

    return events

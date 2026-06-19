"""
email_digest.py — group categorized events, render HTML + plaintext, send via Resend.

Rules (CLAUDE.md): empty categories are omitted (never rendered blank). Events
grouped by PRIMARY category in config.CATEGORY_ORDER; "uncategorized" is rendered
last so nothing silently disappears. Resend is the only delivery dependency.
"""

import html
import logging
from datetime import datetime

import requests

import config

log = logging.getLogger("email")


# --------------------------------------------------------------------------- #
# Grouping
# --------------------------------------------------------------------------- #
def group_by_category(events):
    """Return ordered list of (key, label, [events]) for non-empty categories.

    Order follows config.CATEGORY_ORDER; uncategorized (if any) appended last.
    Events within a category are sorted by start_datetime.
    """
    cats = config.enabled_categories()
    buckets = {key: [] for key in cats}
    buckets["uncategorized"] = []

    for ev in events:
        key = ev.get("primary", "uncategorized")
        buckets.setdefault(key, []).append(ev)

    ordered = []
    for key in list(cats.keys()) + ["uncategorized"]:
        bucket = buckets.get(key)
        if not bucket:
            continue  # omit empty categories
        bucket.sort(key=lambda e: e["start_datetime"])
        label = cats[key]["label"] if key in cats else "Other / Uncategorized"
        ordered.append((key, label, bucket))
    return ordered


def _fmt_when_safe(dt):
    """Format a datetime like 'Sun Jun 21, 7:00 PM' (date-only if midnight).

    Built manually because Windows strftime lacks %-I (no-pad hour).
    """
    if dt.hour == 0 and dt.minute == 0:
        return dt.strftime("%a %b %d")
    hour = dt.hour % 12 or 12
    return f"{dt.strftime('%a %b %d')}, {hour}:{dt.strftime('%M %p')}"


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def build_html(grouped, window_label):
    """Render a clean, responsive HTML digest from grouped events."""
    blocks = []
    for _key, label, events in grouped:
        rows = []
        for ev in events:
            title = html.escape(ev["title"])
            url = html.escape(ev["url"] or "#")
            when = html.escape(_fmt_when_safe(ev["start_datetime"]))
            loc = html.escape(ev["location"]) if ev["location"] else ""
            desc = html.escape(ev["description"]) if ev["description"] else ""
            meta = " · ".join(p for p in (when, loc) if p)
            rows.append(f"""
              <tr><td style="padding:10px 0;border-bottom:1px solid #eee;">
                <a href="{url}" style="font-size:16px;font-weight:600;color:#1a73e8;text-decoration:none;">{title}</a>
                <div style="font-size:13px;color:#666;margin-top:3px;">{meta}</div>
                {f'<div style="font-size:13px;color:#444;margin-top:3px;">{desc}</div>' if desc else ''}
              </td></tr>""")
        blocks.append(f"""
          <h2 style="font-size:18px;color:#222;margin:28px 0 6px;border-bottom:2px solid #1a73e8;padding-bottom:4px;">
            {html.escape(label)} <span style="color:#999;font-weight:400;font-size:14px;">({len(events)})</span>
          </h2>
          <table width="100%" cellpadding="0" cellspacing="0" role="presentation">{''.join(rows)}</table>""")

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#f6f7f9;">
  <div style="max-width:640px;margin:0 auto;padding:24px 18px;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#222;background:#fff;">
    <h1 style="font-size:24px;margin:0 0 4px;">🌧️ Seattle This Week</h1>
    <p style="color:#666;font-size:14px;margin:0 0 8px;">{html.escape(window_label)}</p>
    {''.join(blocks)}
    <p style="color:#999;font-size:12px;margin-top:32px;border-top:1px solid #eee;padding-top:12px;">
      Auto-generated weekly digest · sources: Luma, EverOut, Visit Seattle.
    </p>
  </div>
</body></html>"""


def build_text(grouped, window_label):
    """Plain-text fallback."""
    lines = [f"SEATTLE THIS WEEK — {window_label}", ""]
    for _key, label, events in grouped:
        lines.append(f"== {label} ({len(events)}) ==")
        for ev in events:
            when = _fmt_when_safe(ev["start_datetime"])
            meta = " · ".join(p for p in (when, ev["location"]) if p)
            lines.append(f"- {ev['title']}")
            lines.append(f"  {meta}")
            if ev["description"]:
                lines.append(f"  {ev['description']}")
            lines.append(f"  {ev['url']}")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Send
# --------------------------------------------------------------------------- #
def send_email(subject, html_body, text_body):
    """Send via Resend REST API. Returns True on success, False otherwise."""
    if not config.RESEND_API_KEY:
        log.error("RESEND_API_KEY missing — cannot send email")
        return False
    if not config.EMAIL_TO:
        log.error("EMAIL_TO missing — cannot send email")
        return False

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {config.RESEND_API_KEY}",
                     "Content-Type": "application/json"},
            json={
                "from": config.EMAIL_FROM,
                "to": [config.EMAIL_TO],
                "subject": subject,
                "html": html_body,
                "text": text_body,
            },
            timeout=config.HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        log.info("email sent to %s (id=%s)", config.EMAIL_TO,
                 resp.json().get("id", "?"))
        return True
    except Exception as exc:  # noqa: BLE001
        body = getattr(getattr(exc, "response", None), "text", "")
        log.error("Resend send failed: %s %s", exc, body)
        return False


# --------------------------------------------------------------------------- #
# Public entry
# --------------------------------------------------------------------------- #
def build_and_maybe_send(events, days, dry_run):
    """Build the digest. Print it if dry_run, else send via Resend.

    Returns the (html, text) pair so callers/tests can inspect it.
    """
    grouped = group_by_category(events)
    today = datetime.now().strftime("%b %d")
    window_label = f"Next {days} days · generated {today} · {len(events)} events"
    subject = f"{config.EMAIL_SUBJECT_PREFIX} ({len(events)} events)"

    html_body = build_html(grouped, window_label)
    text_body = build_text(grouped, window_label)

    if dry_run:
        print("\n" + "=" * 70)
        print(f"DRY RUN — subject: {subject}")
        print("=" * 70)
        print(text_body)
        print("=" * 70)
        print(f"(HTML body: {len(html_body)} chars — not sent)\n")
    else:
        send_email(subject, html_body, text_body)

    return html_body, text_body

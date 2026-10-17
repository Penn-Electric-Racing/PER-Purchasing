"""The Slack message of a price alert: its text and its Use / Keep buttons.

Slack sends the button clicks to PERBot, not this service, because the DM is posted with
PERBot's bot token; PERBot re-reads the row and writes Unit cost (and the product link when
the option is a different listing). The block ids, action ids and button values are a
contract with PERBot's src/priceCheck/blocks.ts: change both together. PERBot also acks
the View-listing link buttons (action ids price_swap_view:<i>), which Slack reports too.
"""
import json
import re

from scrapers.regex_scraper import _same_url


def money(n):
    """Like PERBot's partFinder money(): 2 decimals, up to 4 for sub-dollar prices (0.0425)."""
    if n >= 1:
        return f"{n:,.2f}"
    s = f"{n:.4f}".rstrip("0")
    return s + "0" * max(0, 2 - len(s.split(".")[1]))


def _flags(r):
    return [n for n in r.get("notes", []) if n != "shipping cost estimated"]


def price_break(r):
    """" (25+ price)" when a quantity price break set this listing's unit price, else ""."""
    bought = (r.get("packs_needed") or 0) * (r.get("pack_size") or 1)
    tier = max((b["min_qty"] for b in r.get("price_breaks") or [] if b["min_qty"] <= bought),
               default=None)
    return f" ({tier}+ price)" if tier and tier > 1 else ""


def format_alert(f):
    """Slack mrkdwn for one alert, in PERBot's style (emoji lead, <url|label> links)."""
    row, qty = f.row, f.row.units
    up = f.delta_pct > 0
    where = "at your link" if f.at_link else "best option found"
    supplier = f.found.get("supplier") or "listing"
    lines = [
        f"{':chart_with_upwards_trend:' if up else ':chart_with_downwards_trend:'} "
        f"*Price {'went up' if up else 'dropped'} on <{row.url}|{row.name or row.part_number}>*"
        f" (`{row.part_number}` ×{qty})",
        f"• In Notion: ${money(row.unit_price)}/unit (${money(row.unit_price * qty)})"
        + (f", ${money(row.unit_cost)}/pack of {row.pack_size}" if row.pack_size > 1 else ""),
        f"• Now ({where}): ${money(f.found_unit)}/unit{price_break(f.found)} "
        f"(${money(f.found_unit * qty)}) at "
        f"<{f.found['url']}|{supplier}> - *{f.delta_pct:+.0f}%*",
    ]
    if f.cheaper:
        c = f.cheaper
        lines.append(f"• Cheaper option: ${money(c['unit_cost'])}/unit at "
                     f"<{c['url']}|{c.get('supplier') or 'listing'}>"
                     + (f" ({_flags(c)[0]})" if _flags(c) else ""))
    if _flags(f.found):
        lines.append(f"• Flags: {'; '.join(_flags(f.found))}")
    lines.append("_Tap a button to update `Unit cost` in Notion, or edit it there. Prices "
                 "are read automatically from the listing, so check before ordering._")
    return "\n".join(lines)


# Button contract with PERBot (src/priceCheck/blocks.ts), which handles the clicks.
ACTIONS_BLOCK_ID = "price_swap_actions"
LINKS_BLOCK_ID = "price_swap_links"     # View-listing buttons; stay after a click
BUTTON_VALUE_MAX = 2000   # Slack's cap on a button value
BUTTON_TEXT_MAX = 75      # Slack's cap on a button label
_HTTP_URL_RE = re.compile(r"^https?://\S+$", re.I)


def _notion_price(unit_price, row):
    """A per-unit price in the row's own units (per pack on pack rows), rounded as shown."""
    n = unit_price * row.pack_size
    return round(n, 2 if n >= 1 else 4)


def _button_value(v):
    s = json.dumps(v, separators=(",", ":"), ensure_ascii=False)
    return s if len(s) <= BUTTON_VALUE_MAX else None


def _use_button(i, f, listing):
    """A "Use $X" button writing `listing`'s price to the row, or None when its value can't
    be carried (no price, a non-http link, or over Slack's size cap)."""
    row = f.row
    n = _notion_price(listing["unit_cost"], row)
    if not n > 0:
        return None
    supplier = (listing.get("supplier") or "listing")[:80]
    v = {"v": 1, "r": row.id, "q": row.quantity, "o": row.unit_cost, "n": n, "s": supplier}
    if not (row.link and _same_url(listing["url"], row.link)):
        if not _HTTP_URL_RE.match(listing.get("url") or ""):
            return None
        if row.link and not _HTTP_URL_RE.match(row.link):
            return None   # PERBot couldn't restore it on Undo
        v.update(l=listing["url"], ol=row.link)
    value = _button_value(v)
    if value is None:
        return None
    label = f"Use ${money(listing['unit_cost'])}/unit · {supplier}"
    switch = " and switch the product link to it" if "l" in v else ""
    check = (f"Set Unit cost to ${money(n)} ({supplier}){switch}? Open the listing first and "
             "check it's the right part, in stock, at this price.")
    return {"type": "button", "action_id": f"price_swap_use:{i}", "value": value,
            "text": {"type": "plain_text", "text": label[:BUTTON_TEXT_MAX]},
            # The human check: Slack asks before PERBot writes anything.
            "confirm": {"title": {"type": "plain_text", "text": "Update Notion?"},
                        "text": {"type": "plain_text", "text": check[:300]},
                        "confirm": {"type": "plain_text", "text": "Update Unit cost"},
                        "deny": {"type": "plain_text", "text": "Not yet"}}}


def alert_links(f):
    """Link buttons that open each option's listing (flagged ones too: looking is harmless),
    so whoever clicks Use has checked the page. None when no listing has an http link."""
    options = [f.found] + ([f.cheaper] if f.cheaper else [])
    links = []
    for i, o in enumerate(options):
        url = o.get("url") or ""
        if not _HTTP_URL_RE.match(url) or len(url) > 3000:
            continue
        mine = bool(f.row.link and _same_url(url, f.row.link))
        label = f"View {o.get('supplier') or 'listing'}{' (your link)' if mine else ''} ↗"
        links.append({"type": "button", "action_id": f"price_swap_view:{i}", "url": url,
                      "text": {"type": "plain_text", "text": label[:BUTTON_TEXT_MAX]}})
    return {"type": "actions", "block_id": LINKS_BLOCK_ID, "elements": links} if links else None


def alert_buttons(f):
    """The alert's actions block: Use the found price, Use an unflagged cheaper option, and
    Keep the row's price. None when not even the found price can be a button."""
    options = [f.found] + ([f.cheaper] if f.cheaper and not _flags(f.cheaper) else [])
    uses = [b for b in (_use_button(i, f, o) for i, o in enumerate(options)) if b]
    if not uses:
        return None
    uses[-1]["style"] = "primary"   # the cheapest option offered
    row = f.row
    keep = _button_value({"v": 1, "r": row.id, "q": row.quantity, "o": row.unit_cost})
    keep_label = f"Keep ${money(row.unit_price)}/unit"
    return {"type": "actions", "block_id": ACTIONS_BLOCK_ID, "elements": uses + [
        {"type": "button", "action_id": "price_swap_keep", "value": keep,
         "text": {"type": "plain_text", "text": keep_label[:BUTTON_TEXT_MAX]}}]}


def alert_blocks(f, text=None):
    """Block Kit for the DM: the alert text, View-listing links, then Use / Keep."""
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text or format_alert(f)}}]
    return blocks + [b for b in (alert_links(f), alert_buttons(f)) if b]

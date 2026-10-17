"""The Slack text of a price alert."""


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
    lines.append("_Update `Unit cost` in Notion if this is right. Prices are read "
                 "automatically from the listing, so check before ordering._")
    return "\n".join(lines)

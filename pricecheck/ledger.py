"""The savings ledger: one row per alert in 💸 Price Check Savings (Notion).

The alert is logged as Pending when it's DMed. PERBot fills in the rest: Use / Keep / Undo
clicks set Outcome and Saved $ = (Old − Used unit cost) × Quant., and a sweep marks a Used
row Realized once the Purchasing row is ordered at that price. /pricecheck in Slack sums it.
Prices are in the row's own units (per pack on pack rows), so the totals are plain dollars.
PERBot finds the row again by Row ID + Old unit cost + Quant.: keep those as written here.
"""
import json
import sys
from datetime import datetime, timezone
from typing import Any

from .alert import _HTTP_URL_RE, _notion_price, alert_buttons
from .comparison import Finding
from .config import Config
from .notion import Notion, Properties


def _text(s: str) -> dict[str, Any]:
    return {"rich_text": [{"type": "text", "text": {"content": s[:2000]}}] if s else []}


def offered(f: Finding) -> tuple[float, str, str]:
    """(unit cost in the row's units, supplier, listing url) of the option the alert puts
    first: its primary Use button, or the found price when it has no buttons."""
    buttons = alert_buttons(f)
    primary = next((b for b in (buttons or {}).get("elements", []) if b.get("style") == "primary"),
                   None)
    if primary:
        v = json.loads(primary["value"])
        listing = v.get("l") or f.row.link
        return v["n"], v.get("s", ""), listing
    return (_notion_price(f.found_unit, f.row), f.found.get("supplier") or "",
            f.found.get("url") or "")


def ledger_props(f: Finding, now: datetime | None = None) -> Properties:
    """Notion properties for the alert's ledger row."""
    row = f.row
    n, supplier, listing = offered(f)
    props = {
        "Name": {"title": [{"type": "text", "text": {
            "content": f"{row.name or row.part_number} ×{row.quantity}"[:2000]}}]},
        "Outcome": {"select": {"name": "Pending"}},
        "Direction": {"select": {"name": "Drop" if n < row.unit_cost else "Rise"}},
        "Old unit cost": {"number": row.unit_cost},
        "Offered unit cost": {"number": n},
        "Quant.": {"number": row.quantity},
        "Potential $": {"number": round((row.unit_cost - n) * row.quantity, 2)},
        "Supplier": _text(supplier),
        "Part": _text(row.part_number),
        "Row ID": _text(row.id),
        "Alert sent": {"date": {"start": (now or datetime.now(timezone.utc)).isoformat()}},
    }
    if row.url:
        props["Purchasing row"] = {"url": row.url}
    if listing and _HTTP_URL_RE.match(listing):
        props["Listing"] = {"url": listing}
    if row.season:
        props["Season"] = {"select": {"name": row.season}}
    return props


def log_alert(notion: Notion, cfg: Config, f: Finding, now: datetime | None = None) -> bool:
    """Write the alert to the ledger. Never raises: a missed ledger row only costs a
    statistic (PERBot creates it on the first click anyway)."""
    if not cfg.ledger_ds:
        return False
    try:
        notion.create(cfg.ledger_ds, ledger_props(f, now))
        return True
    except (RuntimeError, ValueError, KeyError) as e:
        print(f"  savings ledger: couldn't log the alert ({e})", file=sys.stderr)
        return False

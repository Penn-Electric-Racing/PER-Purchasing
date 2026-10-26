"""Compare a row's Unit cost with the prices found for it."""
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from scrapers.distributors import SITE_RES
from scrapers.ranking import Scorer
from scrapers.regex_scraper import _same_url

from .notion import Row


@dataclass
class Finding:
    row: Row
    found: dict            # the listing whose price is compared with the row
    at_link: bool          # found is the row's own product link
    delta_pct: float       # (found - sheet) / sheet x 100
    cheaper: dict = None   # a better-priced option than `found`, if one was seen
    link_unavailable: str = None   # why the row's own link wasn't used ("out of stock")

    @property
    def found_unit(self):
        return self.found["unit_cost"]


def _comparable(r):
    """A priced USD listing of the right part with enough stock."""
    return (r.get("unit_cost") and r.get("score") is not None
            and (r.get("currency") or "USD") == "USD"
            and r.get("enough_stock") is not False
            and not any(n == "part number not in listing" or n.startswith("priced in")
                        for n in r.get("notes", [])))


def _stock_problem(r):
    """"out of stock" / "stock below N" when the listing can't fill the order, else None."""
    if r.get("enough_stock") is not False:
        return None
    return next((n for n in r.get("notes", []) if n == "out of stock" or n.startswith("stock below")),
                "out of stock" if r.get("in_stock") is False else "not enough stock")


def _site(url):
    host = urlparse(url or "").netloc.lower()
    return next((name for name, rx in SITE_RES.items() if rx.search(host)), None)


def _mpn_key(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _api_twin(link, results):
    """The DigiKey / Mouser API's listing of the part on the row's link, when the link is
    that distributor's page. Its prices are exact, per packaging, at Quant.; read from the
    page, the LLM can pick another packaging's table (Tape & Reel at 1,500+) or a number
    that isn't a price at all."""
    site, mpn = _site(link["url"]), _mpn_key(link.get("mpn"))
    if not site:
        return None
    return next((r for r in results if r.get("cost_source") == "api" and r.get("unit_cost")
                 and _site(r["url"]) == site
                 and ((mpn and _mpn_key(r.get("mpn")) == mpn) or _same_url(r["url"], link["url"]))),
                None)


def _as_ordered(r, units):
    """`r` with unit_cost per unit actually needed: a minimum order or pack that makes you
    buy more than `units` raises it (2 needed, min order 10 at $1 = $5/unit)."""
    if not (r.get("unit_cost") and r.get("subtotal")) or r["subtotal"] <= r["unit_cost"] * units + 0.005:
        return r
    bought = r["packs_needed"] * r["pack_size"]
    return {**r, "unit_cost": round(r["subtotal"] / units, 4),
            "notes": r.get("notes", []) + [f"buys {bought:,} to get {units:,}"]}


def compare(row, results, cfg):
    """A Finding when the found price moved past the thresholds, else None.

    A price at a listing that's out of stock (or short of Quant.) can't be ordered, so it
    never alerts: the row's own link is passed over for the best in-stock option then, and
    with no such option there's no alert. A DigiKey / Mouser link is priced from that
    distributor's API when it returned the part, not from reading the page."""
    page = next((r for r in results if row.link and _same_url(r["url"], row.link)), None)
    twin = page and _api_twin(page, results)
    link = {**twin, "url": page["url"]} if twin else page   # keep the row's own link
    if not (link and link.get("unit_cost") and (link.get("currency") or "USD") == "USD"):
        link = None
    link_unavailable = link and _stock_problem(link)
    if link_unavailable:
        link = None
    link = link and _as_ordered(link, row.units)
    ranked = [_as_ordered(r, row.units) for r in Scorer.pick_top(results, len(results))
              if _comparable(r) and r is not twin]
    best = ranked[0] if ranked else None
    found = link or best
    if not found:
        return None
    delta = found["unit_cost"] - row.unit_price
    pct = delta / row.unit_price * 100
    if abs(pct) < cfg.threshold_pct or abs(delta) * row.units < cfg.min_delta_usd:
        return None
    # Not another reading of the found page (the LLM's, when the API priced the link).
    cheaper = min((r for r in ranked if not _same_url(r["url"], found["url"])),
                  key=lambda r: r["unit_cost"], default=None)
    if cheaper and cheaper["unit_cost"] >= min(found["unit_cost"], row.unit_price):
        cheaper = None
    return Finding(row, found, link is not None, round(pct, 1), cheaper, link_unavailable or None)

"""Compare a row's Unit cost with the prices found for it."""
from dataclasses import dataclass

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


def compare(row, results, cfg):
    """A Finding when the found price moved past the thresholds, else None.

    A price at a listing that's out of stock (or short of Quant.) can't be ordered, so it
    never alerts: the row's own link is passed over for the best in-stock option then, and
    with no such option there's no alert."""
    link = next((r for r in results if row.link and _same_url(r["url"], row.link)
                 and r.get("unit_cost") and (r.get("currency") or "USD") == "USD"), None)
    link_unavailable = link and _stock_problem(link)
    if link_unavailable:
        link = None
    ranked = [r for r in Scorer.pick_top(results, len(results)) if _comparable(r)]
    best = ranked[0] if ranked else None
    found = link or best
    if not found:
        return None
    delta = found["unit_cost"] - row.unit_price
    pct = delta / row.unit_price * 100
    if abs(pct) < cfg.threshold_pct or abs(delta) * row.units < cfg.min_delta_usd:
        return None
    cheaper = min((r for r in ranked if r is not found), key=lambda r: r["unit_cost"], default=None)
    if cheaper and cheaper["unit_cost"] >= min(found["unit_cost"], row.unit_price):
        cheaper = None
    return Finding(row, found, link is not None, round(pct, 1), cheaper, link_unavailable or None)

"""Compare a row's Unit cost with the prices found for it."""
from dataclasses import dataclass

from scrapers.listing import Listing
from scrapers.ranking import Scorer
from scrapers.regex_scraper import _same_url

from .config import Config
from .notion import Row


@dataclass
class Finding:
    row: Row
    found: Listing         # the listing whose price is compared with the row
    at_link: bool          # found is the row's own product link
    delta_pct: float       # (found - sheet) / sheet x 100
    cheaper: Listing | None = None  # a better-priced option than `found`, if one was seen

    @property
    def found_unit(self) -> float:
        return self.found["unit_cost"]


def _comparable(r: Listing) -> bool:
    """A priced USD listing of the right part with enough stock."""
    return bool(r.get("unit_cost") and r.get("score") is not None
            and (r.get("currency") or "USD") == "USD"
            and r.get("enough_stock") is not False
            and not any(n == "part number not in listing" or n.startswith("priced in")
                        for n in r.get("notes", [])))


def compare(row: Row, results: list[Listing], cfg: Config) -> Finding | None:
    """A Finding when the found price moved past the thresholds, else None."""
    link = next((r for r in results if row.link and _same_url(r["url"], row.link)
                 and r.get("unit_cost") and (r.get("currency") or "USD") == "USD"), None)
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
    return Finding(row, found, link is not None, round(pct, 1), cheaper)

"""
ranking.py - score and pick the best purchase options (used by both scrapers).

Score = (effective order cost / cheapest effective cost) x penalty multipliers.
1.00 is the cheapest candidate with nothing counting against it; lower is better.

Tune it by editing the defaults in Weights, or per run without touching code:
    scorer = Scorer(Weights(out_of_stock=2.0, per_ship_day=0.05))
    python -m scrapers.regex_scraper "LM7805" --weights my_weights.json   # {"out_of_stock": 2.0, ...}
"""
import json
import re
from collections import Counter
from dataclasses import dataclass, field, fields
from datetime import date, datetime
from statistics import median
from urllib.parse import unquote

from .listing import Listing


@dataclass
class Weights:
    """Every tunable number used by Scorer. Factors are multipliers (1.0 = no effect);
    see the comments on each field and README.md for the full formula.
    """
    out_of_stock: float = 1.6        # listed as out of stock / backordered
    short_stock: float = 1.4         # stock shown but below the quantity needed
    stock_unknown: float = 1.03
    per_ship_day: float = 0.02       # +2% per day of shipping beyond fast_days...
    ship_day_cap: float = 0.40       # ...capped at +40%
    fast_days: int = 2               # shipping this fast or faster isn't penalised
    slow_note_days: int = 10         # add a "slow" note at this many days
    ship_time_unknown: float = 1.06
    ship_cost_unknown: float = 1.04  # on top of adding the median known shipping cost
    # Confidence in where the price came from (regex_scraper.py: json-ld/meta/page text/snippet,
    # llm_scraper.py: llm/llm snippet, both: api = Mouser/DigiKey APIs).
    # Unlisted sources get unknown_source.
    source: dict[str, float] = field(default_factory=lambda: {
        "json-ld": 1.0, "meta": 1.0, "page text": 1.05, "snippet": 1.15,
        "llm": 1.02, "llm snippet": 1.15, "api": 1.0})
    unknown_source: float = 1.1
    listing_page: float = 1.12       # price is the lowest of many listings, may not be this part
    relevance: float = 0.6           # x(1 + 0.6 * fraction of query words missing)
    mpn_variant: float = 1.08        # part number only inside a longer code (LM317L for LM317)
    outlier: float = 1.35            # unit cost < outlier_ratio x the median...
    outlier_ratio: float = 0.25      # ...likely the wrong item or an accessory
    outlier_min_count: int = 4       # need this many prices before judging outliers
    per_search_rank: float = 0.005   # tiny tiebreak favouring higher search positions

    @classmethod
    def load(cls, path: str) -> "Weights":
        """Defaults overridden by a JSON file of {"weight name": value}."""
        with open(path) as f:
            data = json.load(f)
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown weights in {path}: {', '.join(sorted(unknown))}")
        w = cls()
        if "source" in data:  # merge, so a file can change one source without listing all
            data["source"] = {**w.source, **data["source"]}
        for k, v in data.items():
            setattr(w, k, v)
        return w


# ---------------------------------------------------------------- helpers
def _alnum(s: str) -> str:
    """Lowercase letters and digits only, for loose matching ("LM-317" -> "lm317")."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def relevance(query: str, r: Listing) -> tuple[float, bool, list[str]]:
    """(fraction of query words found, part-number match, variant codes seen instead).
    Part numbers = words with digits. If a part number only shows up inside a longer
    code (LM317L for LM317), that's a variant: maybe the same part in another package,
    maybe a different part, so it's listed for the user to check."""
    words = [w for w in re.findall(r"[a-z0-9][a-z0-9.\-/x]*", query.lower())
             if w not in {"price", "buy", "in", "stock", "for", "the", "a"}]
    raw = f"{r['title']} {r.get('page_title') or ''} {r['snippet']} {unquote(r['url'])}"
    hay = _alnum(raw)
    found = [w for w in words if _alnum(w) and _alnum(w) in hay]
    keys = [w for w in words if re.search(r"\d", w)]
    key_hit = not keys or any(_alnum(k) in hay for k in keys)
    tokens = {_alnum(t): t for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9.\-]*", raw)}
    variants = []
    for k in map(_alnum, keys):
        if k not in tokens:  # no exact token: collect the longer codes it appears in
            variants += [t for a, t in tokens.items() if k in a and t not in variants]
    return (len(found) / len(words) if words else 1.0), key_hit, variants


def ship_days(text: str | None, today: date | None = None) -> int | None:
    """Rough worst-case days until delivery from a shipping_time string."""
    if not text:
        return None
    t, today = text.lower(), today or date.today()
    if "today" in t or "same" in t:
        return 1
    if "next business day" in t:
        return 2
    m = re.search(r"(\d+)(?:\s?(?:-|–|to)\s?(\d+))?\s?(?:business\s|working\s)?(hour|day|week)", t)
    if m:
        n = int(m.group(2) or m.group(1))
        return {"hour": max(1, -(-n // 24)), "day": n, "week": 7 * n}[m.group(3)]
    m = re.search(r"([a-z]{3})[a-z]*\.?\s(\d{1,2})$", t)
    if m:
        try:
            d = datetime.strptime(f"{m.group(1)} {m.group(2)} {today.year}", "%b %d %Y").date()
        except ValueError:
            return None
        if d < today:
            d = d.replace(year=today.year + 1)
        return max((d - today).days, 1)
    return None


def ship_value(ship: str | None) -> float | None:
    """Shipping cost string ("Free", "$5.99", "4.50 USD") as a number, else None."""
    if ship == "Free":
        return 0.0
    try:
        return float(re.sub(r"[^\d.]", "", ship or ""))
    except ValueError:
        return None


# ---------------------------------------------------------------- scorer
class Scorer:
    """Scores candidates and picks the best ones, using a set of Weights.

        scorer = Scorer(Weights(out_of_stock=2.0))
        scorer.score_all(results, query, qty)
        top = scorer.pick_top(results, 5)
    """
    def __init__(self, weights: Weights | None = None) -> None:
        """Use `weights`, or the defaults from Weights()."""
        self.w = weights or Weights()

    def multiplier(self, r: Listing, qty: int, med_unit: float | None = None,
                   n_units: int = 0) -> float:
        """Penalty multiplier for one priced candidate; appends to r["notes"]."""
        w, mult, notes = self.w, 1.0, r["notes"]
        if r["enough_stock"] is False and r["in_stock"] is False:
            mult *= w.out_of_stock; notes.append("out of stock")
        elif r["enough_stock"] is False:
            mult *= w.short_stock; notes.append(f"stock below {qty}")
        elif r["enough_stock"] is None and r["in_stock"] is None:
            mult *= w.stock_unknown
        days = ship_days(r["shipping_time"])
        r["ship_days"] = days
        if days is None:
            mult *= w.ship_time_unknown
        elif days > w.fast_days:
            mult *= 1 + min(w.per_ship_day * (days - w.fast_days), w.ship_day_cap)
            if days >= w.slow_note_days:
                notes.append(f"slow: ~{days} days")
        if ship_value(r["shipping_cost"]) is None:
            mult *= w.ship_cost_unknown; notes.append("shipping cost estimated")
        src = (r["cost_source"] or "snippet").split(" (")[0]
        mult *= w.source.get(src, w.unknown_source)
        if r["listing_page"]:
            mult *= w.listing_page; notes.append("search page: lowest listing")
        mult *= 1 + w.relevance * (1 - r["relevance"])
        if r["mpn_variants"]:
            mult *= w.mpn_variant
        if med_unit and n_units >= w.outlier_min_count and \
                r["unit_cost"] < w.outlier_ratio * med_unit:
            mult *= w.outlier; notes.append("far cheaper than others - verify item")
        mult *= 1 + w.per_search_rank * r["search_rank"]
        return mult

    def score_all(self, results: list[Listing], query: str, qty: int) -> list[Listing]:
        """Attach score + human-readable notes to every priced, relevant candidate."""
        priced = [r for r in results if r["subtotal"] is not None]
        known_ship = [v for v in (ship_value(r["shipping_cost"]) for r in priced) if v is not None]
        ship_fill = median(known_ship) if known_ship else 0.0
        units = [r["unit_cost"] for r in priced if r["unit_cost"]]
        med_unit = median(units) if units else None
        main_cur = Counter(r["currency"] for r in priced).most_common(1)[0][0] if priced else None

        for r in results:
            r["score"], r["notes"] = None, []
            r["relevance"], key_hit, r["mpn_variants"] = relevance(query, r)
            if r["subtotal"] is None:
                r["notes"].append("no price found")
                continue
            if not key_hit:
                r["notes"].append("part number not in listing")
            elif r["mpn_variants"]:
                r["notes"].append(f"listed as {', '.join(r['mpn_variants'][:3])} - check suffix")
            if r["currency"] and main_cur and r["currency"] != main_cur:
                r["notes"].append(f"priced in {r['currency']}, not {main_cur}")
            ship = ship_value(r["shipping_cost"])
            r["effective_cost"] = round(r["subtotal"] + (ship if ship is not None else ship_fill), 2)

        ok = [r for r in results if r["subtotal"] is not None]
        # Baseline = cheapest *valid* option, so a wrong-part bargain doesn't skew everyone.
        # If every listing has a note (e.g. all show a suffixed part number), still leave
        # out the wrong-part and other-currency ones.
        wrong = ("part number not in listing", "priced in ")
        valid = [r["effective_cost"] for r in ok if not r["notes"]] or \
            [r["effective_cost"] for r in ok
             if not any(n.startswith(wrong) for n in r["notes"])] or \
            [r["effective_cost"] for r in ok]
        best = min(valid, default=0) or 0.01
        for r in ok:
            r["score"] = round(r["effective_cost"] / best *
                               self.multiplier(r, qty, med_unit, len(units)), 3)
        return results

    @staticmethod
    def pick_top(results: list[Listing], top: int, mode: str = "best",
                 per_supplier: int = 2) -> list[Listing]:
        """Best `top` options. Hard filters first; loosened only if too few candidates pass."""
        def tier(r: Listing) -> int:
            """Sort group: 0 ok, 1 not enough stock, 2 wrong part/currency, 3 unpriced."""
            if r["score"] is None:
                return 3  # unpriced
            if "part number not in listing" in r["notes"] or any(
                    n.startswith("priced in") for n in r["notes"]):
                return 2
            if r["enough_stock"] is False:
                return 1
            return 0

        key = (lambda r: r["effective_cost"]) if mode == "cheapest" else (lambda r: r["score"])
        ordered = sorted(results, key=lambda r: (tier(r), key(r) if tier(r) < 3 else -r["relevance"],
                                                 r["search_rank"]))
        picked: list[Listing] = []
        per: Counter[str] = Counter()
        for r in ordered:  # first pass: cap listings per supplier for variety
            if per[r["supplier"]] < per_supplier:
                picked.append(r); per[r["supplier"]] += 1
            if len(picked) == top:
                break
        for r in ordered:  # fill up if the cap left gaps
            if len(picked) == top:
                break
            if r not in picked:
                picked.append(r)
        picked.sort(key=lambda r: ordered.index(r))
        return picked

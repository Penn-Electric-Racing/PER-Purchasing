"""
distributors.py - exact price/stock data from distributor APIs (used by both scrapers).

Mouser and DigiKey block or confuse page scraping, but both have free APIs that
return structured part data: price breaks, stock, minimum order and lead time.
Each API that has keys set adds up to --api results (default 10) to the pool,
and replaces any scraped pages from that distributor's site.

  Mouser Search API          env: MOUSER_API_KEY
      https://www.mouser.com/api-hub/  (free; ~30 calls/min, 1,000/day)
  DigiKey Product Info v4    env: DIGIKEY_CLIENT_ID, DIGIKEY_CLIENT_SECRET
      https://developer.digikey.com  (create an app with "Product Information V4";
      free tier ~1,000 calls/day). Optional: DIGIKEY_SITE (US), DIGIKEY_CURRENCY (USD).

Shipping policies change and aren't in either API, so shipping cost is left
unknown unless you set the free-shipping minimum for your country:
  MOUSER_FREE_SHIP_MIN=50  DIGIKEY_FREE_SHIP_MIN=100
"""
import os
import re
import sys
import time
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from .listing import Listing, PriceBreaks

TIMEOUT = 15
MAX_RECORDS = 50  # both APIs return at most 50 parts per call

MOUSER_URL = "https://api.mouser.com/api/v1/search/keyword"
DIGIKEY_TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
DIGIKEY_SEARCH_URL = "https://api.digikey.com/products/v4/search/keyword"

SITE_RES = {"mouser": re.compile(r"(?:^|\.)mouser\.[a-z.]+$"),
            "digikey": re.compile(r"(?:^|\.)digikey\.[a-z.]+$")}


class APIError(Exception):
    """A distributor API is not configured, or a request failed."""


def _blank() -> Listing:
    # Imported here: regex_scraper imports this module, so a top-level import would be circular.
    from .regex_scraper import _blank_info
    return _blank_info()


def _env_float(name: str) -> float | None:
    """Float from an environment variable, or None if unset/invalid."""
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return None


def _price(s: object) -> float | None:
    """Mouser price strings: "$0.89", "1,234.50 USD", "0,89 €" -> float, else None."""
    s = re.sub(r"[^\d.,]", "", str(s or ""))
    if "," in s and "." not in s and re.search(r",\d{1,4}$", s) and not re.search(r",\d{3}$", s):
        s = s.replace(",", ".")  # decimal comma
    try:
        return float(s.replace(",", "")) or None
    except ValueError:
        return None


def _int(v: object) -> int | None:
    """"12,400" / 12400 / "12400 In Stock" -> 12400, else None."""
    m = re.search(r"\d[\d,]*", str(v if v is not None else ""))
    return int(m.group().replace(",", "")) if m else None


def order_units(qty: int, min_order: int | None = None, mult: int | None = None) -> int:
    """Units you actually have to buy: at least the minimum, in whole multiples."""
    units = max(qty, min_order or 1)
    mult = mult or 1
    return -(-units // mult) * mult


def _ship_time(stock: int | None, lead: str | None) -> str | None:
    """In stock: ships today. Otherwise the factory lead time ("16 weeks"), if known."""
    if stock:
        return "ships same day"
    return lead or None


def _candidate(title: str, url: str, snippet: str, supplier: str, **info: Any) -> Listing:
    """A result dict in the same shape regex_scraper.part_info produces."""
    return {"title": title, "url": url, "snippet": snippet,
            **_blank(), "supplier": supplier, "cost_source": "api", **info}


# ---------------------------------------------------------------- Mouser
def mouser(query: str, qty: int = 1, n: int = 10) -> list[Listing]:
    """Up to `n` priced parts from Mouser's keyword search (needs MOUSER_API_KEY)."""
    key = os.getenv("MOUSER_API_KEY")
    if not key:
        raise APIError("MOUSER_API_KEY not set")
    r = requests.post(MOUSER_URL, params={"apiKey": key}, timeout=TIMEOUT, json={
        "SearchByKeywordRequest": {"keyword": query, "records": min(n * 2, MAX_RECORDS),
                                   "startingRecord": 0}})
    if r.status_code != 200:
        raise APIError(f"Mouser HTTP {r.status_code}")
    data = r.json()
    if data.get("Errors"):
        raise APIError("Mouser: " + "; ".join(e.get("Message", str(e)) for e in data["Errors"]))
    out = []
    for p in (data.get("SearchResults") or {}).get("Parts") or []:
        breaks = [(int(b["Quantity"]), _price(b.get("Price")))
                  for b in p.get("PriceBreaks") or [] if _int(b.get("Quantity"))]
        breaks = sorted((q, v) for q, v in breaks if v)
        if not breaks:
            continue  # quote-only / not sold by Mouser
        stock = _int(p.get("AvailabilityInStock"))
        if stock is None and "in stock" in str(p.get("Availability", "")).lower():
            stock = _int(p.get("Availability"))
        mpn, mfr = p.get("ManufacturerPartNumber", ""), p.get("Manufacturer", "")
        out.append(_candidate(
            title=f"{mpn} {mfr} | Mouser ({p.get('MouserPartNumber', '')})",
            url=p.get("ProductDetailUrl") or "https://www.mouser.com",
            snippet=p.get("Description", ""),
            supplier="Mouser Electronics",
            cost=breaks[0][1],
            currency=(p.get("PriceBreaks") or [{}])[0].get("Currency") or "USD",
            price_breaks=breaks,
            stock=stock or 0,
            in_stock=bool(stock),
            min_order=_int(p.get("Min")),
            order_mult=_int(p.get("Mult")),
            shipping_time=_ship_time(stock, (p.get("LeadTime") or "").lower() or None),
            free_ship_min=_env_float("MOUSER_FREE_SHIP_MIN"),
        ))
        if len(out) == n:
            break
    return out


# ---------------------------------------------------------------- DigiKey
_token: dict[str, Any] = {"value": None, "expires": 0.0}


def _digikey_token(client_id: str, secret: str) -> str:
    """OAuth2 client-credentials token, cached until shortly before it expires."""
    if _token["value"] and time.time() < _token["expires"]:
        return _token["value"]
    r = requests.post(DIGIKEY_TOKEN_URL, timeout=TIMEOUT, data={
        "client_id": client_id, "client_secret": secret, "grant_type": "client_credentials"})
    if r.status_code != 200:
        raise APIError(f"DigiKey token HTTP {r.status_code}")
    d = r.json()
    _token.update(value=d["access_token"], expires=time.time() + int(d.get("expires_in", 600)) - 30)
    return _token["value"]


# (order cost, variation, package name, price breaks, minimum order, order multiple)
Variation = tuple[float, dict[str, Any], str, PriceBreaks, int, int | None]


def _best_variation(variations: list[dict[str, Any]] | None, qty: int) -> Variation | None:
    """The packaging (cut tape, reel, tube...) that's cheapest for `qty` units.
    Digi-Reel is skipped: its price excludes the reeling fee."""
    best: Variation | None = None
    for v in variations or []:
        pkg = ((v.get("PackageType") or {}).get("Name") or "")
        if "digi-reel" in pkg.lower():
            continue
        breaks = sorted((int(b["BreakQuantity"]), float(b["UnitPrice"]))
                        for b in v.get("StandardPricing") or []
                        if b.get("BreakQuantity") and b.get("UnitPrice"))
        if not breaks:
            continue
        moq = v.get("MinimumOrderQuantity") or breaks[0][0]
        mult = v.get("StandardPackage") if "reel" in pkg.lower() else 1
        units = order_units(qty, moq, mult)
        unit_price = [p for q, p in breaks if q <= units][-1]
        cost = unit_price * units
        if best is None or cost < best[0]:
            best = (cost, v, pkg, breaks, moq, mult)
    return best


def digikey(query: str, qty: int = 1, n: int = 10) -> list[Listing]:
    """Up to `n` priced parts from DigiKey's v4 keyword search
    (needs DIGIKEY_CLIENT_ID and DIGIKEY_CLIENT_SECRET)."""
    cid, secret = os.getenv("DIGIKEY_CLIENT_ID"), os.getenv("DIGIKEY_CLIENT_SECRET")
    if not (cid and secret):
        raise APIError("DIGIKEY_CLIENT_ID / DIGIKEY_CLIENT_SECRET not set")
    currency = os.getenv("DIGIKEY_CURRENCY", "USD")
    r = requests.post(DIGIKEY_SEARCH_URL, timeout=TIMEOUT, headers={
        "Authorization": f"Bearer {_digikey_token(cid, secret)}",
        "X-DIGIKEY-Client-Id": cid,
        "X-DIGIKEY-Locale-Site": os.getenv("DIGIKEY_SITE", "US"),
        "X-DIGIKEY-Locale-Language": "en",
        "X-DIGIKEY-Locale-Currency": currency,
    }, json={"Keywords": query, "Limit": min(n * 2, MAX_RECORDS), "Offset": 0})
    if r.status_code != 200:
        raise APIError(f"DigiKey HTTP {r.status_code}")
    data = r.json()
    products, seen = [], set()
    for p in (data.get("ExactMatches") or []) + (data.get("Products") or []):
        key = p.get("ManufacturerProductNumber"), (p.get("Manufacturer") or {}).get("Name")
        if key not in seen:
            seen.add(key); products.append(p)
    out = []
    for p in products:
        best = _best_variation(p.get("ProductVariations"), qty)
        if best is None:
            continue
        _, v, pkg, breaks, moq, mult = best
        stock = p.get("QuantityAvailable") or 0
        lead = p.get("ManufacturerLeadWeeks")
        desc = p.get("Description") or {}
        mpn, mfr = p.get("ManufacturerProductNumber", ""), (p.get("Manufacturer") or {}).get("Name", "")
        out.append(_candidate(
            title=f"{mpn} {mfr} | DigiKey ({v.get('DigiKeyProductNumber', '')}, {pkg})",
            url=p.get("ProductUrl") or "https://www.digikey.com",
            snippet=desc.get("DetailedDescription") or desc.get("ProductDescription") or "",
            supplier="DigiKey",
            cost=breaks[0][1],
            currency=(data.get("SearchLocaleUsed") or {}).get("Currency") or currency,
            price_breaks=breaks,
            stock=stock,
            in_stock=stock > 0,
            min_order=moq,
            order_mult=mult,
            shipping_time=_ship_time(stock, f"{lead} weeks" if _int(lead) else None),
            free_ship_min=_env_float("DIGIKEY_FREE_SHIP_MIN"),
        ))
        if len(out) == n:
            break
    return out


APIS: list[tuple[str, Callable[[str, int, int], list[Listing]]]] = [
    ("mouser", mouser), ("digikey", digikey)]


# ---------------------------------------------------------------- merging
def lookup(query: str, qty: int = 1, n: int = 10,
           verbose: bool = False) -> tuple[list[Listing], list[str]]:
    """Results from every configured API: (results, names of APIs that returned some).
    Unconfigured or failing APIs are skipped (logged with `verbose`)."""
    out, used = [], []
    for name, fn in APIS:
        try:
            res = fn(query, qty, n)
        except (APIError, requests.RequestException, ValueError, KeyError) as e:
            if verbose:
                print(f"[{name} api] skipped: {e}", file=sys.stderr)
            continue
        for i, r in enumerate(res):
            r["search_rank"] = i
        if res:
            used.append(name)
        out += res
    return out, used


def drop_api_sites(web: list[Listing], used: list[str]) -> list[Listing]:
    """Web results minus pages from a site whose API answered: the API's data for the
    same parts is exact, so those pages needn't be fetched."""
    def from_api_site(r: Listing) -> bool:
        host = urlparse(r["url"]).netloc.lower()
        return any(SITE_RES[name].search(host) for name in used)
    return [r for r in web if not from_api_site(r)]

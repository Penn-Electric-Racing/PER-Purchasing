"""
Web search and page fetching, shared by both scrapers.

Search sources (first that works wins; no google.com scraping, so no CAPTCHAs):
  1. Brave Search API        (free tier, ~2k queries/mo)   env: BRAVE_API_KEY
  2. Serper.dev Google results (2,500 free, no card)       env: SERPER_API_KEY
  3. Google Programmable Search JSON API (100/day free)    env: GOOGLE_API_KEY, GOOGLE_CSE_ID
  4. DuckDuckGo HTML endpoint (no key; paginated politely)
"""
import json
import os
import random
import sys
import time
from urllib.parse import parse_qs, unquote, urlparse

import requests
from bs4 import BeautifulSoup

TIMEOUT = 15
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")


class BackendError(Exception):
    """A search backend or page fetch failed (missing key, HTTP error, not HTML...)."""


# ---------------------------------------------------------------- backends
# Each backend pages through results until it has `n` (or runs out).
def brave(query, n):
    """Up to `n` results from the Brave Search API (needs BRAVE_API_KEY).

    Returns [{"title", "url", "snippet"}, ...]; raises BackendError if the first page fails.
    """
    key = os.getenv("BRAVE_API_KEY")
    if not key:
        raise BackendError("BRAVE_API_KEY not set")
    out = []
    for page in range(10):  # Brave: count <= 20, offset is a page index (max 9)
        r = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": 20, "offset": page},
            headers={"X-Subscription-Token": key, "Accept": "application/json"},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            if out:
                break
            raise BackendError(f"HTTP {r.status_code}")
        items = r.json().get("web", {}).get("results", [])
        out += [{"title": i.get("title", ""), "url": i.get("url", ""),
                 "snippet": BeautifulSoup(i.get("description", ""), "html.parser").get_text()}
                for i in items]
        if len(out) >= n or len(items) < 20:
            break
    return out[:n]


def serper(query, n):
    """Up to `n` Google results via Serper.dev (needs SERPER_API_KEY), 10 per request."""
    key = os.getenv("SERPER_API_KEY")
    if not key:
        raise BackendError("SERPER_API_KEY not set")
    out = []
    for page in range(1, min(-(-n // 10), 10) + 1):  # 10 per call = 1 credit each
        r = requests.post(
            "https://google.serper.dev/search",
            json={"q": query, "num": 10, "page": page},
            headers={"X-API-KEY": key, "Content-Type": "application/json"},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            if out:
                break
            raise BackendError(f"HTTP {r.status_code}")
        items = r.json().get("organic", [])
        out += [{"title": i.get("title", ""), "url": i.get("link", ""),
                 "snippet": i.get("snippet", "")} for i in items]
        if len(items) < 10:
            break
    return out[:n]


def google_cse(query, n):
    """Up to `n` (max 100) results from Google Programmable Search
    (needs GOOGLE_API_KEY and GOOGLE_CSE_ID), 10 per request.
    """
    key, cx = os.getenv("GOOGLE_API_KEY"), os.getenv("GOOGLE_CSE_ID")
    if not (key and cx):
        raise BackendError("GOOGLE_API_KEY / GOOGLE_CSE_ID not set")
    out = []
    for start in range(1, min(n, 100) + 1, 10):  # CSE: 10 per call, max 100 total
        r = requests.get(
            "https://www.googleapis.com/customsearch/v1",
            params={"key": key, "cx": cx, "q": query, "num": 10, "start": start},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            if out:
                break
            raise BackendError(f"HTTP {r.status_code}")
        items = r.json().get("items", [])
        out += [{"title": i.get("title", ""), "url": i.get("link", ""),
                 "snippet": i.get("snippet", "")} for i in items]
        if len(items) < 10:
            break
    return out[:n]


def _clean_ddg_url(href):
    # DDG sometimes wraps links: //duckduckgo.com/l/?uddg=<encoded>
    """Unwrap DuckDuckGo's //duckduckgo.com/l/?uddg=<url> redirect links."""
    if "duckduckgo.com/l/" in href:
        qs = parse_qs(urlparse(href).query)
        if "uddg" in qs:
            return unquote(qs["uddg"][0])
    return href


def _ddg_page(form, retries=2):
    """POST one DDG HTML results page; return (results, next-page form data or None)."""
    last = None
    for attempt in range(retries + 1):
        if attempt:
            # polite backoff; HTTP 202 is DDG's "slow down", so wait longer for it
            time.sleep(2 ** attempt * (3 if last == "HTTP 202" else 1) + random.random())
        try:
            r = requests.post(
                "https://html.duckduckgo.com/html/",
                data=form,
                headers={"User-Agent": UA, "Referer": "https://html.duckduckgo.com/"},
                timeout=TIMEOUT,
            )
        except requests.RequestException as e:
            last = str(e)
            continue
        if r.status_code != 200:
            last = f"HTTP {r.status_code}"
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        # Challenge / anomaly page detection
        if soup.select_one(".anomaly-modal, #challenge-form") or "captcha" in r.text.lower():
            last = "challenge page returned"
            continue
        results = []
        for res in soup.select("div.result"):
            if "result--ad" in res.get("class", []):
                continue  # skip sponsored
            a = res.select_one("a.result__a")
            if not a:
                continue
            snip = res.select_one(".result__snippet")
            results.append({
                "title": a.get_text(strip=True),
                "url": _clean_ddg_url(a.get("href", "")),
                "snippet": snip.get_text(" ", strip=True) if snip else "",
            })
        nxt = None
        for f in soup.select("form"):
            data = {i["name"]: i.get("value", "") for i in f.select("input[name]")}
            if "s" in data and "vqd" in data:  # the "Next" button's hidden form
                nxt = data
        if results:
            return results, nxt
        last = "no results parsed"
    raise BackendError(last or "unknown error")


def duckduckgo(query, n):
    """Up to `n` results from DuckDuckGo's HTML page (no key), following its
    "Next" form with a pause between pages. Keeps partial results if a later page fails.
    """
    out, form = [], {"q": query}
    while form and len(out) < n:
        if out:
            time.sleep(1 + random.random())  # be gentle between pages
        try:
            page, form = _ddg_page(form)
        except BackendError:
            if out:
                break  # keep what we have
            raise
        out += page
    return out[:n]


BACKENDS = [("brave", brave), ("serper", serper), ("google_cse", google_cse), ("duckduckgo", duckduckgo)]


MAX_PAGE_BYTES = 5_000_000


def _fetch_html(url):
    """GET a page as text; refuse non-HTML (datasheet PDFs are common part-number hits)
    and stop reading after MAX_PAGE_BYTES."""
    with requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
                      timeout=TIMEOUT, stream=True) as r:
        if r.status_code != 200:
            raise BackendError(f"HTTP {r.status_code}")
        ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype and "html" not in ctype and "xml" not in ctype:
            raise BackendError(f"not a web page ({ctype})")
        body = bytearray()
        for chunk in r.iter_content(64 * 1024):
            body += chunk
            if len(body) >= MAX_PAGE_BYTES:
                break
        try:  # same decoding as r.text
            return body.decode(r.encoding or "utf-8", errors="replace")
        except LookupError:  # unknown charset name in the headers
            return body.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- search
def search(query, n=5, verbose=False, variants=()):
    """Top `n` unique URLs from the first working backend, topping up with `variants`."""
    errors = {}
    for name, fn in BACKENDS:
        try:
            res, seen = [], set()
            for q in (query, *variants):
                if len(res) >= n:
                    break
                try:
                    batch = fn(q, n)
                except BackendError:
                    if res:
                        continue  # extra variants are best-effort
                    raise
                for r in batch:
                    u = r["url"].split("#")[0].rstrip("/")
                    if u and u not in seen:
                        seen.add(u); res.append(r)
            if res:
                for i, r in enumerate(res[:n]):
                    r["search_rank"] = i
                return name, res[:n]
            errors[name] = "empty result"
        except (BackendError, requests.RequestException, ValueError) as e:
            errors[name] = str(e)
        if verbose:
            print(f"[{name}] skipped: {errors[name]}", file=sys.stderr)
    raise RuntimeError("All backends failed: " + json.dumps(errors, indent=2))


def _search_help(msg):
    """Search failure message, plus advice when DuckDuckGo is rate-limiting."""
    if "HTTP 202" in msg or "Connection reset" in msg or "challenge" in msg:
        msg += ("\n\nDuckDuckGo is rate-limiting this network (usually clears in "
                "15-60 min).\nFor reliable 30-result searches, set a free Brave Search "
                "API key (Serper needs no card):\n"
                "  https://serper.dev  ->  export SERPER_API_KEY=...\n"
                "  https://api.search.brave.com  ->  export BRAVE_API_KEY=...")
    return msg

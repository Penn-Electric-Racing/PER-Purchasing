"""Offline tests for the search backends and page fetching (scrapers/websearch.py) and page
parsing (scrapers/regex_scraper.py).

    pip install requests beautifulsoup4 pytest
    pytest
"""
import json

import pytest
import requests

from scrapers import regex_scraper as s
from scrapers import websearch as ws


class Resp:
    """A requests.Response stand-in: JSON body or HTML text."""

    def __init__(self, status=200, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text

    def json(self):
        return self._body


def calls_to(monkeypatch, method, *responses):
    """Patch requests.<method> to return `responses` in order; returns the recorded calls."""
    calls, queue = [], list(responses)

    def fake(url, **kw):
        calls.append({"url": url, **kw})
        return queue.pop(0)
    monkeypatch.setattr(s.requests, method, fake)
    return calls


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(ws.time, "sleep", lambda t: None)


# ---------------------------------------------------------------- Brave
def brave_page(n, start=0):
    return Resp(body={"web": {"results": [
        {"title": f"t{i}", "url": f"https://x.com/{i}", "description": f"<b>LM7805</b> {i}"}
        for i in range(start, start + n)]}})


def test_brave_needs_a_key(monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    with pytest.raises(s.BackendError, match="BRAVE_API_KEY"):
        ws.brave("LM7805", 10)


def test_brave_pages_until_enough(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    calls = calls_to(monkeypatch, "get", brave_page(20), brave_page(20, 20))
    out = ws.brave("LM7805", 30)
    assert len(out) == 30 and [c["params"]["offset"] for c in calls] == [0, 1]
    assert out[0] == {"title": "t0", "url": "https://x.com/0", "snippet": "LM7805 0"}  # HTML stripped
    assert calls[0]["headers"]["X-Subscription-Token"] == "k"


def test_brave_stops_on_a_short_page(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    calls = calls_to(monkeypatch, "get", brave_page(5))
    assert len(ws.brave("LM7805", 30)) == 5 and len(calls) == 1


def test_brave_first_page_error_raises_later_error_keeps_results(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    calls_to(monkeypatch, "get", Resp(429))
    with pytest.raises(s.BackendError, match="HTTP 429"):
        ws.brave("LM7805", 10)
    calls_to(monkeypatch, "get", brave_page(20), Resp(500))
    assert len(ws.brave("LM7805", 30)) == 20


# ---------------------------------------------------------------- Serper
def serper_page(n):
    return Resp(body={"organic": [{"title": f"t{i}", "link": f"https://x.com/{i}",
                                   "snippet": "s"} for i in range(n)]})


def test_serper(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "k")
    calls = calls_to(monkeypatch, "post", serper_page(10), serper_page(10), serper_page(4))
    out = ws.serper("LM7805", 30)
    assert len(out) == 24 and [c["json"]["page"] for c in calls] == [1, 2, 3]
    assert out[0]["url"] == "https://x.com/0" and calls[0]["headers"]["X-API-KEY"] == "k"


def test_serper_errors(monkeypatch):
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    with pytest.raises(s.BackendError, match="SERPER_API_KEY"):
        ws.serper("q", 10)
    monkeypatch.setenv("SERPER_API_KEY", "k")
    calls_to(monkeypatch, "post", Resp(403))
    with pytest.raises(s.BackendError, match="HTTP 403"):
        ws.serper("q", 10)
    calls_to(monkeypatch, "post", serper_page(10), Resp(500))
    assert len(ws.serper("q", 20)) == 10


# ---------------------------------------------------------------- Google CSE
def test_google_cse(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_CSE_ID", "cx")
    page = Resp(body={"items": [{"title": "t", "link": f"https://x.com/{i}", "snippet": ""}
                                for i in range(10)]})
    calls = calls_to(monkeypatch, "get", page, Resp(body={"items": []}))
    assert len(ws.google_cse("LM7805", 20)) == 10
    assert [c["params"]["start"] for c in calls] == [1, 11] and calls[0]["params"]["cx"] == "cx"


def test_google_cse_errors(monkeypatch):
    monkeypatch.delenv("GOOGLE_CSE_ID", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    with pytest.raises(s.BackendError, match="GOOGLE_CSE_ID"):
        ws.google_cse("q", 10)
    monkeypatch.setenv("GOOGLE_CSE_ID", "cx")
    calls_to(monkeypatch, "get", Resp(400))
    with pytest.raises(s.BackendError, match="HTTP 400"):
        ws.google_cse("q", 10)


# ---------------------------------------------------------------- DuckDuckGo
DDG_HTML = """<html><body>
<div class="result result--ad"><a class="result__a" href="https://ad.com">Ad</a></div>
<div class="result"><a class="result__a"
   href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fshop.com%2Flm7805&rut=x">LM7805 at Shop</a>
   <div class="result__snippet">LM7805 $0.89</div></div>
<div class="result"><a class="result__a" href="https://b.com/p">B</a></div>
<div class="result"><span>no link</span></div>
{next}
</body></html>"""
NEXT_FORM = ('<form><input name="q" value="LM7805"><input name="s" value="30">'
             '<input name="vqd" value="abc"></form>')


def test_clean_ddg_url():
    assert ws._clean_ddg_url("//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.com%2Fx") == "https://a.com/x"
    assert ws._clean_ddg_url("https://a.com/x") == "https://a.com/x"


def test_ddg_page_parses_results_and_next_form(monkeypatch):
    calls_to(monkeypatch, "post", Resp(text=DDG_HTML.format(next=NEXT_FORM)))
    results, nxt = ws._ddg_page({"q": "LM7805"})
    assert [r["url"] for r in results] == ["https://shop.com/lm7805", "https://b.com/p"]  # ad skipped
    assert results[0]["snippet"] == "LM7805 $0.89"
    assert nxt == {"q": "LM7805", "s": "30", "vqd": "abc"}


def test_ddg_page_retries_challenges_and_errors(monkeypatch):
    calls = calls_to(monkeypatch, "post", Resp(202), Resp(text='<div class="anomaly-modal"></div>'),
                     Resp(text=DDG_HTML.format(next="")))
    results, nxt = ws._ddg_page({"q": "x"})
    assert len(calls) == 3 and len(results) == 2 and nxt is None


def test_ddg_page_gives_up(monkeypatch):
    def boom(url, **kw):
        raise requests.ConnectionError("Connection reset")
    monkeypatch.setattr(s.requests, "post", boom)
    with pytest.raises(s.BackendError, match="Connection reset"):
        ws._ddg_page({"q": "x"})
    calls_to(monkeypatch, "post", *[Resp(text="<html></html>")] * 3)
    with pytest.raises(s.BackendError, match="no results parsed"):
        ws._ddg_page({"q": "x"})


def test_duckduckgo_follows_next_and_keeps_partial(monkeypatch):
    pages = [([{"url": "https://a.com"}], {"s": "1", "vqd": "v"}), s.BackendError("HTTP 202")]

    def fake(form):
        p = pages.pop(0)
        if isinstance(p, Exception):
            raise p
        return p
    monkeypatch.setattr(ws, "_ddg_page", fake)
    assert ws.duckduckgo("q", 10) == [{"url": "https://a.com"}]
    monkeypatch.setattr(ws, "_ddg_page", lambda form: (_ for _ in ()).throw(s.BackendError("x")))
    with pytest.raises(s.BackendError):
        ws.duckduckgo("q", 10)


# ---------------------------------------------------------------- search
def test_search_uses_first_working_backend_and_tops_up(monkeypatch):
    seen = []

    def broken(q, n):
        raise s.BackendError("no key")

    def good(q, n):
        seen.append(q)
        if q == "LM7805 price":
            return [{"title": "a", "url": "https://a.com/x#frag", "snippet": ""},
                    {"title": "a2", "url": "https://a.com/x/", "snippet": ""}]  # same page
        return [{"title": "b", "url": "https://b.com", "snippet": ""}]
    monkeypatch.setattr(ws, "BACKENDS", [("broken", broken), ("good", good)])
    name, res = s.search("LM7805 price", 5, variants=("buy LM7805",))
    assert name == "good" and seen == ["LM7805 price", "buy LM7805"]
    assert [r["url"] for r in res] == ["https://a.com/x#frag", "https://b.com"]
    assert [r["search_rank"] for r in res] == [0, 1]


def test_search_variant_failure_is_tolerated(monkeypatch):
    def backend(q, n):
        if q != "q":
            raise s.BackendError("rate limited")
        return [{"title": "a", "url": "https://a.com", "snippet": ""}]
    monkeypatch.setattr(ws, "BACKENDS", [("x", backend)])
    assert len(s.search("q", 5, variants=("buy q",))[1]) == 1


def test_search_all_failed(monkeypatch, capsys):
    monkeypatch.setattr(ws, "BACKENDS", [("empty", lambda q, n: []),
                                        ("down", lambda q, n: (_ for _ in ()).throw(
                                            requests.Timeout("timed out")))])
    with pytest.raises(RuntimeError, match="All backends failed") as e:
        s.search("q", 5, verbose=True)
    assert "empty result" in str(e.value) and "timed out" in str(e.value)
    assert "[empty] skipped" in capsys.readouterr().err


def test_search_help_only_for_rate_limits():
    assert "SERPER_API_KEY" in s._search_help("duckduckgo: HTTP 202")
    assert s._search_help("brave: HTTP 401") == "brave: HTTP 401"


# ---------------------------------------------------------------- fetching
class Stream:
    def __init__(self, status=200, ctype="text/html", body=b"<html></html>", encoding="utf-8"):
        self.status_code, self.headers = status, {"Content-Type": ctype}
        self._body, self.encoding = body, encoding

    def iter_content(self, size):
        for i in range(0, len(self._body), size):
            yield self._body[i:i + size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_html_errors_and_limits(monkeypatch):
    monkeypatch.setattr(s.requests, "get", lambda *a, **k: Stream(status=403))
    with pytest.raises(s.BackendError, match="HTTP 403"):
        s._fetch_html("https://x.com")
    monkeypatch.setattr(ws, "MAX_PAGE_BYTES", 100_000)
    monkeypatch.setattr(s.requests, "get", lambda *a, **k: Stream(body=b"a" * 500_000))
    assert len(s._fetch_html("https://x.com")) == 128 * 1024  # stops after the chunk past the cap
    monkeypatch.setattr(s.requests, "get", lambda *a, **k: Stream(body="é".encode(), encoding="bogus"))
    assert s._fetch_html("https://x.com") == "é"  # unknown charset falls back to utf-8


PRODUCT_PAGE = """<html><head>
<meta property="og:site_name" content="Acme Parts">
<script type="application/ld+json">{ld}</script></head>
<body><div>Header deal $1.00</div><h1>LM7805 Regulator</h1>
<span class="list-price">$9.99</span> $0.89 In stock: 1,200. Ships within 2 business days.
Free shipping on orders over $50</body></html>"""


def page_result(url="https://acme.com/p/lm7805", title="LM7805"):
    return {"title": title, "url": url, "snippet": "", "search_rank": 0}


def test_part_info_reads_a_product_page(monkeypatch):
    monkeypatch.setattr(s, "_fetch_html", lambda url: PRODUCT_PAGE.format(ld="{}"))
    r = s.part_info(page_result())
    assert r["cost"] == 0.89 and r["cost_source"] == "page text"  # not the banner or list price
    assert r["supplier"] == "Acme Parts" and r["page_title"] == "LM7805 Regulator"
    assert r["stock"] == 1200 and r["in_stock"] is True
    assert r["shipping_time"] == "2 business days" and r["free_ship_min"] == 50.0


def test_part_info_prefers_the_displayed_json_ld_variant(monkeypatch):
    ld = json.dumps({"@type": "Product", "name": "LM7805 Regulator", "offers": [
        {"@type": "Offer", "price": "0.50", "priceCurrency": "USD"},
        {"@type": "Offer", "price": "0.89", "priceCurrency": "USD"}]})
    monkeypatch.setattr(s, "_fetch_html", lambda url: PRODUCT_PAGE.format(ld=ld))
    r = s.part_info(page_result())
    assert r["cost"] == 0.89 and r["cost_source"] == "json-ld"


def test_part_info_fetch_error_uses_snippet_and_domain(monkeypatch):
    def boom(url):
        raise requests.ConnectionError("refused")
    monkeypatch.setattr(s, "_fetch_html", boom)
    r = s.part_info({**page_result("https://www.shop.com/search?q=lm7805"),
                     "snippet": "LM7805 from $0.40 to $0.90"})
    assert r["fetch_error"] == "refused" and r["supplier"] == "shop.com"
    assert r["listing_page"] and r["cost"] == 0.40 and r["cost_source"] == "snippet (lowest of 2)"


def test_enrich_keeps_order(monkeypatch):
    monkeypatch.setattr(s, "part_info", lambda r: {**r, "done": True})
    rs = [page_result(f"https://x.com/{i}") for i in range(12)]
    assert [r["url"] for r in s.enrich(rs)] == [r["url"] for r in rs]
    assert s.enrich([]) == []


# ---------------------------------------------------------------- JSON-LD / meta details
def soup_of(html):
    return s.BeautifulSoup(html, "html.parser")


def test_json_ld_shipping_stock_seller_and_breaks():
    ld = {"@graph": [{
        "@type": "Product", "name": "LM7805", "offers": {
            "@type": "Offer", "price": "1.00", "priceCurrency": "USD",
            "availability": "https://schema.org/OutOfStock",
            "inventoryLevel": {"value": "0"}, "seller": {"name": "Seller Co"},
            "priceSpecification": [{"price": "0.80", "eligibleQuantity": {"minValue": 10}}],
            "shippingDetails": {"shippingRate": {"value": "0"}, "deliveryTime": {
                "handlingTime": {"minValue": 0, "maxValue": 1},
                "transitTime": {"minValue": 2, "maxValue": 4}}}}}]}
    info = s._blank_info()
    s._from_json_ld(soup_of(f'<script type="application/ld+json">{json.dumps(ld)}</script>'
                            '<script type="application/ld+json">not json</script>'),
                    info, "", "LM7805")
    assert info["cost"] == 1.0 and info["in_stock"] is False and info["stock"] == 0
    assert info["supplier"] == "Seller Co" and info["price_breaks"] == [(10, 0.8)]
    assert info["shipping_cost"] == "Free" and info["shipping_time"] == "2-5 days"


def test_json_ld_organization_is_the_supplier_fallback():
    ld = {"@graph": [{"@type": "Organization", "name": "Org Name"},
                     {"@type": "Product", "name": "X", "offers": {"@type": "Offer", "price": "1"}}]}
    info = s._blank_info()
    s._from_json_ld(soup_of(f'<script type="application/ld+json">{json.dumps(ld)}</script>'),
                    info, "", "X")
    assert info["supplier"] == "Org Name"


@pytest.mark.parametrize("text, stock", [("$0.89 In stock: 1,200", 1200), ("$0.89 In stock", None),
                                         ("1,200 in stock", 1200), ("€5 available", None)])
def test_prices_are_not_read_as_stock(text, stock):
    info = s._blank_info()
    s._from_text(text, info, "page text")
    assert info["stock"] == stock


def test_json_ld_ignores_related_products_and_bulk_offers():
    ld = {"@type": "Product", "name": "LM7805", "offers": [
        {"@type": "Offer", "price": "0.89", "priceCurrency": "USD"},
        {"@type": "Offer", "price": "0.50", "eligibleQuantity": {"minValue": 100}}],
          "isRelatedTo": {"@type": "Product", "name": "Heatsink",
                          "offers": {"@type": "Offer", "price": "0.10"}}}
    info = s._blank_info()
    s._from_json_ld(soup_of(f'<script type="application/ld+json">{json.dumps(ld)}</script>'),
                    info, "", "LM7805")
    assert info["cost"] == 0.89 and (100, 0.5) in info["price_breaks"]


@pytest.mark.parametrize("lo, hi, out", [(2, 5, "2-5 days"), (None, 3, "3 days"),
                                         (3, 3, "3 days"), (2, None, "2+ days"),
                                         (None, None, None)])
def test_fmt_span(lo, hi, out):
    assert s._fmt_span(lo, hi) == out


def test_meta_itemprop_price_and_currency():
    info = s._blank_info()
    s._from_meta(soup_of('<span itemprop="price">$1.25</span><span itemprop="price">$2.00</span>'
                         '<meta itemprop="priceCurrency" content="EUR">'), info)
    assert info["cost"] == 1.25 and info["currency"] == "EUR" and info["cost_source"] == "meta"
    assert info["ld_prices"] == [1.25, 2.0]


def test_text_stock_and_out_of_stock():
    info = s._blank_info()
    s._from_text("Only 3 left", info, "page text")
    assert info["stock"] == 3 and info["in_stock"] is True
    info = s._blank_info()
    s._from_text("Currently unavailable", info, "page text")
    assert info["in_stock"] is False


@pytest.mark.parametrize("title, pack", [("LM7805 (30pcs)", 30), ("Pack of 10 LM7805", 10),
                                         ("LM7805", 1), ("LM7805 99999 pcs", 1)])
def test_pack_size(title, pack):
    assert s._pack_size(title) == pack

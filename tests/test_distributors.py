"""Offline tests for scrapers/distributors.py: the Mouser and DigiKey APIs are faked.

    pip install requests beautifulsoup4 pytest
    pytest
"""

import pytest

from scrapers import distributors as d
from scrapers import ranking as rk
from scrapers import regex_scraper


class FakeResponse:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data


MOUSER_REPLY = {"Errors": [], "SearchResults": {"NumberOfResult": 3, "Parts": [
    {"ManufacturerPartNumber": "LM7805CT/NOPB", "Manufacturer": "Texas Instruments",
     "MouserPartNumber": "926-LM7805CT/NOPB", "Description": "Linear Voltage Regulators 5V 1A",
     "ProductDetailUrl": "https://www.mouser.com/ProductDetail/926-LM7805CT-NOPB",
     "Availability": "12400 In Stock", "AvailabilityInStock": "12400", "Min": "1", "Mult": "1",
     "LeadTime": "6 Weeks",
     "PriceBreaks": [{"Quantity": 1, "Price": "$0.89", "Currency": "USD"},
                     {"Quantity": 10, "Price": "$0.62", "Currency": "USD"},
                     {"Quantity": 1000, "Price": "$0.41", "Currency": "USD"}]},
    {"ManufacturerPartNumber": "LM7805-QUOTE", "Manufacturer": "X", "PriceBreaks": []},
    {"ManufacturerPartNumber": "LM7805CT-REEL", "Manufacturer": "onsemi",
     "MouserPartNumber": "863-LM7805CT-REEL", "Description": "Reel only",
     "ProductDetailUrl": "https://www.mouser.com/ProductDetail/863-LM7805CT-REEL",
     "Availability": "On Order", "AvailabilityInStock": None, "Min": "2500", "Mult": "2500",
     "LeadTime": "16 Weeks",
     "PriceBreaks": [{"Quantity": 2500, "Price": "$0.30", "Currency": "USD"}]},
]}}

DIGIKEY_REPLY = {"SearchLocaleUsed": {"Currency": "USD"}, "ExactMatches": [], "Products": [
    {"ManufacturerProductNumber": "LM7805CT/NOPB", "Manufacturer": {"Name": "Texas Instruments"},
     "Description": {"ProductDescription": "IC REG LINEAR 5V 1A TO220-3",
                     "DetailedDescription": "Linear Voltage Regulator IC Positive Fixed 5V 1A"},
     "ProductUrl": "https://www.digikey.com/en/products/detail/texas-instruments/LM7805CT-NOPB/1",
     "QuantityAvailable": 5000, "ManufacturerLeadWeeks": "6",
     "ProductVariations": [
         {"DigiKeyProductNumber": "LM7805CT/NOPB-ND", "PackageType": {"Name": "Tube"},
          "MinimumOrderQuantity": 1, "StandardPackage": 50,
          "StandardPricing": [{"BreakQuantity": 1, "UnitPrice": 0.95},
                              {"BreakQuantity": 50, "UnitPrice": 0.55}]},
         {"DigiKeyProductNumber": "LM7805CT/NOPB-DKR", "PackageType": {"Name": "Digi-Reel®"},
          "MinimumOrderQuantity": 1,
          "StandardPricing": [{"BreakQuantity": 1, "UnitPrice": 0.01}]}]},
    {"ManufacturerProductNumber": "LM7805-OBS", "Manufacturer": {"Name": "Y"},
     "ProductVariations": []},
]}


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("MOUSER_API_KEY", "m")
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "id")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "secret")
    monkeypatch.delenv("MOUSER_FREE_SHIP_MIN", raising=False)
    monkeypatch.setattr(d, "_token", {"value": None, "expires": 0.0})
    calls = []

    def post(url, **kw):
        calls.append((url, kw))
        if url == d.MOUSER_URL:
            return FakeResponse(MOUSER_REPLY)
        if url == d.DIGIKEY_TOKEN_URL:
            return FakeResponse({"access_token": "tok", "expires_in": 600})
        if url == d.DIGIKEY_SEARCH_URL:
            return FakeResponse(DIGIKEY_REPLY)
        raise AssertionError(url)
    monkeypatch.setattr(d.requests, "post", post)
    return calls


# ---------------------------------------------------------------- parsing helpers
@pytest.mark.parametrize("s, v", [("$0.89", 0.89), ("$1,234.50", 1234.5), ("0,89 €", 0.89),
                                  ("1,234", 1234.0), ("", None), ("$0.00", None)])
def test_price_strings(s, v):
    assert d._price(s) == v


def test_order_units_rounds_up_to_minimum_and_multiple():
    assert d.order_units(5) == 5
    assert d.order_units(5, 10) == 10
    assert d.order_units(2600, 2500, 2500) == 5000


# ---------------------------------------------------------------- Mouser
def test_mouser_parts(keys):
    rs = d.mouser("LM7805", qty=1, n=10)
    assert [r["title"].split()[0] for r in rs] == ["LM7805CT/NOPB", "LM7805CT-REEL"]  # quote-only skipped
    r = rs[0]
    assert r["cost"] == 0.89 and r["currency"] == "USD" and r["cost_source"] == "api"
    assert r["price_breaks"] == [(1, 0.89), (10, 0.62), (1000, 0.41)]
    assert r["stock"] == 12400 and r["in_stock"] is True
    assert r["shipping_time"] == "ships same day" and r["shipping_cost"] is None
    reel = rs[1]
    assert reel["stock"] == 0 and reel["in_stock"] is False
    assert reel["shipping_time"] == "16 weeks" and reel["min_order"] == 2500


def test_mouser_errors_raise(keys, monkeypatch):
    monkeypatch.setattr(d.requests, "post",
                        lambda *a, **k: FakeResponse({"Errors": [{"Message": "Invalid key"}]}))
    with pytest.raises(d.APIError, match="Invalid key"):
        d.mouser("LM7805")


def test_missing_keys_skip_quietly(monkeypatch):
    for k in ("MOUSER_API_KEY", "DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    assert d.lookup("LM7805") == ([], [])


def test_minimum_order_sets_quantity_bought(keys):
    reel = d.mouser("LM7805", qty=10)[1]
    r = regex_scraper.apply_qty(reel, 10)
    assert r["packs_needed"] == 2500 and r["subtotal"] == 750.0


# ---------------------------------------------------------------- DigiKey
def test_digikey_parts(keys):
    rs = d.digikey("LM7805", qty=1, n=10)
    assert len(rs) == 1  # no-pricing product skipped
    r = rs[0]
    assert "LM7805CT/NOPB-ND" in r["title"]  # Digi-Reel (excludes reeling fee) not chosen
    assert r["price_breaks"] == [(1, 0.95), (50, 0.55)] and r["supplier"] == "DigiKey"
    assert r["stock"] == 5000 and r["shipping_time"] == "ships same day"
    search = next(kw for url, kw in keys if url == d.DIGIKEY_SEARCH_URL)
    assert search["headers"]["Authorization"] == "Bearer tok"


def test_digikey_token_is_cached(keys):
    d.digikey("LM7805"); d.digikey("LM7805")
    assert sum(url == d.DIGIKEY_TOKEN_URL for url, _ in keys) == 1


# ---------------------------------------------------------------- merging + ranking
def test_api_sites_dropped_from_web_results():
    web = [{"url": "https://www.mouser.co.uk/ProductDetail/x"},
           {"url": "https://www.digikey.com/en/products/x"},
           {"url": "https://www.lcsc.com/product-detail/x"}]
    assert d.drop_api_sites(web, ["mouser"]) == web[1:]
    assert d.drop_api_sites(web, []) == web


def test_api_results_rank_with_price_breaks(keys):
    api, used = d.lookup("LM7805", qty=50, n=10)
    assert used == ["mouser", "digikey"]
    rs = [regex_scraper.apply_qty(r, 50) for r in api]
    rk.Scorer().score_all(rs, "LM7805", 50)
    top = rk.Scorer.pick_top(rs, 2)
    assert top[0]["supplier"] == "DigiKey" and top[0]["subtotal"] == 27.5   # 50 @ 0.55
    assert top[1]["supplier"] == "Mouser Electronics" and top[1]["subtotal"] == 31.0  # 50 @ 0.62


# ---------------------------------------------------------------- errors and edges
def test_http_errors_raise(keys, monkeypatch):
    monkeypatch.setattr(d.requests, "post", lambda url, **kw: FakeResponse({}, status=500))
    with pytest.raises(d.APIError, match="Mouser HTTP 500"):
        d.mouser("LM7805")
    with pytest.raises(d.APIError, match="DigiKey token HTTP 500"):
        d.digikey("LM7805")
    d._token.update(value="tok", expires=d.time.time() + 600)
    with pytest.raises(d.APIError, match="DigiKey HTTP 500"):
        d.digikey("LM7805")


def test_digikey_needs_keys(monkeypatch):
    monkeypatch.delenv("DIGIKEY_CLIENT_ID", raising=False)
    with pytest.raises(d.APIError, match="DIGIKEY_CLIENT_ID"):
        d.digikey("LM7805")


def test_results_capped_at_n(keys):
    assert len(d.mouser("LM7805", n=1)) == 1


def test_mouser_stock_from_availability_text(keys, monkeypatch):
    part = {**MOUSER_REPLY["SearchResults"]["Parts"][0], "AvailabilityInStock": None,
            "Availability": "3,000 In Stock"}
    monkeypatch.setattr(d.requests, "post", lambda url, **kw: FakeResponse(
        {"Errors": [], "SearchResults": {"Parts": [part]}}))
    assert d.mouser("LM7805")[0]["stock"] == 3000


def test_digikey_reel_uses_full_reel_multiples(keys, monkeypatch):
    product = {"ManufacturerProductNumber": "LM7805", "Manufacturer": {"Name": "TI"},
               "QuantityAvailable": 0, "ManufacturerLeadWeeks": "12", "ProductVariations": [
                   {"PackageType": {"Name": "Tape & Reel (TR)"}, "MinimumOrderQuantity": 2500,
                    "StandardPackage": 2500, "StandardPricing": [{"BreakQuantity": 2500, "UnitPrice": 0.2}]},
                   {"PackageType": {"Name": "Bulk"}, "StandardPricing": []}]}
    monkeypatch.setattr(d.requests, "post", lambda url, **kw: FakeResponse(
        {"access_token": "t"} if url == d.DIGIKEY_TOKEN_URL else {"Products": [product]}))
    r = d.digikey("LM7805", qty=3000)[0]
    assert (r["min_order"], r["order_mult"]) == (2500, 2500)
    assert r["in_stock"] is False and r["shipping_time"] == "12 weeks"


def test_lookup_logs_failures(keys, monkeypatch, capsys):
    monkeypatch.setattr(d, "APIS", [("mouser", lambda *a: (_ for _ in ()).throw(d.APIError("down")))])
    assert d.lookup("LM7805", verbose=True) == ([], [])
    assert "[mouser api] skipped: down" in capsys.readouterr().err


@pytest.mark.parametrize("v, n", [("12,400", 12400), (12400, 12400), ("12400 In Stock", 12400),
                                  (None, None), ("On Order", None)])
def test_int(v, n):
    assert d._int(v) == n


def test_env_float(monkeypatch):
    monkeypatch.setenv("MOUSER_FREE_SHIP_MIN", "50")
    assert d._env_float("MOUSER_FREE_SHIP_MIN") == 50.0
    monkeypatch.setenv("MOUSER_FREE_SHIP_MIN", "fifty")
    assert d._env_float("MOUSER_FREE_SHIP_MIN") is None

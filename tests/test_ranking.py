"""Offline tests for scrapers/ranking.py's Weights / Scorer.

    pip install requests beautifulsoup4 pytest
    pytest
"""
import json

import pytest

from scrapers import ranking as rk
from scrapers import regex_scraper


def candidate(i, cost, **kw):
    r = {"title": "LM7805 voltage regulator", "url": f"https://site{i}.com/p/{i}",
         "snippet": "", "search_rank": i, **regex_scraper._blank_info()}
    r.update(cost=cost, cost_source="json-ld", supplier=f"site{i}", in_stock=True,
             shipping_cost="Free", shipping_time="2 days")
    r.update(kw)
    return regex_scraper.apply_qty(r, 1)


def test_cheapest_clean_option_scores_one():
    rs = [candidate(0, 1.00), candidate(1, 2.00)]
    rk.Scorer().score_all(rs, "LM7805", 1)
    assert rs[0]["score"] == pytest.approx(1.0) and rs[1]["score"] > 1.9


def test_custom_weights_change_the_ranking():
    def ranked(weights):
        rs = [candidate(0, 1.00, shipping_time="12 days"), candidate(1, 1.10)]
        scorer = rk.Scorer(weights)
        scorer.score_all(rs, "LM7805", 1)
        return [r["supplier"] for r in scorer.pick_top(rs, 2)]

    assert ranked(rk.Weights()) == ["site1", "site0"]  # slow shipping costs ~20%
    assert ranked(rk.Weights(per_ship_day=0)) == ["site0", "site1"]


def test_unknown_source_weight():
    r = candidate(0, 1.00, cost_source="mystery")
    rk.Scorer(rk.Weights(unknown_source=1.5)).score_all([r], "LM7805", 1)
    assert r["score"] == pytest.approx(1.5)


def test_scorers_do_not_share_weights():
    a, b = rk.Weights(), rk.Weights()
    a.source["snippet"] = 9
    assert b.source["snippet"] == 1.15


def test_load_overrides_and_merges_source(tmp_path):
    f = tmp_path / "w.json"
    f.write_text(json.dumps({"out_of_stock": 2.5, "source": {"snippet": 1.3}}))
    w = rk.Weights.load(f)
    assert w.out_of_stock == 2.5 and w.short_stock == rk.Weights().short_stock
    assert w.source["snippet"] == 1.3 and w.source["json-ld"] == 1.0


def test_load_rejects_typos(tmp_path):
    f = tmp_path / "w.json"
    f.write_text(json.dumps({"out_of_stok": 2}))
    with pytest.raises(ValueError, match="out_of_stok"):
        rk.Weights.load(f)


@pytest.mark.parametrize("ship,value", [("Free", 0.0), ("$5.99", 5.99), ("4.50 USD", 4.5),
                                        (None, None), ("call us", None)])
def test_ship_value(ship, value):
    assert rk.ship_value(ship) == value


def test_scraper_uses_the_default_scorer():
    assert regex_scraper.score_all.__self__ is regex_scraper.SCORER
    assert regex_scraper.SCORER.w == rk.Weights()


def test_wrong_part_never_sets_baseline_when_all_listings_have_notes():
    # Every right part shows a suffix (LM7805CT); a cheaper MC7805 lacks the part number.
    rs = [candidate(0, 0.37, title="MC7805CTG onsemi"),
          candidate(1, 1.07, title="LM7805CT/NOPB Texas Instruments")]
    rk.Scorer().score_all(rs, "LM7805", 1)
    assert rs[1]["score"] < 1.2   # its own price is the baseline, not the MC7805's


# ---------------------------------------------------------------- ship_days / multiplier
from datetime import date  # noqa: E402


@pytest.mark.parametrize("text, days", [
    (None, None), ("", None), ("Ships today", 1), ("same day shipping", 1),
    ("ships next business day", 2), ("3-5 business days", 5), ("1 to 2 weeks", 14),
    ("48 hours", 2), ("Arrives Tue, Oct 20", 5), ("Get it Jan 3", 80),
    ("Arrives Feb 30", None), ("contact us", None)])
def test_ship_days_formats(text, days):
    assert rk.ship_days(text, today=date(2026, 10, 15)) == days


def test_out_of_stock_short_stock_and_slow_shipping_notes():
    out = candidate(0, 1.0, in_stock=False, stock=None)
    short = candidate(1, 1.0, stock=0)
    slow = candidate(2, 1.0, shipping_time="3 weeks")
    rs = [out, short, slow]
    scraper_qty = [regex_scraper.apply_qty(r, 5) for r in rs]
    rk.Scorer().score_all(scraper_qty, "LM7805", 5)
    assert "out of stock" in out["notes"]
    assert "stock below 5" in short["notes"]
    assert "slow: ~21 days" in slow["notes"] and slow["ship_days"] == 21


def test_unknown_stock_and_ship_time_cost_a_little():
    known = candidate(0, 1.0)
    unknown = candidate(1, 1.0, in_stock=None, shipping_time=None)
    rk.Scorer(rk.Weights(per_search_rank=0)).score_all([known, unknown], "LM7805", 1)
    assert unknown["score"] == pytest.approx(1.03 * 1.06, abs=1e-3)  # scores are rounded


def test_outlier_needs_enough_prices():
    rs = [candidate(i, 1.0) for i in range(4)] + [candidate(9, 0.10)]
    rk.Scorer().score_all(rs, "LM7805", 1)
    assert "far cheaper than others - verify item" in rs[-1]["notes"]


def test_pick_top_caps_per_supplier_then_fills():
    rs = [candidate(i, 1.0 + i / 100, supplier="same") for i in range(4)] + [candidate(9, 2.0)]
    rk.Scorer().score_all(rs, "LM7805", 1)
    top = rk.Scorer.pick_top(rs, 3, per_supplier=2)
    assert [r["search_rank"] for r in top] == [0, 1, 9]  # third "same" waits behind site9
    assert len(rk.Scorer.pick_top(rs, 5, per_supplier=1)) == 5  # cap left gaps, filled


def test_relevance_counts_missing_words():
    frac, key_hit, _ = rk.relevance("LM7805 voltage regulator",
                                    {"title": "LM7805", "snippet": "", "url": "https://x.com"})
    assert frac == pytest.approx(1 / 3) and key_hit
    frac, key_hit, _ = rk.relevance("LM7805", {"title": "7805 regulator", "snippet": "",
                                               "url": "https://x.com"})
    assert frac == 0 and not key_hit

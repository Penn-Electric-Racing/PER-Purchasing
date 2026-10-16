"""Offline tests for the command-line entry points: scraper.main, scraper2.find/main and
price_check.main. Search, page fetches, the LLM, Notion and Slack are all faked, and
load_env is disabled so the real .env keys are never read.

    pip install requests beautifulsoup4 pytest
    pytest
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import distributors  # noqa: E402
import price_check as pc  # noqa: E402
import scraper  # noqa: E402
import scraper2  # noqa: E402

HITS = [{"title": "LM7805 regulator", "url": "https://a.com/lm7805", "snippet": "LM7805 $0.89",
         "search_rank": 0},
        {"title": "LM7805 at Mouser", "url": "https://www.mouser.com/p/lm7805", "snippet": "",
         "search_rank": 1},
        {"title": "LM7805 10pcs", "url": "https://b.com/lm7805", "snippet": "LM7805 (10pcs) $4.00",
         "search_rank": 2}]


def api_part(cost=0.75):
    """A Mouser result as distributors.lookup returns it (search_rank set)."""
    return {"search_rank": 0, **distributors._candidate("LM7805CT Texas Instruments | Mouser", "https://mouser.com/x",
                                   "Linear regulator", "Mouser Electronics", cost=cost,
                                   currency="USD", price_breaks=[(1, cost)], stock=500,
                                   in_stock=True, shipping_time="ships same day")}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No .env, no real network: tests opt in to each fake they need."""
    monkeypatch.setattr(scraper, "load_env", lambda *a: None)
    monkeypatch.setattr(scraper2, "load_env", lambda *a: None)
    monkeypatch.setattr(pc, "load_env", lambda *a: None)
    monkeypatch.setattr(distributors, "lookup", lambda *a, **k: ([], []))
    monkeypatch.setattr(scraper.time, "sleep", lambda t: None)


def run(module, monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", [module.__name__ + ".py", *argv])
    module.main()


def fake_search(monkeypatch, module, hits=HITS, error=None):
    calls = []

    def search(query, n, verbose=False, variants=()):
        calls.append((query, n, variants))
        if error:
            raise RuntimeError(error)
        return "fake", [dict(h) for h in hits]
    monkeypatch.setattr(module, "search", search)
    return calls


# ---------------------------------------------------------------- scraper.py
def test_scraper_no_fetch_text_output(monkeypatch, capsys):
    calls = fake_search(monkeypatch, scraper)
    run(scraper, monkeypatch, "LM7805", "-q", "10", "--no-fetch", "-n", "2")
    out = capsys.readouterr().out
    assert calls == [("LM7805 price", 30, ("buy LM7805", "LM7805 in stock"))]
    assert out.startswith('Best 2 of 3 results for "LM7805"  x10  (via fake;')
    assert "1. LM7805 10pcs" in out and "1 pack(s) of 10 = 4.00 USD" in out  # 0.40/unit beats 0.89


def test_scraper_json_and_exact(monkeypatch, capsys):
    calls = fake_search(monkeypatch, scraper)
    monkeypatch.setattr(scraper, "enrich", lambda rs: [
        {**r, **scraper._blank_info(), "cost": 1.0, "currency": "USD", "cost_source": "json-ld",
         "supplier": r["url"].split("/")[2]} for r in rs])
    run(scraper, monkeypatch, "LM7805", "--exact", "--json", "--rank", "cheapest")
    data = json.loads(capsys.readouterr().out)
    assert calls[0] == ("LM7805", 30, ())
    assert data["query"] == "LM7805" and data["rank"] == "cheapest" and data["priced"] == 3
    assert len(data["results"]) == 3 and data["results"][0]["total"] is None


def test_scraper_adds_api_results_and_drops_their_site(monkeypatch, capsys):
    fake_search(monkeypatch, scraper)
    monkeypatch.setattr(distributors, "lookup", lambda *a, **k: ([api_part()], ["mouser"]))
    fetched = []
    monkeypatch.setattr(scraper, "enrich", lambda rs: fetched.extend(rs) or
                        [{**r, **scraper._blank_info(), "supplier": "x"} for r in rs])
    run(scraper, monkeypatch, "LM7805", "--json")
    data = json.loads(capsys.readouterr().out)
    assert "www.mouser.com" not in " ".join(r["url"] for r in fetched)  # API replaces the page
    assert data["source"] == "fake + mouser api" and data["results"][0]["cost_source"] == "api"


def test_scraper_api_only_when_search_fails(monkeypatch, capsys):
    fake_search(monkeypatch, scraper, error="All backends failed: HTTP 202")
    monkeypatch.setattr(distributors, "lookup", lambda *a, **k: ([api_part()], ["mouser"]))
    run(scraper, monkeypatch, "LM7805", "--json")
    data = json.loads(capsys.readouterr().out)
    assert data["source"] == "mouser api" and data["candidates"] == 1


def test_scraper_exits_with_help_when_everything_fails(monkeypatch):
    fake_search(monkeypatch, scraper, error="All backends failed: HTTP 202")
    with pytest.raises(SystemExit, match="SERPER_API_KEY"):
        run(scraper, monkeypatch, "LM7805")


@pytest.mark.parametrize("argv", [["x", "-q", "0"], ["x", "-n", "5", "--pool", "2"],
                                  ["x", "--api", "-1"], ["x", "--weights", "/no/such.json"]])
def test_scraper_rejects_bad_arguments(monkeypatch, argv):
    with pytest.raises(SystemExit):
        run(scraper, monkeypatch, *argv)


# ---------------------------------------------------------------- scraper2.find / main
class FakeLLM:
    provider, model = "fake", "fake-model"

    def __init__(self, *a, **k):
        self.prompts = []

    def json(self, system, user):
        self.prompts.append(user)
        if "Return {\"results\"" in system or "=== [" in user:
            n = user.count("=== [") or user.count("\n[")
            return {"results": [{"index": i, "cost": 1.0 + i, "currency": "USD"}
                                for i in range(max(n, 3))]}
        return {"cost": 2.0, "currency": "USD"}


def test_find_reads_extra_links_and_dedupes(monkeypatch):
    fake_search(monkeypatch, scraper2)
    monkeypatch.setattr(scraper2, "_fetch_html", lambda url: "<h1>LM7805</h1>")
    llm = FakeLLM()
    _, source, results = scraper2.find("LM7805", 5, llm, batch=1, extra=[
        {"title": "mine", "url": "https://mine.com/p"}, {"title": "dup", "url": HITS[0]["url"]}])
    urls = [r["url"] for r in results]
    assert urls.count(HITS[0]["url"]) == 1 and "https://mine.com/p" in urls
    assert source == "fake" and len(llm.prompts) == 4  # 3 hits + 1 extra, one page per request
    assert all(r["score"] is not None for r in results)


def test_find_with_only_extra_links_when_search_fails(monkeypatch):
    fake_search(monkeypatch, scraper2, error="All backends failed")
    monkeypatch.setattr(scraper2, "_fetch_html", lambda url: "<h1>x</h1>")
    _, source, results = scraper2.find("LM7805", 1, FakeLLM(), extra=[{"title": "m", "url": "https://m.com"}])
    assert source == "none" and [r["url"] for r in results] == ["https://m.com"]
    fake_search(monkeypatch, scraper2, error="All backends failed")
    with pytest.raises(RuntimeError):
        scraper2.find("LM7805", 1, FakeLLM())


def test_scraper2_main_json(monkeypatch, capsys):
    fake_search(monkeypatch, scraper2)
    monkeypatch.setattr(scraper2, "LLM", FakeLLM)
    monkeypatch.setattr(scraper2, "_fetch_html", lambda url: "<h1>LM7805</h1>")
    run(scraper2, monkeypatch, "LM7805", "--json", "--batch", "3")
    data = json.loads(capsys.readouterr().out)
    assert data["llm"] == "fake/fake-model" and data["priced"] == 3


def test_scraper2_main_no_fetch_text(monkeypatch, capsys):
    fake_search(monkeypatch, scraper2)
    monkeypatch.setattr(scraper2, "LLM", FakeLLM)
    run(scraper2, monkeypatch, "LM7805", "--no-fetch", "-n", "1")
    out = capsys.readouterr().out
    assert "via fake + fake/fake-model" in out and "Cost:          1.00 USD  (llm snippet)" in out


def test_scraper2_main_errors(monkeypatch):
    def no_llm(*a, **k):
        raise scraper2.LLMError("no LLM configured")
    monkeypatch.setattr(scraper2, "LLM", no_llm)
    with pytest.raises(SystemExit, match="no LLM configured"):
        run(scraper2, monkeypatch, "LM7805")
    monkeypatch.setattr(scraper2, "LLM", FakeLLM)
    with pytest.raises(SystemExit):
        run(scraper2, monkeypatch, "LM7805", "--batch", "0")
    fake_search(monkeypatch, scraper2, error="All backends failed")
    with pytest.raises(SystemExit, match="All backends failed"):
        run(scraper2, monkeypatch, "LM7805")


# ---------------------------------------------------------------- price_check.main
ROW = {"id": "row1", "url": "https://www.notion.so/row1",
       "parent": {"type": "data_source_id", "data_source_id": pc.DEFAULT_PURCHASING_DS},
       "properties": {
    "Season": {"select": {"name": "REV12"}}, "Exported?": {"checkbox": False},
    "Name": {"type": "title", "title": [{"plain_text": "5V regulator"}]},
    "Cat. or model no.": {"rich_text": [{"plain_text": "LM7805"}]},
    "Quant.": {"number": 10}, "Unit cost": {"number": 1.0},
    "Link to product webpage": {"url": "https://a.com/lm7805"},
    "Point of contact": {"people": [{"id": "u1", "name": "Ada", "person": {"email": "a@x.edu"}}]},
}}


class FakeNotionAPI:
    """Stands in for price_check.Notion (constructed by main)."""
    instances = []

    entries = []

    def __init__(self, token, version):
        self.token, self.created, self.limits, self.done = token, [], [], []
        FakeNotionAPI.instances.append(self)

    def query(self, ds, filter_=None, page_size=100, sorts=None, limit=None):
        if "title" in filter_:
            return []
        self.limits.append(limit)
        if "rich_text" in filter_:  # the queue
            return [{"id": e, "properties": {"Run key": {"title": [{"plain_text": f"price-check-queue {p}"}]}}}
                    for e, p in FakeNotionAPI.entries][:limit]
        return [ROW]

    def page(self, page_id):
        return {**ROW, "id": page_id}

    def update(self, page_id, props):
        self.done.append(page_id)

    def create(self, ds, props):
        self.created.append(props)

    def users(self):
        return {}


class FakeSlackAPI:
    posts = []

    def __init__(self, token):
        pass

    def directory(self):
        return [("S1", ["Ada"], "a@x.edu")]

    def lookup_email(self, email):
        return None

    def post(self, channel, text):
        FakeSlackAPI.posts.append((channel, text))


@pytest.fixture
def notion_env(monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "n")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "s")
    FakeNotionAPI.instances.clear()
    FakeNotionAPI.entries = []
    FakeSlackAPI.posts.clear()
    monkeypatch.setattr(pc, "Notion", FakeNotionAPI)
    monkeypatch.setattr(pc, "Slack", FakeSlackAPI)
    monkeypatch.setattr(scraper2, "LLM", FakeLLM)
    searched = []

    def find(base, qty, llm, pool=15, extra=(), verbose=False, **k):
        searched.append((base, qty, [e["url"] for e in extra]))
        r = {"title": "LM7805", "url": "https://a.com/lm7805", "snippet": "", "search_rank": 0,
             **scraper._blank_info(), "cost": 2.0, "currency": "USD", "cost_source": "llm",
             "supplier": "a.com", "in_stock": True}
        rs = [scraper2.apply_qty(r, qty)]
        scraper2.Scorer().score_all(rs, base, qty)
        return "LM7805 price", "fake", rs
    monkeypatch.setattr(scraper2, "find", find)
    return searched


def test_price_check_sweep_alerts(monkeypatch, notion_env, capsys):
    run(pc, monkeypatch, "--all", "--limit", "5")
    notion = FakeNotionAPI.instances[0]
    assert notion.limits == [5]
    assert notion_env == [("LM7805", 10, ["https://a.com/lm7805"])]
    assert FakeSlackAPI.posts[0][0] == "S1" and "+100%" in FakeSlackAPI.posts[0][1]
    assert len(notion.created) == 1  # the Job Log claim
    assert "Price check: 1 alerted" in capsys.readouterr().err


def test_price_check_single_row_dry_run(monkeypatch, notion_env):
    run(pc, monkeypatch, "--row", "https://www.notion.so/X-0123456789abcdef0123456789abcdef",
        "--dry-run")
    assert FakeSlackAPI.posts == [] and FakeNotionAPI.instances[0].created == []


def test_price_check_queue_drains_entries(monkeypatch, notion_env, capsys):
    FakeNotionAPI.entries = [("e1", "row1")]
    monkeypatch.setenv("PRICE_CHECK_BATCH", "4")
    run(pc, monkeypatch, "--queue")
    notion = FakeNotionAPI.instances[0]
    assert notion.limits == [None, 4] and notion.done == ["e1"]  # discovery, then the queue
    assert len(notion.created) == 2  # row1 discovered + queued, then its alert claim
    err = capsys.readouterr().err
    assert FakeSlackAPI.posts and "1 new row(s) queued" in err and "Price check: 1 alerted" in err


def test_price_check_queue_dry_run_previews_discovered_rows(monkeypatch, notion_env, capsys):
    run(pc, monkeypatch, "--queue", "--dry-run")
    notion = FakeNotionAPI.instances[0]
    assert notion.created == [] and notion.done == []  # nothing written
    assert notion_env and "1 new row(s) would be queued" in capsys.readouterr().err


def test_price_check_empty_queue_skips_the_llm(monkeypatch, notion_env, capsys):
    monkeypatch.setenv("PRICE_CHECK_LOOKBACK_HOURS", "0")  # no discovery
    monkeypatch.setattr(scraper2, "LLM", lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM used")))
    run(pc, monkeypatch, "--queue")
    assert "queue is empty" in capsys.readouterr().err and notion_env == []


def test_price_check_enqueue(monkeypatch, notion_env, capsys):
    monkeypatch.delenv("SLACK_BOT_TOKEN")  # not needed to queue
    run(pc, monkeypatch, "--enqueue", "https://www.notion.so/X-0123456789abcdef0123456789abcdef")
    assert FakeNotionAPI.instances[0].created[0]["Run key"]["title"][0]["text"]["content"] == \
        "price-check-queue 0123456789abcdef0123456789abcdef"
    assert "queued" in capsys.readouterr().err


def test_price_check_needs_row_or_all(monkeypatch, notion_env):
    with pytest.raises(SystemExit):
        run(pc, monkeypatch)


def test_price_check_main_requires_tokens(monkeypatch):
    for k in ("NOTION_TOKEN", "NOTION_API_KEY", "SLACK_BOT_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(SystemExit, match="NOTION_TOKEN / NOTION_API_KEY"):
        run(pc, monkeypatch, "--all")
    monkeypatch.setenv("NOTION_API_KEY", "n")
    with pytest.raises(SystemExit, match="SLACK_BOT_TOKEN"):
        run(pc, monkeypatch, "--all")

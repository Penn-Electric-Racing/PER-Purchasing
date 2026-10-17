"""Offline tests for pricecheck: Notion, Slack and the search are faked.

    pip install requests beautifulsoup4 pytest
    pytest
"""
import json
import sys
import time
from datetime import datetime, timezone

import pytest
import requests

import pricecheck as pc
from pricecheck import check
from pricecheck.api import MAX_RETRY_WAIT, RATE_LIMIT_RETRIES
from pricecheck.config import DEFAULT_JOB_LOG_DS, DEFAULT_PURCHASING_DS
from scrapers import llm_scraper, regex_scraper
from scrapers.ranking import Scorer

CFG = pc.Config(notion_token="n", slack_token="s", threshold_pct=10, min_delta_usd=0)


def page(pid="p1", part="LM7805CT/NOPB", qty=50, cost=1.00, link="https://www.digikey.com/x",
         contacts=(("u1", "Ada Lovelace", "ada@upenn.edu"),), notes="", season="REV12",
         exported=False, parent=DEFAULT_PURCHASING_DS):
    return {"id": pid, "url": f"https://www.notion.so/{pid}",
            "parent": {"type": "data_source_id", "data_source_id": parent}, "properties": {
        "Name": {"type": "title", "title": [{"plain_text": "5V regulator"}]},
        "Cat. or model no.": {"type": "rich_text", "rich_text": [{"plain_text": part}]},
        "Quant.": {"type": "number", "number": qty},
        "Unit cost": {"type": "number", "number": cost},
        "Link to product webpage": {"type": "url", "url": link},
        "Notes (internal)": {"type": "rich_text", "rich_text": [{"plain_text": notes}]},
        "Point of contact": {"type": "people", "people": [
            {"id": i, "name": n, "person": {"email": e}} for i, n, e in contacts]},
        "Season": {"type": "select", "select": {"name": season} if season else None},
        "Exported?": {"type": "checkbox", "checkbox": exported},
    }}


def listing(url, unit, qty=50, title="LM7805CT/NOPB regulator", supplier=None, **kw):
    r = {"title": title, "url": url, "snippet": "", "search_rank": 0, **regex_scraper._blank_info()}
    r.update({"cost": unit, "currency": "USD", "cost_source": "llm",
              "supplier": supplier or url.split("/")[2], "in_stock": True,
              "shipping_cost": "Free", "shipping_time": "2 days", **kw})
    return regex_scraper.apply_qty(r, qty)


def scored(*rs, query="LM7805CT/NOPB", qty=50):
    rs = list(rs)
    Scorer().score_all(rs, query, qty)
    return rs


class FakeNotion:
    """Job Log claims (by key), queue entries, Purchasing pages and the open-rows query."""

    def __init__(self, claimed=(), rows=(), entries=(), pages=None):
        self.claimed, self.created, self.rows, self.queries = set(claimed), [], list(rows), []
        self.ledger = []
        self.entries, self.pages, self.done = list(entries), pages or {}, []

    def query(self, ds, filter_=None, page_size=100, sorts=None, limit=None):
        if "title" in filter_:  # Job Log claim
            return [{"id": "x"}] if filter_["title"]["equals"] in self.claimed else []
        self.queries.append((filter_, sorts, limit))
        if "rich_text" in filter_:  # queue entries
            return [{"id": eid, "properties": {"Run key": {"title": [{"plain_text": key}]}}}
                    for eid, key in self.entries][:limit]
        return self.rows[:limit]  # open rows / discovery

    def create(self, ds, props):
        if "Run key" not in props:   # a savings-ledger row
            self.ledger.append(props)
            return
        self.created.append(props["Run key"]["title"][0]["text"]["content"])

    def page(self, pid):
        if pid not in self.pages:
            raise RuntimeError(f"Notion GET /pages/{pid}: HTTP 404 object_not_found")
        page_ = self.pages[pid]
        if isinstance(page_, Exception):
            raise page_
        return page_

    def update(self, page_id, props):
        assert props == {"Job": {"rich_text": [{"text": {"content": "price-check-done"}}]}}
        self.done.append(page_id)

    def users(self):
        return {}


class FakeSlack:
    def __init__(self, members=(("S1", ["Ada Lovelace"], "ada@upenn.edu"),)):
        self.members, self.posts, self.blocks = list(members), [], []

    def directory(self):
        return self.members

    def lookup_email(self, email):
        return None

    def post(self, channel, text, blocks=None):
        self.posts.append((channel, text))
        self.blocks.append(blocks)


# ---------------------------------------------------------------- rows
def test_parse_row():
    r = pc.parse_row(page())
    assert (r.name, r.part_number, r.quantity, r.unit_cost) == ("5V regulator", "LM7805CT/NOPB", 50, 1.0)
    assert r.contacts == [{"id": "u1", "name": "Ada Lovelace", "email": "ada@upenn.edu"}]
    assert r.pack_size == 1 and r.units == 50


def test_parts_pack_rows_compare_per_unit():
    # /parts: "Sold in packs of 10: 5 pack(s) = 50 units", Unit cost = per pack.
    r = pc.parse_row(page(qty=5, cost=8.0, notes="Added from /parts. Sold in packs of 10: 5 pack(s)."))
    assert r.units == 50 and r.unit_price == 0.8
    assert pc.compare(r, scored(listing(r.link, 0.8)), CFG) is None


@pytest.mark.parametrize("s", ["https://www.notion.so/team/5V-regulator-0123456789abcdef0123456789abcdef?pvs=4",
                               "0123456789abcdef0123456789abcdef",
                               "01234567-89ab-cdef-0123-456789abcdef"])
def test_page_id(s):
    assert check._page_id(s).replace("-", "") == "0123456789abcdef0123456789abcdef"


# ---------------------------------------------------------------- compare
def test_link_price_is_compared_first():
    row = pc.parse_row(page(cost=1.00))
    f = pc.compare(row, scored(listing(row.link, 1.25), listing("https://cheap.com/p", 1.05)), CFG)
    assert f.at_link and f.found_unit == 1.25 and f.delta_pct == 25.0
    assert f.cheaper is None  # 1.05 isn't below the sheet's 1.00


def test_best_option_used_when_link_unpriced():
    row = pc.parse_row(page(cost=1.00))
    f = pc.compare(row, scored(listing(row.link, None), listing("https://shop.com/p", 0.70)), CFG)
    assert not f.at_link and f.found_unit == 0.70 and f.delta_pct == -30.0


def test_small_change_or_small_total_is_not_alerted():
    row = pc.parse_row(page(cost=1.00, qty=2))
    assert pc.compare(row, scored(listing(row.link, 1.05, qty=2), qty=2), CFG) is None  # 5%
    cfg = pc.Config("n", "s", threshold_pct=10, min_delta_usd=5)
    assert pc.compare(row, scored(listing(row.link, 1.50, qty=2), qty=2), cfg) is None  # +$1 total


def test_wrong_part_other_currency_and_short_stock_never_count():
    row = pc.parse_row(page(cost=1.00, link=""))
    rs = scored(listing("https://a.com/p", 0.30, title="MC7805 regulator"),
                listing("https://b.com/p", 0.40, currency="EUR"),
                listing("https://c.com/p", 0.50, stock=10))
    assert pc.compare(row, rs, CFG) is None


def test_cheaper_option_is_reported():
    row = pc.parse_row(page(cost=1.00))
    f = pc.compare(row, scored(listing(row.link, 1.30), listing("https://shop.com/p", 0.80)), CFG)
    assert f.cheaper["url"] == "https://shop.com/p"
    text = pc.format_alert(f)
    assert "*Price went up on <https://www.notion.so/p1|5V regulator>*" in text
    assert "$1.30/unit ($65.00)" in text and "*+30%*" in text
    assert "Cheaper option: $0.80/unit at <https://shop.com/p|shop.com>" in text
    assert "shipping cost estimated" not in text


@pytest.mark.parametrize("n, s", [(1234.5, "1,234.50"), (0.8, "0.80"), (0.0425, "0.0425"),
                                  (0.123456, "0.1235")])
def test_money(n, s):
    assert pc.money(n) == s


def test_row_quantity_picks_the_price_break():
    # 38 units: the 15+ break applies, not the qty-1 price or the 50+ one.
    row = pc.parse_row(page(qty=38, cost=1.00))
    link = listing(row.link, 1.86, qty=38,
                   price_breaks=[(1, 1.86), (10, 1.33), (15, 1.18), (50, 1.08)])
    f = pc.compare(row, scored(link, qty=38), CFG)
    assert f.found_unit == 1.18 and f.delta_pct == 18.0
    assert "$1.18/unit (15+ price) ($44.84)" in pc.format_alert(f)


# ---------------------------------------------------------------- identity
DIRECTORY = [("S1", ["Ada Lovelace", "ada"], "ada@upenn.edu"),
             ("S2", ["Alan Turing"], None), ("S3", ["Alan Kay"], None)]


@pytest.mark.parametrize("name, uid", [("Ada Lovelace", "S1"), ("ADA  lovelace", "S1"),
                                       ("Turing", "S2"), ("Alan", None), ("A. Kay", "S3"), ("", None)])
def test_match_by_name(name, uid):
    assert pc.match_by_name(name, DIRECTORY) == uid


def test_slack_user_prefers_email():
    slack = FakeSlack(DIRECTORY)
    assert pc.slack_user(slack, {"id": "u", "name": "Someone Else", "email": "ADA@upenn.edu"}, dict) == "S1"
    assert pc.slack_user(slack, {"id": "u", "name": "Alan Turing", "email": None}, dict) == "S2"


# ---------------------------------------------------------------- run
def checker(notion=None, slack=None, find=None, cfg=CFG, dry_run=False):
    return pc.Checker(cfg, notion or FakeNotion(), slack or FakeSlack(),
                      find or (lambda row: scored(listing(row.link, 2.0))), dry_run)


def test_check_dms_point_of_contact_once():
    row = pc.parse_row(page())
    notion, slack = FakeNotion(), FakeSlack()
    find = lambda row: scored(listing(row.link, 1.40))  # noqa: E731
    assert checker(notion, slack, find).check(row) == "alerted"
    assert notion.created == ["price-alert p1 1 x50"]
    assert slack.posts[0][0] == "S1" and "+40%" in slack.posts[0][1]
    # Checked again: already claimed in the Job Log, so no second DM.
    assert checker(FakeNotion(claimed=notion.created), slack, find).check(row) == "ok"
    assert len(slack.posts) == 1


def test_new_quantity_can_alert_again():
    notion = FakeNotion(claimed=["price-alert p1 1 x50"])
    assert checker(notion).check(pc.parse_row(page(qty=100))) == "alerted"
    assert notion.created == ["price-alert p1 1 x100"]


@pytest.mark.parametrize("kw, reason", [
    ({"parent": "00000000-0000-0000-0000-000000000000"}, "not a Purchasing row"),
    ({"exported": True}, "already ordered"),
    ({"season": "REV11"}, "season REV11"),
    ({"season": ""}, "season empty"),
    ({"cost": 0}, "missing"), ({"part": ""}, "missing"), ({"qty": 0}, "missing")])
def test_rows_that_are_not_checked(kw, reason):
    row = pc.parse_row(page(**kw))
    assert reason in pc.skip_reason(row, CFG)
    searched = []
    assert checker(find=searched.append).check(row) == "skipped" and searched == []


def test_open_purchasing_row_is_checked():
    assert pc.skip_reason(pc.parse_row(page()), CFG) is None
    # Same id written without dashes still matches.
    assert pc.skip_reason(pc.parse_row(page(parent=DEFAULT_PURCHASING_DS.replace("-", ""))), CFG) is None


def test_unmatched_contact_falls_back_to_channel():
    slack = FakeSlack()
    cfg = pc.Config("n", "s", fallback_channel="purchasing")
    checker(slack=slack, cfg=cfg).check(pc.parse_row(page(contacts=(("u9", "Nobody Known", None),))))
    assert [c for c, _ in slack.posts] == ["purchasing"]


def test_dry_run_sends_and_claims_nothing(capsys):
    notion, slack = FakeNotion(), FakeSlack()
    assert checker(notion, slack, dry_run=True).check(pc.parse_row(page())) == "alerted"
    assert slack.posts == [] and notion.created == []
    assert "Price went up" in capsys.readouterr().out


def test_unfindable_part_is_ok_not_retried():
    assert checker(find=lambda row: []).check(pc.parse_row(page())) == "ok"


def test_search_failure_and_llm_outage_mean_retry():
    def boom(row):
        raise RuntimeError("All backends failed")
    assert checker(find=boom).check(pc.parse_row(page())) == "retry"
    outage = lambda row: scored(listing(row.link, None, fetch_error="llm: HTTP 429"))  # noqa: E731
    assert checker(find=outage).check(pc.parse_row(page())) == "retry"


def test_sweep_counts_and_stops_on_repeated_outage():
    calls = []

    def find(row):
        calls.append(row.id)
        if row.id == "p0":
            return scored(listing(row.link, 2.0))
        return scored(listing(row.link, None, fetch_error="llm: HTTP 429"))
    rows = [pc.parse_row(page(f"p{i}")) for i in range(6)]
    assert checker(find=find).check_all(rows) == {"alerted": 1, "retry": 3}
    assert calls == ["p0", "p1", "p2", "p3"]


def test_one_failure_does_not_stop_the_sweep():
    def find(row):
        if row.id == "p0":
            raise RuntimeError("All backends failed")
        return scored(listing(row.link, 1.0))
    rows = [pc.parse_row(page(f"p{i}")) for i in range(3)]
    assert checker(find=find).check_all(rows) == {"retry": 1, "ok": 2}


def test_open_rows_query():
    notion = FakeNotion(rows=[page("a"), page("b")])
    assert [r.id for r in pc.open_rows(notion, CFG, limit=1)] == ["a"]
    filter_, sorts, limit = notion.queries[0]
    assert {"property": "Exported?", "checkbox": {"equals": False}} in filter_["and"]
    assert {"property": "Season", "select": {"equals": "REV12"}} in filter_["and"]
    assert sorts == [{"timestamp": "created_time", "direction": "ascending"}] and limit == 1


# ---------------------------------------------------------------- queue (Job Log)
def test_enqueue_claims_the_queue_key():
    notion = FakeNotion()
    assert pc.enqueue(notion, CFG, "p1") is True
    assert notion.created == ["price-check-queue p1"]
    assert pc.enqueue(FakeNotion(claimed=["price-check-queue p1"]), CFG, "p1") is False


def test_queued_reads_entries_oldest_first():
    notion = FakeNotion(entries=[("e1", "price-check-queue p1"), ("e2", "price-check-queue  p2 "),
                                 ("e3", "price-alert p1 1 x50")])
    assert pc.queued(notion, CFG, limit=10) == [("e1", "p1"), ("e2", "p2")]
    filter_, sorts, limit = notion.queries[0]
    assert filter_ == {"property": "Job", "rich_text": {"equals": "price-check-queue"}}
    assert sorts == [{"property": "Ran at", "direction": "ascending"}] and limit == 10


def test_queue_needs_the_job_log():
    with pytest.raises(SystemExit, match="NOTION_JOB_LOG_DS_ID"):
        pc.queued(FakeNotion(), pc.Config("n", "s", job_log_ds=""))


def test_drain_marks_finished_entries_done():
    notion = FakeNotion(pages={"p1": page("p1"), "p2": page("p2", exported=True)})
    counts = checker(notion).drain([("e1", "p1"), ("e2", "p2"), ("e3", "gone")])
    assert counts == {"alerted": 1, "skipped": 1, "gone": 1}
    assert notion.done == ["e1", "e2", "e3"]


def test_drain_preview_entries_are_not_marked():
    notion = FakeNotion(pages={"p1": page("p1")})
    assert checker(notion).drain([(None, "p1")]) == {"alerted": 1} and notion.done == []


# ---------------------------------------------------------------- discovery
def test_discover_queues_new_complete_rows_once():
    notion = FakeNotion(rows=[page("new1"), page("new2")], claimed=["price-check-queue new2"])
    now = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
    assert pc.discover(notion, CFG, now) == ["new1"]          # new2: PERBot queued it already
    assert notion.created == ["price-check-queue new1"]
    filter_, sorts, limit = notion.queries[0]
    assert {"timestamp": "created_time",
            "created_time": {"on_or_after": "2026-10-08T12:00:00+00:00"}} in filter_["and"]
    assert {"property": "Cat. or model no.", "rich_text": {"is_not_empty": True}} in filter_["and"]
    assert {"property": "Exported?", "checkbox": {"equals": False}} in filter_["and"]
    assert sorts == [{"timestamp": "created_time", "direction": "ascending"}]


def test_discover_dry_run_and_off():
    notion = FakeNotion(rows=[page("new1")])
    assert pc.discover(notion, CFG, dry_run=True) == ["new1"] and notion.created == []
    assert pc.discover(notion, pc.Config("n", "s", lookback_hours=0)) == []


def test_drain_keeps_failures_queued_and_stops_on_outage():
    pages = {f"p{i}": page(f"p{i}") for i in range(5)}
    notion = FakeNotion(pages=pages)
    outage = lambda row: scored(listing(row.link, None, fetch_error="llm: HTTP 429"))  # noqa: E731
    counts = checker(notion, find=outage).drain([(f"e{i}", f"p{i}") for i in range(5)])
    assert counts == {"retry": 3} and notion.done == []


def test_drain_unreadable_page_is_retried():
    notion = FakeNotion(pages={"p1": RuntimeError("HTTP 502"), "p2": page("p2")})
    assert checker(notion).drain([("e1", "p1"), ("e2", "p2")]) == {"retry": 1, "alerted": 1}
    assert notion.done == ["e2"]


def test_drain_dry_run_leaves_the_queue():
    notion = FakeNotion(pages={"p1": page("p1")})
    checker(notion, dry_run=True).drain([("e1", "p1")])
    assert notion.done == [] and notion.created == []


# ---------------------------------------------------------------- config
def test_config_from_env(monkeypatch):
    for k in ("NOTION_TOKEN", "BENBUYS_SEASON", "NOTION_JOB_LOG_DS_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NOTION_API_KEY", "n")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "s")
    monkeypatch.setenv("PRICE_CHECK_THRESHOLD_PCT", "15")
    monkeypatch.setenv("PRICE_CHECK_MIN_DELTA_USD", "2.5")
    cfg = pc.Config.from_env()
    assert (cfg.notion_token, cfg.threshold_pct, cfg.min_delta_usd) == ("n", 15, 2.5)
    assert cfg.season == "REV12" and cfg.job_log_ds == DEFAULT_JOB_LOG_DS
    monkeypatch.setenv("NOTION_JOB_LOG_DS_ID", "")  # set but empty = dedup off, as in PERBot
    assert pc.Config.from_env().job_log_ds == ""


def test_config_rejects_non_numbers(monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "n")
    monkeypatch.setenv("PRICE_CHECK_THRESHOLD_PCT", "ten")
    with pytest.raises(SystemExit, match="PRICE_CHECK_THRESHOLD_PCT must be a number"):
        pc.Config.from_env(need_slack=False)


# ---------------------------------------------------------------- Notion client (HTTP faked)
class HTTPResp:
    def __init__(self, body, status=200, headers=None):
        self._body, self.status_code, self.text = body, status, str(body)
        self.headers = headers or {}

    def json(self):
        return self._body


def notion_http(monkeypatch, *bodies):
    calls, queue = [], list(bodies)

    def request(method, url, headers, json, timeout):
        calls.append((method, url, json, headers))
        body = queue.pop(0)
        return body if isinstance(body, HTTPResp) else HTTPResp(body)
    monkeypatch.setattr(requests, "request", request)
    return calls


def test_notion_query_paginates_and_sends_headers(monkeypatch):
    calls = notion_http(monkeypatch, {"results": [1, 2], "has_more": True, "next_cursor": "c"},
                        {"results": [3], "has_more": False})
    n = pc.Notion("tok", "2026-03-11")
    assert n.query("ds", {"f": 1}, sorts=[{"s": 1}]) == [1, 2, 3]
    (m1, url, body1, headers), (_, _, body2, _) = calls
    assert m1 == "POST" and url.endswith("/data_sources/ds/query")
    assert headers["Authorization"] == "Bearer tok" and headers["Notion-Version"] == "2026-03-11"
    assert body1 == {"page_size": 100, "filter": {"f": 1}, "sorts": [{"s": 1}]}
    assert body2["start_cursor"] == "c"


def test_notion_query_limit_stops_early(monkeypatch):
    calls = notion_http(monkeypatch, {"results": [1, 2, 3], "has_more": True, "next_cursor": "c"})
    assert pc.Notion("t", "v").query("ds", limit=2) == [1, 2] and len(calls) == 1


def test_notion_other_calls(monkeypatch):
    calls = notion_http(monkeypatch,
                        {"properties": {"Name": {"id": "title", "type": "title"}},
                         "parent": {"database_id": "db"}},
                        {"id": "p"}, {"id": "new"}, {"id": "e"},
                        {"results": [{"id": "u1", "type": "person", "person": {"email": "a@x"}},
                                     {"id": "b1", "type": "bot"}], "has_more": False})
    n = pc.Notion("t", "v")
    assert n.data_source("ds")["parent"] == {"database_id": "db"}
    n.page("p")
    n.create("ds", {"Run key": 1})
    n.update("e", {"Job": 1})
    assert n.users() == {"u1": "a@x"}
    assert [(m, u.split("/v1")[1]) for m, u, _, _ in calls] == [
        ("GET", "/data_sources/ds"), ("GET", "/pages/p"), ("POST", "/pages"),
        ("PATCH", "/pages/e"), ("GET", "/users?page_size=100")]
    assert calls[3][2] == {"properties": {"Job": 1}}
    assert calls[2][2]["parent"] == {"type": "data_source_id", "data_source_id": "ds"}


def test_notion_errors_raise(monkeypatch):
    notion_http(monkeypatch, HTTPResp({"message": "API token is invalid."}, status=401))
    with pytest.raises(RuntimeError, match="HTTP 401"):
        pc.Notion("t", "v").page("p")


def test_claim_once_writes_the_job_log_record(monkeypatch):
    calls = notion_http(monkeypatch, {"results": [], "has_more": False}, {"id": "rec"})
    assert pc.claim_once(pc.Notion("t", "v"), CFG, "price-alert p1 1") is True
    query, create = calls[0][2], calls[1][2]
    assert query["filter"] == {"property": "Run key", "title": {"equals": "price-alert p1 1"}}
    assert create["properties"]["Job"]["rich_text"][0]["text"]["content"] == "price-check"
    assert "Ran at" in create["properties"]


def test_claim_once_already_claimed_or_no_job_log(monkeypatch, capsys):
    notion_http(monkeypatch, {"results": [{"id": "rec"}], "has_more": False})
    assert pc.claim_once(pc.Notion("t", "v"), CFG, "k") is False
    no_log = pc.Config("n", "s", job_log_ds="")
    assert pc.claim_once(None, no_log, "k") is True
    assert "cannot dedupe" in capsys.readouterr().err


# ---------------------------------------------------------------- Slack client (HTTP faked)
def slack_http(monkeypatch, *bodies):
    calls, queue = [], list(bodies)

    def fake(url, headers, timeout, json=None, params=None):
        calls.append((url.rsplit("/", 1)[1], json or params, headers))
        return HTTPResp(queue.pop(0))
    monkeypatch.setattr(requests, "post", fake)
    monkeypatch.setattr(requests, "get", fake)
    return calls


def test_slack_post_dm(monkeypatch):
    calls = slack_http(monkeypatch, {"ok": True})
    pc.Slack("xoxb").post("U1", "hi")
    method, body, headers = calls[0]
    assert method == "chat.postMessage" and headers["Authorization"] == "Bearer xoxb"
    assert body == {"channel": "U1", "text": "hi", "unfurl_links": False}


def test_slack_errors_raise(monkeypatch):
    slack_http(monkeypatch, {"ok": False, "error": "channel_not_found"})
    with pytest.raises(RuntimeError, match="channel_not_found"):
        pc.Slack("t").post("C1", "hi")


def test_slack_lookup_email(monkeypatch):
    slack_http(monkeypatch, {"ok": True, "user": {"id": "U9"}}, {"ok": False, "error": "users_not_found"})
    s = pc.Slack("t")
    assert s.lookup_email("a@x") == "U9" and s.lookup_email("b@x") is None


def test_slack_directory_paginates_and_skips_bots(monkeypatch):
    calls = slack_http(monkeypatch, {"ok": True, "members": [
        {"id": "U1", "name": "ada", "profile": {"real_name": "Ada Lovelace", "email": "A@X.EDU"}},
        {"id": "B1", "is_bot": True, "name": "bot"},
        {"id": "U2", "deleted": True, "name": "gone"}],
        "response_metadata": {"next_cursor": "c"}},
        {"ok": True, "members": [{"id": "USLACKBOT", "name": "slackbot"},
                                 {"id": "U3", "name": "alan", "profile": {}}]})
    s = pc.Slack("t")
    assert s.directory() == [("U1", ["Ada Lovelace", "ada"], "a@x.edu"), ("U3", ["alan"], None)]
    assert s.directory() is s.directory() and len(calls) == 2  # cached
    assert calls[1][1]["cursor"] == "c"


def test_slack_post_failure_does_not_stop_the_run(capsys):
    class Down(FakeSlack):
        def post(self, channel, text, blocks=None):
            raise RuntimeError("Slack chat.postMessage: not_in_channel")
    checker(slack=Down()).check(pc.parse_row(page()))
    assert "Slack post to S1 failed" in capsys.readouterr().err


def test_no_target_and_no_channel_logs_the_alert(capsys):
    checker().check(pc.parse_row(page(contacts=())))
    assert "alert not sent" in capsys.readouterr().err


# ---------------------------------------------------------------- remaining edges
def test_notion_users_unavailable_falls_back_to_names(capsys):
    class NoUsers(FakeNotion):
        def users(self):
            raise RuntimeError("HTTP 403 restricted_resource")
    slack = FakeSlack([("S7", ["Ada Lovelace"], None)])
    row = pc.parse_row(page(contacts=(("u1", "Ada Lovelace", None),)))
    assert checker(NoUsers(), slack).check(row) == "alerted"
    assert slack.posts[0][0] == "S7"
    assert "matching by name" in capsys.readouterr().err


def test_mark_done_failure_does_not_stop_the_queue(capsys):
    class ReadOnly(FakeNotion):
        def update(self, page_id, props):
            raise RuntimeError("HTTP 409 conflict")
    notion = ReadOnly(pages={"p1": page("p1"), "p2": page("p2")})
    assert checker(notion).drain([("e1", "p1"), ("e2", "p2")]) == {"alerted": 2}
    assert capsys.readouterr().err.count("couldn't mark queue entry") == 2


def test_alert_lists_flags_but_not_estimated_shipping():
    row = pc.parse_row(page(cost=1.00))
    link = listing(row.link, 1.50, shipping_cost=None)
    f = pc.compare(row, scored(link), CFG)
    f.found["notes"] = ["shipping cost estimated", "search page: lowest listing"]
    text = pc.format_alert(f)
    assert "• Flags: search page: lowest listing" in text and "estimated" not in text


def test_main_exits_without_an_llm(monkeypatch):
    monkeypatch.setattr(check, "load_env", lambda *a: None)
    monkeypatch.setenv("NOTION_TOKEN", "n")
    monkeypatch.setattr(check, "Notion", lambda t, v: FakeNotion(rows=[page()]))

    def no_llm(*a, **k):
        raise llm_scraper.LLMError("no LLM configured")
    monkeypatch.setattr(llm_scraper, "LLM", no_llm)
    monkeypatch.setattr(sys, "argv", ["pricecheck.check", "--all", "--dry-run"])
    with pytest.raises(SystemExit, match="no LLM configured"):
        check.main()


# ---------------------------------------------------------------- rate limits (HTTP 429)
@pytest.fixture
def slept(monkeypatch):
    waits = []
    monkeypatch.setattr(time, "sleep", waits.append)
    return waits


def test_notion_429_waits_and_retries(monkeypatch, slept):
    calls = notion_http(monkeypatch, HTTPResp({}, 429, {"Retry-After": "2"}), {"id": "p"})
    assert pc.Notion("t", "v").page("p") == {"id": "p"}
    assert len(calls) == 2 and slept == [2.0]


def test_slack_429_does_not_lose_the_dm(monkeypatch, slept):
    responses = [HTTPResp({"ok": False, "error": "ratelimited"}, 429, {"Retry-After": "1"}),
                 HTTPResp({"ok": True})]
    sent = []

    def post(url, headers, timeout, json=None, params=None):
        sent.append(json)
        return responses.pop(0)
    monkeypatch.setattr(requests, "post", post)
    pc.Slack("t").post("U1", "hi")
    assert len(sent) == 2 and slept == [1.0]


def test_rate_limit_wait_is_capped_and_defaults(monkeypatch, slept):
    notion_http(monkeypatch, HTTPResp({}, 429, {"Retry-After": "600"}),
                HTTPResp({}, 429, {"Retry-After": "soon"}), {"id": "p"})
    pc.Notion("t", "v").page("p")
    assert slept == [MAX_RETRY_WAIT, 2]   # capped; unreadable -> 2^attempt


def test_rate_limit_gives_up_after_retries(monkeypatch, slept):
    calls = notion_http(monkeypatch, *[HTTPResp({}, 429)] * (RATE_LIMIT_RETRIES + 1))
    with pytest.raises(RuntimeError, match="HTTP 429"):
        pc.Notion("t", "v").page("p")
    assert len(calls) == RATE_LIMIT_RETRIES + 1 and slept == [1, 2, 4]


# ---------------------------------------------------------------- buttons (PERBot handles clicks)
def _values(block):
    return [(b["action_id"], json.loads(b["value"])) for b in block["elements"]]


def test_buttons_use_found_price_at_own_link_and_keep():
    row = pc.parse_row(page(cost=1.00))
    f = pc.compare(row, scored(listing(row.link, 0.80)), CFG)
    block = pc.alert_buttons(f)
    assert block["block_id"] == "price_swap_actions"
    (use_id, use), (keep_id, keep) = _values(block)
    assert use_id == "price_swap_use:0" and keep_id == "price_swap_keep"
    # Same listing as the row's link: price only, no link switch.
    assert use == {"v": 1, "r": "p1", "q": 50, "o": 1.0, "n": 0.8, "s": "www.digikey.com"}
    assert keep == {"v": 1, "r": "p1", "q": 50, "o": 1.0}
    labels = [b["text"]["text"] for b in block["elements"]]
    assert labels == ["Use $0.80/unit · www.digikey.com", "Keep $1.00/unit"]
    assert block["elements"][0]["style"] == "primary"


def test_cheaper_option_button_switches_the_link():
    row = pc.parse_row(page(cost=1.00))
    f = pc.compare(row, scored(listing(row.link, 1.30), listing("https://shop.com/p", 0.80)), CFG)
    (a, found), (b, cheaper), (_, _) = _values(pc.alert_buttons(f))
    assert (a, b) == ("price_swap_use:0", "price_swap_use:1")
    assert found["n"] == 1.3 and "l" not in found
    assert cheaper["n"] == 0.8 and cheaper["l"] == "https://shop.com/p"
    assert cheaper["ol"] == "https://www.digikey.com/x"
    els = pc.alert_buttons(f)["elements"]
    assert "style" not in els[0] and els[1]["style"] == "primary"


def test_flagged_cheaper_option_gets_no_button():
    row = pc.parse_row(page(cost=1.00))
    f = pc.compare(row, scored(listing(row.link, 1.30), listing("https://shop.com/p", 0.80)), CFG)
    f.cheaper["notes"] = ["search page: lowest listing"]
    assert [i for i, _ in _values(pc.alert_buttons(f))] == ["price_swap_use:0", "price_swap_keep"]


def test_best_option_elsewhere_switches_link_even_from_empty():
    row = pc.parse_row(page(cost=1.00, link=None))
    f = pc.compare(row, scored(listing("https://shop.com/p", 1.50)), CFG)
    use = _values(pc.alert_buttons(f))[0][1]
    assert use["l"] == "https://shop.com/p" and use["ol"] == ""


def test_pack_row_buttons_carry_per_pack_prices():
    row = pc.parse_row(page(qty=5, cost=10.00, notes="Sold in packs of 10"))
    f = pc.compare(row, scored(listing(row.link, 0.80, qty=50)), CFG)
    (_, use), (_, keep) = _values(pc.alert_buttons(f))
    assert use["n"] == 8.0 and use["o"] == 10.0 and use["q"] == 5 and keep["o"] == 10.0
    assert pc.alert_buttons(f)["elements"][1]["text"]["text"] == "Keep $1.00/unit"


def test_sub_dollar_prices_round_like_money():
    row = pc.parse_row(page(cost=0.10))
    f = pc.compare(row, scored(listing(row.link, 0.0425123)), CFG)
    assert _values(pc.alert_buttons(f))[0][1]["n"] == 0.0425


def test_oversized_or_non_http_link_drops_that_button():
    row = pc.parse_row(page(cost=1.00))
    f = pc.compare(row, scored(listing("https://shop.com/" + "x" * 2100, 1.50)), CFG)
    assert pc.alert_buttons(f) is None
    # Alert still sent, without Use / Keep (the listing can still be opened).
    assert [b.get("block_id") for b in pc.alert_blocks(f)[1:]] == ["price_swap_links"]
    row = pc.parse_row(page(cost=1.00, link="digikey part 123"))
    f = pc.compare(row, scored(listing("https://shop.com/p", 1.50)), CFG)
    assert pc.alert_buttons(f) is None


def test_alert_dm_carries_the_buttons(capsys):
    slack = FakeSlack()
    checker(slack=slack).check(pc.parse_row(page()))
    section, links, actions = slack.blocks[0]
    assert section["text"]["text"] == slack.posts[0][1]
    assert links["block_id"] == "price_swap_links" and actions["block_id"] == "price_swap_actions"


def test_dry_run_prints_the_buttons(capsys):
    checker(dry_run=True).check(pc.parse_row(page()))
    assert ("[View www.digikey.com (your link) ↗ | Use $2.00/unit · www.digikey.com | "
            "Keep $1.00/unit]") in capsys.readouterr().out


def test_slack_post_with_blocks(monkeypatch):
    calls = slack_http(monkeypatch, {"ok": True})
    pc.Slack("xoxb").post("U1", "hi", [{"type": "divider"}])
    assert calls[0][1] == {"channel": "U1", "text": "hi", "unfurl_links": False,
                           "blocks": [{"type": "divider"}]}


def test_view_links_for_every_option_even_flagged():
    row = pc.parse_row(page(cost=1.00))
    f = pc.compare(row, scored(listing(row.link, 1.30), listing("https://shop.com/p", 0.80)), CFG)
    f.cheaper["notes"] = ["search page: lowest listing"]
    links = pc.alert_links(f)
    assert links["block_id"] == "price_swap_links"
    assert [(b["action_id"], b["url"], b["text"]["text"]) for b in links["elements"]] == [
        ("price_swap_view:0", "https://www.digikey.com/x", "View www.digikey.com (your link) ↗"),
        ("price_swap_view:1", "https://shop.com/p", "View shop.com ↗")]
    assert all("value" not in b for b in links["elements"])
    # Links sit above Use / Keep, so they stay when PERBot swaps the buttons for the outcome.
    assert [b.get("block_id") for b in pc.alert_blocks(f)] == [None, "price_swap_links", "price_swap_actions"]


def test_use_asks_for_a_check_first():
    row = pc.parse_row(page(cost=1.00))
    f = pc.compare(row, scored(listing(row.link, 1.30), listing("https://shop.com/p", 0.80)), CFG)
    found, cheaper, keep = pc.alert_buttons(f)["elements"]
    assert found["confirm"]["text"]["text"] == (
        "Set Unit cost to $1.30 (www.digikey.com)? Open the listing first and check it's the "
        "right part, in stock, at this price.")
    assert "switch the product link to it" in cheaper["confirm"]["text"]["text"]
    assert found["confirm"]["confirm"]["text"] == "Update Unit cost" and "confirm" not in keep
    for b in (found, cheaper):
        c = b["confirm"]
        assert len(c["title"]["text"]) <= 100 and len(c["text"]["text"]) <= 300
        assert len(c["confirm"]["text"]) <= 30 and len(c["deny"]["text"]) <= 30


# ---------------------------------------------------------------- savings ledger
def test_alert_is_logged_to_the_ledger_once_sent():
    notion, slack = FakeNotion(), FakeSlack()
    row = pc.parse_row(page(cost=1.00))
    find = lambda r: scored(listing(r.link, 1.30), listing("https://shop.com/p", 0.80))  # noqa: E731
    assert checker(notion, slack, find).check(row) == "alerted"
    (props,) = notion.ledger
    assert props["Outcome"]["select"]["name"] == "Pending"
    assert props["Name"]["title"][0]["text"]["content"] == "5V regulator ×50"
    assert props["Row ID"]["rich_text"][0]["text"]["content"] == "p1"
    assert (props["Old unit cost"]["number"], props["Quant."]["number"]) == (1.0, 50)
    # The offered price is the alert's primary (cheapest) option, with its listing.
    assert props["Offered unit cost"]["number"] == 0.8
    assert props["Potential $"]["number"] == 10.0          # (1.00 - 0.80) x 50
    assert props["Direction"]["select"]["name"] == "Drop"
    assert props["Supplier"]["rich_text"][0]["text"]["content"] == "shop.com"
    assert props["Listing"]["url"] == "https://shop.com/p"
    assert props["Purchasing row"]["url"] == "https://www.notion.so/p1"
    assert props["Season"]["select"]["name"] == "REV12"
    assert "Alert sent" in props


def test_pack_row_ledger_is_in_notion_units():
    row = pc.parse_row(page(qty=5, cost=10.00, notes="Sold in packs of 10"))
    f = pc.compare(row, scored(listing(row.link, 0.80, qty=50)), CFG)
    props = pc.ledger_props(f)
    assert props["Offered unit cost"]["number"] == 8.0      # per pack, like Unit cost
    assert props["Potential $"]["number"] == 10.0           # (10 - 8) x 5 packs
    assert "Listing" in props and props["Direction"]["select"]["name"] == "Drop"


def test_price_rise_is_logged_as_a_rise():
    row = pc.parse_row(page(cost=1.00))
    props = pc.ledger_props(pc.compare(row, scored(listing(row.link, 1.40)), CFG))
    assert props["Direction"]["select"]["name"] == "Rise"
    assert props["Potential $"]["number"] == -20.0
    assert "Listing" in props  # the row's own link


def test_nothing_logged_when_not_sent_or_ledger_off(capsys):
    class Down(FakeSlack):
        def post(self, channel, text, blocks=None):
            raise RuntimeError("Slack chat.postMessage: not_in_channel")
    notion = FakeNotion()
    checker(notion, Down()).check(pc.parse_row(page()))
    assert notion.ledger == []
    notion = FakeNotion()
    checker(notion, dry_run=True).check(pc.parse_row(page()))
    assert notion.ledger == []
    notion = FakeNotion()
    checker(notion, cfg=pc.Config("n", "s", threshold_pct=10, ledger_ds="")).check(pc.parse_row(page()))
    assert notion.ledger == []


def test_ledger_failure_does_not_stop_the_alert(capsys):
    class NoLedger(FakeNotion):
        def create(self, ds, props):
            if "Run key" not in props:
                raise RuntimeError("HTTP 404 object_not_found")
            super().create(ds, props)
    slack = FakeSlack()
    assert checker(NoLedger(), slack).check(pc.parse_row(page())) == "alerted"
    assert slack.posts and "couldn't log the alert" in capsys.readouterr().err


def test_config_ledger_env(monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "n")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "s")
    assert pc.Config.from_env().ledger_ds == "dd1818e3-82cd-461b-8071-12af3b0f1341"
    monkeypatch.setenv("PRICE_CHECK_LEDGER_DS_ID", "")
    assert pc.Config.from_env().ledger_ds == ""

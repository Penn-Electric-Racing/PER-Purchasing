"""Offline tests for price_listener.py: settling, rate limit, webhook routing, the worker
(queueing into the Job Log and draining it), and the HTTP endpoint on a real local server.
Notion, Slack and the price check itself are faked.

    pip install requests beautifulsoup4 pytest
    pytest
"""
import hashlib
import hmac
import json
import os
import sys
import threading
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import price_check as pc  # noqa: E402
import price_listener as pl  # noqa: E402

CFG = pc.Config(notion_token="n", slack_token="s")
DS = pc.DEFAULT_PURCHASING_DS


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def event(kind="page.created", pid="p1", parent=None, updated=None):
    data = {"parent": parent if parent is not None else {"id": "db1", "type": "database",
                                                           "data_source_id": DS}}
    if updated is not None:
        data["updated_properties"] = updated
    return {"type": kind, "entity": {"id": pid, "type": "page"}, "data": data}


def open_page(pid="p1", part="LM7805", **props):
    return {"id": pid, "url": "", "parent": {"data_source_id": DS}, "properties": {
        "Name": {"type": "title", "title": [{"plain_text": "reg"}]},
        "Cat. or model no.": {"rich_text": [{"plain_text": part}] if part else []},
        "Quant.": {"number": 10}, "Unit cost": {"number": 1.0},
        "Season": {"select": {"name": "REV12"}}, "Exported?": {"checkbox": False}, **props}}


# ---------------------------------------------------------------- signature
def test_signature_is_hmac_of_the_raw_body():
    body = b'{"type": "page.created"}'
    good = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert pl.valid_signature("secret", body, good)
    assert not pl.valid_signature("secret", body + b" ", good)
    assert not pl.valid_signature("other", body, good)
    assert not pl.valid_signature("", body, good) and not pl.valid_signature("secret", body, None)


# ---------------------------------------------------------------- settling + limit
def test_settling_waits_for_the_last_event():
    clock = Clock()
    s = pl.Settling(clock)
    s.touch("p1", 60)
    clock.t += 50
    s.touch("p1", 60)          # edited again: wait 60 s from now
    clock.t += 30
    assert s.pop_due() == []
    clock.t += 30
    assert s.pop_due() == ["p1"] and len(s) == 0


def test_settling_pops_earliest_first():
    clock = Clock()
    s = pl.Settling(clock)
    s.touch("late", 30)
    s.touch("early", 10)
    clock.t += 100
    assert s.pop_due() == ["early", "late"]


def test_hourly_limit_rolls():
    clock = Clock()
    limit = pl.HourlyLimit(2, clock=clock)
    limit.record()
    clock.t += 10
    limit.record()
    assert limit.wait() == 3590
    clock.t += 3590
    assert limit.wait() == 0


# ---------------------------------------------------------------- routing
@pytest.fixture
def router():
    return pl.Router(CFG, pl.Settling(Clock()), 60, ["db1"], relevant_props={"qty:", "title"})


def test_created_row_starts_settling(router):
    assert "looking at it in 60s" in router.handle(event())
    assert len(router.settling) == 1


def test_parent_matched_by_database_or_data_source_id(router):
    assert "looking" in router.handle(event(parent={"id": "db1", "type": "database"}))
    assert "looking" in router.handle(event(pid="p2", parent={"data_source_id": DS.replace("-", "")}))
    assert "looking" in router.handle(event(pid="p3", parent={}))  # unknown: checked on read


def test_other_databases_are_ignored(router):
    assert "another database" in router.handle(event(parent={"id": "job-log", "type": "database"}))
    assert len(router.settling) == 0


def test_only_price_relevant_edits_count(router):
    assert "no price-relevant" in router.handle(event("page.properties_updated", updated=["zzz"]))
    assert "looking" in router.handle(event("page.properties_updated", updated=["qty%3A"]))
    assert "looking" in router.handle(event("page.properties_updated", pid="p2", updated=[]))


def test_non_page_and_other_events_are_ignored(router):
    assert "not a page" in router.handle({"type": "database.created", "entity": {"type": "database"}})
    assert "ignored page.deleted" in router.handle(event("page.deleted"))
    assert len(router.settling) == 0


def test_relevant_property_ids():
    schema = {"Quant.": {"id": "qty%3A"}, "Unit cost": {"id": "uc"}, "Vendor": {"id": "v"}}
    assert pl.relevant_property_ids(schema) == {"qty:", "uc"}


# ---------------------------------------------------------------- worker
class FakeNotion:
    """Purchasing pages, Job Log claims and queue entries."""

    def __init__(self, pages=None, queue=(), claimed=()):
        self.pages, self.queue, self.claimed = pages or {}, list(queue), set(claimed)
        self.created = []

    def page(self, pid):
        if pid not in self.pages:
            raise RuntimeError("HTTP 404")
        return self.pages[pid]

    def query(self, ds, filter_=None, page_size=100, sorts=None, limit=None):
        if "title" in filter_:
            return [{"id": "x"}] if filter_["title"]["equals"] in self.claimed else []
        return [{"id": e, "properties": {"Run key": {"title": [{"plain_text": f"price-check-queue {p}"}]}}}
                for e, p in self.queue][:limit]

    def create(self, ds, props):
        key = props["Run key"]["title"][0]["text"]["content"]
        self.created.append(key)
        self.claimed.add(key)


class FakeChecker:
    def __init__(self, notion, status="ok"):
        self.notion, self.status, self.drained = notion, status, []

    def drain(self, entries):
        self.drained += entries
        if self.status != "retry":  # a real drain marks the entry done: it leaves the queue
            self.notion.queue = [e for e in self.notion.queue if e not in entries]
        return {self.status: 1}


def worker(pages=None, queue=(), status="ok", per_hour=20, dry_run=False, claimed=()):
    clock = Clock()
    notion = FakeNotion(pages or {"p1": open_page()}, queue, claimed)
    settling = pl.Settling(clock)
    w = pl.Worker(CFG, notion, FakeChecker(notion, status), settling,
                  pl.HourlyLimit(per_hour, clock=clock), clock=clock, dry_run=dry_run)
    return w, notion, settling, clock


def test_settled_complete_row_is_queued_once():
    w, notion, settling, _ = worker()
    settling.touch("p1", 0)
    assert w.queue_settled() == 1 and notion.created == ["price-check-queue p1"]
    settling.touch("p1", 0)                       # edited again later
    assert w.queue_settled() == 0 and len(notion.created) == 1


def test_incomplete_or_closed_rows_are_not_queued(capsys):
    pages = {"a": open_page("a", part=""), "b": open_page("b", **{"Exported?": {"checkbox": True}})}
    w, notion, settling, _ = worker(pages)
    for pid in ("a", "b", "gone"):
        settling.touch(pid, 0)
    assert w.queue_settled() == 0 and notion.created == []
    err = capsys.readouterr().err
    assert "part number, Quant. or Unit cost missing" in err and "already ordered" in err
    assert "couldn't read the page" in err


def test_worker_checks_one_queued_row_per_step():
    w, notion, _, _ = worker(queue=[("e1", "p1"), ("e2", "p2")])
    assert w.check_next() == "ok" and w.checker.drained == [("e1", "p1")]
    assert w.check_next() == "ok" and w.checker.drained[-1] == ("e2", "p2")


def test_empty_queue_is_polled_every_few_minutes():
    w, notion, settling, clock = worker()
    assert w.check_next() is None and w.next_poll == clock.t + pl.POLL_SECONDS
    notion.queue = [("e1", "p1")]                 # PERBot queued something
    assert w.check_next() is None                 # not looked at until the next poll...
    settling.touch("p1", 0)
    w.queue_settled()                             # ...unless the listener queued a row itself
    assert w.check_next() == "ok"


def test_hourly_limit_and_skips():
    w, notion, _, clock = worker(queue=[("e1", "p1"), ("e2", "p2")], per_hour=1)
    w.check_next()
    assert w.check_next() is None                 # limit reached
    clock.t += 3600
    assert w.check_next() == "ok"
    w2, _, _, _ = worker(queue=[("e1", "p1"), ("e2", "p2")], per_hour=1, status="skipped")
    w2.check_next()
    assert w2.limit.wait() == 0                   # skipped rows don't use the limit


def test_failures_back_off_and_stay_queued(capsys):
    w, notion, _, clock = worker(queue=[("e1", "p1")], status="retry")
    assert w.check_next() == "retry" and w.backoff_until == clock.t + 600
    assert w.check_next() is None                 # backing off
    clock.t = w.backoff_until
    w.check_next()
    assert w.backoff_until == clock.t + 1200 and len(notion.queue) == 1
    w.failures = 50
    clock.t = w.backoff_until
    w.check_next()
    assert w.backoff_until == clock.t + pl.RETRY_MAX_SECONDS
    w.checker.status = "ok"
    clock.t = w.backoff_until
    assert w.check_next() == "ok" and w.failures == 0


def test_dry_run_previews_without_writing_or_looping():
    w, notion, settling, _ = worker(queue=[("e1", "p0")], dry_run=True)
    settling.touch("p1", 0)
    assert w.queue_settled() == 1 and notion.created == [] and w.preview == ["p1"]
    assert w.check_next() == "ok" and w.checker.drained == [("e1", "p0")]
    notion.queue = [("e1", "p0")]                 # dry runs never mark entries done...
    assert w.check_next() == "ok" and w.checker.drained[-1] == (None, "p1")   # ...but move on
    w.next_poll = 0
    assert w.check_next() is None


def test_run_loop_stops():
    w, _, _, _ = worker()
    sleeps = []

    def sleep(t):
        sleeps.append(t)
        w.stopped.set()
    w.sleep = sleep
    w.run()
    assert sleeps == [2]


# ---------------------------------------------------------------- HTTP endpoint
@pytest.fixture
def server():
    settling = pl.Settling(Clock())
    router = pl.Router(CFG, settling, 60, ["db1"])
    httpd = pl.ThreadingHTTPServer(("127.0.0.1", 0), pl.make_handler(
        router, "secret", lambda: {"settling": len(settling)}))
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", settling
    httpd.shutdown()
    httpd.server_close()


def http(url, body=None, headers=None):
    data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def signed(payload, secret="secret"):
    body = json.dumps(payload).encode()
    return body, {"X-Notion-Signature": "sha256=" + hmac.new(secret.encode(), body,
                                                             hashlib.sha256).hexdigest()}


def test_healthz(server):
    url, settling = server
    settling.touch("p1", 60)
    assert http(url + "/healthz") == (200, {"ok": True, "settling": 1})
    assert http(url + "/nope")[0] == 404


def test_verification_handshake_logs_the_token(server, capsys):
    url, _ = server
    assert http(url + "/notion/webhook", {"verification_token": "secret_abc"}) == (200, {"ok": True})
    assert "secret_abc" in capsys.readouterr().err


def test_signed_event_is_accepted(server):
    url, settling = server
    body, headers = signed(event())
    assert http(url + "/notion/webhook", body, headers) == (200, {"ok": True})
    assert len(settling) == 1


def test_unsigned_or_forged_events_are_rejected(server):
    url, settling = server
    assert http(url + "/notion/webhook", event())[0] == 401
    body, headers = signed(event(), secret="wrong")
    assert http(url + "/notion/webhook", body, headers)[0] == 401
    assert len(settling) == 0


def test_bad_requests(server):
    url, _ = server
    assert http(url + "/notion/webhook", b"{not json")[0] == 400
    assert http(url + "/other", {"a": 1})[0] == 404


# ---------------------------------------------------------------- main wiring
def test_main_catches_up_then_serves(monkeypatch, capsys):
    monkeypatch.setattr(pl, "load_env", lambda *a: None)
    monkeypatch.setenv("NOTION_TOKEN", "n")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "s")
    monkeypatch.setenv("PORT", "0")
    monkeypatch.delenv("NOTION_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("NOTION_JOB_LOG_DS_ID", raising=False)

    class LLM:
        provider, model = "fake", "m"

        def __init__(self, *a, **k):
            pass

    class Notion:
        def __init__(self, token, version):
            pass

        def data_source(self, ds):
            return {"parent": {"database_id": "db1"}, "properties": {"Quant.": {"id": "q"}}}

    started, served = [], []
    monkeypatch.setattr(pl.scraper2, "LLM", LLM)
    monkeypatch.setattr(pl.pc, "Notion", Notion)
    monkeypatch.setattr(pl.pc, "discover", lambda notion, cfg, dry_run=False: ["old1", "old2"])
    monkeypatch.setattr(pl.threading, "Thread", lambda target, daemon: type(
        "T", (), {"start": lambda self: started.append(target)})())

    class Server:
        def __init__(self, addr, handler):
            served.append(addr)

        def serve_forever(self):
            pass
    monkeypatch.setattr(pl, "ThreadingHTTPServer", Server)
    monkeypatch.setattr(sys, "argv", ["price_listener.py"])
    pl.main()
    err = capsys.readouterr().err
    assert served == [("", 0)] and len(started) == 1
    assert "startup: 2 row(s) from the last 168 h queued" in err
    assert "NOTION_WEBHOOK_SECRET not set" in err and "max 20/hour" in err


def test_main_needs_the_job_log(monkeypatch):
    monkeypatch.setattr(pl, "load_env", lambda *a: None)
    monkeypatch.setenv("NOTION_TOKEN", "n")
    monkeypatch.setenv("NOTION_JOB_LOG_DS_ID", "")
    monkeypatch.setattr(sys, "argv", ["price_listener.py", "--dry-run"])
    with pytest.raises(SystemExit, match="Job Log"):
        pl.main()


# ---------------------------------------------------------------- end to end (all faked)
class E2ENotion(FakeNotion):
    """FakeNotion whose queue is fed by enqueue (claims) and emptied by mark_done."""

    def query(self, ds, filter_=None, page_size=100, sorts=None, limit=None):
        if "title" in filter_:
            return [{"id": "x"}] if filter_["title"]["equals"] in self.claimed else []
        open_keys = [k for k in self.created if k.startswith("price-check-queue ")
                     and k not in self.done_keys]
        return [{"id": k, "properties": {"Run key": {"title": [{"plain_text": k}]}}}
                for k in open_keys][:limit]

    def update(self, entry_id, props):
        self.done_keys.add(entry_id)

    def users(self):
        return {}


class E2ESlack:
    def __init__(self):
        self.posts = []

    def directory(self):
        return [("S1", ["Ada"], "ada@upenn.edu")]

    def lookup_email(self, email):
        return None

    def post(self, channel, text):
        self.posts.append((channel, text))


def pipeline(pages, per_hour=20, price=2.0):
    """Router + Worker + a real price_check.Checker over fake Notion / Slack / search."""
    import scraper
    clock = Clock()
    notion = E2ENotion(pages)
    notion.done_keys = set()
    slack = E2ESlack()
    searched = []

    def find(row):
        searched.append(row.id)
        r = {"title": "LM7805", "url": f"https://shop.com/{row.id}", "snippet": "", "search_rank": 0,
             **scraper._blank_info(), "cost": price, "currency": "USD", "cost_source": "llm",
             "supplier": "shop.com", "in_stock": True, "shipping_cost": "Free",
             "shipping_time": "2 days"}
        rs = [scraper.apply_qty(r, row.units)]
        pc.Scorer().score_all(rs, row.part_number, row.units)
        return rs
    checker = pc.Checker(CFG, notion, slack, find)
    settling = pl.Settling(clock)
    router = pl.Router(CFG, settling, 60, ["db1"])
    w = pl.Worker(CFG, notion, checker, settling, pl.HourlyLimit(per_hour, clock=clock), clock=clock)
    return router, w, notion, slack, searched, clock


def with_contact(page_):
    page_["properties"]["Point of contact"] = {"people": [
        {"id": "u1", "name": "Ada", "person": {"email": "ada@upenn.edu"}}]}
    return page_


def run_until_idle(w, clock, max_steps=1000):
    for _ in range(max_steps):
        if w.step() is None:
            return


def test_webhook_to_slack_dm():
    router, w, notion, slack, searched, clock = pipeline({"p1": with_contact(open_page("p1"))})
    router.handle(event(pid="p1"))
    run_until_idle(w, clock)
    assert searched == []                       # still settling
    clock.t += 60
    run_until_idle(w, clock)
    assert searched == ["p1"]
    assert notion.created == ["price-check-queue p1", "price-alert p1 1 x10"]
    assert slack.posts[0][0] == "S1" and "+100%" in slack.posts[0][1]
    assert notion.done_keys == {"price-check-queue p1"}


def test_many_events_for_one_row_mean_one_check():
    router, w, notion, slack, searched, clock = pipeline({"p1": open_page("p1")})
    for _ in range(10):                         # created, then edited field by field
        router.handle(event("page.properties_updated", pid="p1", updated=[]))
        clock.t += 5
    clock.t += 60
    run_until_idle(w, clock)
    router.handle(event("page.properties_updated", pid="p1", updated=[]))   # edited after the check
    clock.t += 60
    run_until_idle(w, clock)
    assert searched == ["p1"] and notion.created.count("price-check-queue p1") == 1


def test_burst_is_rate_limited_and_drains_over_time():
    pages = {f"p{i}": open_page(f"p{i}") for i in range(100)}
    router, w, notion, slack, searched, clock = pipeline(pages, per_hour=20)
    for pid in pages:
        router.handle(event(pid=pid))
    clock.t += 60
    run_until_idle(w, clock)
    assert len(searched) == 20                  # the first hour's worth
    assert len([k for k in notion.created if k.startswith("price-check-queue")]) == 100  # all queued
    for hour in range(4):
        clock.t += 3600
        run_until_idle(w, clock)
    assert sorted(searched) == sorted(pages)    # oldest first, every row exactly once


def test_row_completed_later_is_checked_when_complete():
    pages = {"p1": open_page("p1", part="")}
    router, w, notion, slack, searched, clock = pipeline(pages)
    router.handle(event(pid="p1"))              # created empty
    clock.t += 60
    run_until_idle(w, clock)
    assert notion.created == [] and searched == []
    pages["p1"] = open_page("p1")               # part number filled in
    router.handle(event("page.properties_updated", pid="p1", updated=[]))
    clock.t += 60
    run_until_idle(w, clock)
    assert searched == ["p1"]


# ---------------------------------------------------------------- remaining edges
def test_oversized_request_is_refused_before_reading_it(server):
    import http.client
    url, settling = server
    conn = http.client.HTTPConnection(url.split("//")[1], timeout=5)
    conn.putrequest("POST", "/notion/webhook")
    conn.putheader("Content-Length", str(pl.MAX_BODY_BYTES + 1))
    conn.endheaders()                           # headers only: the server must not wait for 1 MB
    resp = conn.getresponse()
    assert resp.status == 413 and json.loads(resp.read()) == {"error": "too large"}
    conn.close()
    assert len(settling) == 0


def _main_env(monkeypatch):
    monkeypatch.setattr(pl, "load_env", lambda *a: None)
    monkeypatch.setenv("NOTION_TOKEN", "n")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "s")
    monkeypatch.delenv("NOTION_JOB_LOG_DS_ID", raising=False)


def test_main_exits_without_an_llm(monkeypatch):
    _main_env(monkeypatch)

    def no_llm(*a, **k):
        raise pl.scraper2.LLMError("no LLM configured")
    monkeypatch.setattr(pl.scraper2, "LLM", no_llm)
    monkeypatch.setattr(sys, "argv", ["price_listener.py"])
    with pytest.raises(SystemExit, match="no LLM configured"):
        pl.main()


def test_main_dry_run_previews_startup_rows(monkeypatch, capsys):
    _main_env(monkeypatch)
    monkeypatch.setenv("PORT", "0")
    workers = []

    class LLM:
        provider, model = "fake", "m"

        def __init__(self, *a, **k):
            pass

    class Notion:
        def __init__(self, token, version):
            pass

        def data_source(self, ds):
            return {"parent": {}, "properties": {}}

    calls = []
    monkeypatch.setattr(pl.scraper2, "LLM", LLM)
    monkeypatch.setattr(pl.pc, "Notion", Notion)
    monkeypatch.setattr(pl.pc, "discover", lambda notion, cfg, dry_run=False: calls.append(dry_run) or ["old1"])
    monkeypatch.setattr(pl.threading, "Thread", lambda target, daemon: workers.append(target.__self__) or
                        type("T", (), {"start": lambda self: None})())
    monkeypatch.setattr(pl, "ThreadingHTTPServer", lambda addr, handler: type(
        "S", (), {"serve_forever": lambda self: None})())
    monkeypatch.setattr(sys, "argv", ["price_listener.py", "--dry-run"])
    pl.main()
    assert calls == [True] and workers[0].preview == ["old1"] and workers[0].dry_run
    assert "1 row(s) from the last 168 h would be queued" in capsys.readouterr().err

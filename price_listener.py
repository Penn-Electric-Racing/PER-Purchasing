#!/usr/bin/env python3
"""
price_listener.py - price-check Purchasing rows as they're added or edited (Notion webhooks).

Notion calls POST /notion/webhook when a page is created or its properties change, however
the row was added (PERBot's /parts, typed into Notion, imported). For the Purchasing data
source:

  1. Settle: the row is read PRICE_LISTENER_SETTLE_SECONDS (default 60) after its LAST
     event, so a row typed in by hand (created empty, filled in field by field) is looked
     at once, complete. Only edits to price-relevant columns count.
  2. Queue: if it's open and complete (price_check.skip_reason) it's queued in PERBot's Job
     Log as "price-check-queue <page id>", the same entry PERBot's /parts writes, so a row
     is queued once however many events or apps report it.
  3. Check: one worker drains the queue one row at a time, at most
     PRICE_LISTENER_MAX_PER_HOUR (default 20) rows an hour, with the LLM paced per minute
     on top, and marks entries done (price_check.Checker). A burst of 100 new rows waits
     its turn. If search or the LLM is failing, the row stays queued and the worker backs
     off 10 min, 20 min, ... up to 6 h.

The queue lives in Notion, so a restart loses nothing that was queued. On startup the
listener also queues rows created in the last PRICE_CHECK_LOOKBACK_HOURS that were never
queued (price_check.discover), which covers events missed while it was down.

Setup (once):
  1. Deploy with a public HTTPS URL (render.yaml: a Render web service).
  2. In the Notion integration's settings -> Webhooks, add a subscription to
     https://<host>/notion/webhook with the events page.created and
     page.properties_updated. Use the same integration as NOTION_TOKEN (PERBot's).
  3. Notion sends a verification token, which this server logs. Paste it into Notion to
     verify, and set it as NOTION_WEBHOOK_SECRET (checks every event's X-Notion-Signature).

Environment: everything price_check.py uses, plus
  NOTION_WEBHOOK_SECRET          the subscription's verification token
  PORT                           default 8080 (Render sets it)
  PRICE_LISTENER_SETTLE_SECONDS  default 60
  PRICE_LISTENER_MAX_PER_HOUR    default 20

Usage:
    python price_listener.py            # serve
    python price_listener.py --dry-run  # log alerts instead of DMing; nothing written
"""
import argparse
import hashlib
import hmac
import json
import os
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

import price_check as pc
import scraper2
from scraper import load_env

EVENTS = {"page.created", "page.properties_updated", "page.undeleted"}
# Columns whose edits can change the check (other edits are ignored).
PRICE_COLUMNS = (pc.COL_PART, pc.COL_QTY, pc.COL_UNIT_COST, pc.COL_LINK, pc.COL_SEASON,
                 pc.COL_EXPORTED, pc.COL_NOTES)
POLL_SECONDS = 300            # look at the queue at least this often (PERBot adds to it too)
RETRY_BASE_SECONDS = 600      # back off 10 min, doubling...
RETRY_MAX_SECONDS = 6 * 3600  # ...up to 6 h
MAX_BODY_BYTES = 1_000_000


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- settle + rate limit
class Settling:
    """Pages waiting for their edits to settle: {page id: due time}. Thread-safe."""

    def __init__(self, clock=time.time):
        self.clock, self.lock, self.due = clock, threading.Lock(), {}

    def touch(self, page_id, delay):
        """(Re)start the wait for a page: `delay` seconds after its latest event."""
        with self.lock:
            self.due[page_id] = self.clock() + delay

    def pop_due(self):
        """Pages whose wait is over, earliest first."""
        with self.lock:
            now = self.clock()
            ready = sorted((t, pid) for pid, t in self.due.items() if t <= now)
            for _, pid in ready:
                del self.due[pid]
            return [pid for _, pid in ready]

    def __len__(self):
        with self.lock:
            return len(self.due)


class HourlyLimit:
    """At most `per_hour` starts in any rolling hour."""

    def __init__(self, per_hour, clock=time.time):
        self.per_hour, self.clock, self.starts = per_hour, clock, deque()

    def wait(self):
        """Seconds until another start is allowed (0 = now)."""
        now = self.clock()
        while self.starts and self.starts[0] <= now - 3600:
            self.starts.popleft()
        return 0 if len(self.starts) < self.per_hour else self.starts[0] + 3600 - now

    def record(self):
        self.starts.append(self.clock())


# ---------------------------------------------------------------- webhook events
def valid_signature(secret, body, header):
    """Notion's X-Notion-Signature: "sha256=" + HMAC-SHA256(verification token, raw body)."""
    if not (secret and header):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def _norm_id(s):
    return (s or "").replace("-", "").lower()


def relevant_property_ids(schema):
    """Property ids (decoded) of PRICE_COLUMNS, from the data source's properties."""
    return {unquote(p["id"]) for name, p in schema.items() if name in PRICE_COLUMNS and p.get("id")}


class Router:
    """Turns webhook events into settling pages."""

    def __init__(self, cfg, settling, settle, watched_parents=(), relevant_props=None):
        self.settling, self.settle = settling, settle
        # Purchasing's data source id and its database id (events may name either).
        self.parents = {_norm_id(p) for p in (cfg.purchasing_ds, *watched_parents) if p}
        # Property ids of PRICE_COLUMNS; None = every edit counts.
        self.relevant = relevant_props

    def handle(self, event):
        """Apply one event; returns a short description for the log."""
        kind = event.get("type", "")
        entity = event.get("entity") or {}
        pid = entity.get("id")
        if entity.get("type") != "page" or not pid:
            return f"ignored {kind or 'event'} (not a page)"
        if kind not in EVENTS:
            return f"ignored {kind} {pid}"
        data = event.get("data") or {}
        parent = data.get("parent") or {}
        parent_ids = {_norm_id(parent.get(k)) for k in ("data_source_id", "id") if parent.get(k)}
        if parent_ids and not parent_ids & self.parents:
            return f"ignored {kind} {pid} (another database)"
        if kind == "page.properties_updated" and self.relevant is not None:
            changed = {unquote(p) for p in data.get("updated_properties") or []}
            if changed and not changed & self.relevant:
                return f"ignored edit to {pid} (no price-relevant column changed)"
        self.settling.touch(pid, self.settle)
        return f"{kind} {pid}: looking at it in {self.settle:g}s"


# ---------------------------------------------------------------- worker
class Worker:
    """Queues settled rows, then drains the Job Log queue one row at a time."""

    def __init__(self, cfg, notion, checker, settling, limit, clock=time.time,
                 sleep=time.sleep, dry_run=False):
        self.cfg, self.notion, self.checker = cfg, notion, checker
        self.settling, self.limit, self.clock, self.sleep = settling, limit, clock, sleep
        self.dry_run, self.stopped = dry_run, threading.Event()
        self.next_poll = 0.0       # look at the queue now (startup backlog, PERBot's entries)
        self.backoff_until = 0.0
        self.failures = 0
        self.preview = []          # dry run: rows that would have been queued...
        self.seen = set()          # ...and rows already checked (nothing is marked done)

    def queue_settled(self):
        """Queue settled rows that are open and complete. Returns how many were new."""
        added = 0
        for pid in self.settling.pop_due():
            try:
                row = pc.parse_row(self.notion.page(pid))
            except RuntimeError as e:
                log(f"{pid}: couldn't read the page ({e})")
                continue
            reason = pc.skip_reason(row, self.cfg)
            if reason:
                log(f"{pid} ({row.part_number or 'no part number'}): not queued, {reason}")
                continue
            new = True if self.dry_run else pc.enqueue(self.notion, self.cfg, pid)
            if new:
                added += 1
                if self.dry_run:
                    self.preview.append(pid)
                log(f"{pid} ({row.part_number}): queued")
            else:
                log(f"{pid} ({row.part_number}): already queued or checked")
        if added:
            self.next_poll = 0.0
        return added

    def check_next(self):
        """Check the oldest queued row if the hourly limit and any backoff allow.
        Returns its status, or None if nothing was done."""
        now = self.clock()
        if now < self.backoff_until or now < self.next_poll or self.limit.wait() > 0:
            return None
        entries = [e for e in pc.queued(self.notion, self.cfg, len(self.seen) + 1)
                   if e[1] not in self.seen][:1]
        if not entries and self.preview:
            entries = [(None, self.preview.pop(0))]
        if entries and self.dry_run:
            self.seen.add(entries[0][1])
        if not entries:
            self.next_poll = now + POLL_SECONDS
            return None
        counts = self.checker.drain(entries)
        status = next(iter(counts))
        if status == "retry":
            self.failures += 1
            delay = min(RETRY_BASE_SECONDS * 2 ** (self.failures - 1), RETRY_MAX_SECONDS)
            self.backoff_until = now + delay
            log(f"check failed; trying again in {delay / 60:g} min")
        else:
            self.failures = 0
            if status not in ("skipped", "gone"):
                self.limit.record()  # only real searches count against the limit
        return status

    def step(self):
        self.queue_settled()
        return self.check_next()

    def run(self):
        while not self.stopped.is_set():
            if self.step() is None:
                self.sleep(2)


# ---------------------------------------------------------------- HTTP
def make_handler(router, secret, status):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet default access log
            pass

        def _reply(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in ("/", "/healthz"):
                return self._reply(200, {"ok": True, **status()})
            self._reply(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/notion/webhook":
                return self._reply(404, {"error": "not found"})
            size = int(self.headers.get("Content-Length") or 0)
            if size > MAX_BODY_BYTES:
                return self._reply(413, {"error": "too large"})
            body = self.rfile.read(size)
            try:
                event = json.loads(body or b"{}")
            except ValueError:
                return self._reply(400, {"error": "not JSON"})
            if "verification_token" in event and "type" not in event:
                log("Notion webhook verification token received. Paste it into the Notion "
                    "subscription to verify it, and set NOTION_WEBHOOK_SECRET to it:\n"
                    f"    {event['verification_token']}")
                return self._reply(200, {"ok": True})
            if not valid_signature(secret, body, self.headers.get("X-Notion-Signature")):
                log("rejected an event with a missing or invalid signature"
                    + ("" if secret else " (NOTION_WEBHOOK_SECRET is not set)"))
                return self._reply(401, {"error": "bad signature"})
            log(router.handle(event))
            self._reply(200, {"ok": True})

    return Handler


def main():
    p = argparse.ArgumentParser(description="Price-check Purchasing rows as Notion reports "
                                            "them added or edited.")
    p.add_argument("--dry-run", action="store_true",
                   help="log alerts instead of DMing; nothing written to Notion")
    p.add_argument("--provider", choices=list(scraper2.PROVIDERS), help="LLM provider")
    p.add_argument("--model", help="LLM model id")
    p.add_argument("--rpm", type=float, default=15, help="max LLM requests per minute")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()
    load_env()
    cfg = pc.Config.from_env(need_slack=not a.dry_run)
    if not cfg.job_log_ds:
        sys.exit("NOTION_JOB_LOG_DS_ID is empty: the queue lives in the Job Log.")
    secret = (os.getenv("NOTION_WEBHOOK_SECRET") or "").strip()
    settle = float(os.getenv("PRICE_LISTENER_SETTLE_SECONDS") or 60)
    per_hour = int(os.getenv("PRICE_LISTENER_MAX_PER_HOUR") or 20)
    port = int(os.getenv("PORT") or 8080)
    try:
        llm = scraper2.LLM(a.provider, a.model, a.rpm, a.verbose)
    except scraper2.LLMError as e:
        sys.exit(str(e))

    notion = pc.Notion(cfg.notion_token, cfg.notion_version)
    slack = pc.Slack(cfg.slack_token) if cfg.slack_token else None
    source = notion.data_source(cfg.purchasing_ds)
    settling = Settling()
    router = Router(cfg, settling, settle, [(source.get("parent") or {}).get("database_id")],
                    relevant_property_ids(source.get("properties", {})))
    checker = pc.Checker(cfg, notion, slack, pc.make_find(llm, cfg, a.verbose), a.dry_run, a.verbose)
    worker = Worker(cfg, notion, checker, settling, HourlyLimit(per_hour), dry_run=a.dry_run)

    # Catch up on rows added while the listener wasn't running.
    found = pc.discover(notion, cfg, dry_run=a.dry_run)
    if a.dry_run:
        worker.preview += found
    log(f"startup: {len(found)} row(s) from the last {cfg.lookback_hours:g} h "
        f"{'would be ' if a.dry_run else ''}queued")
    threading.Thread(target=worker.run, daemon=True).start()

    def status():
        return {"settling": len(settling), "checks_this_hour": len(worker.limit.starts)}
    if not secret:
        log("NOTION_WEBHOOK_SECRET not set: only the verification handshake will be accepted.")
    log(f"listening on :{port}/notion/webhook - settle {settle:g}s, max {per_hour}/hour, "
        f"LLM {llm.provider}/{llm.model}{' (dry run)' if a.dry_run else ''}")
    ThreadingHTTPServer(("", port), make_handler(router, secret, status)).serve_forever()


if __name__ == "__main__":
    main()

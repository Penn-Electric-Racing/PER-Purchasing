#!/usr/bin/env python3
"""
check.py - re-price open Purchasing (PEFS) rows and DM the orderer when a price moved.

Runs over the same Notion database PERBot writes to (/parts "Add to Purchasing") and reads
(/benbuys prep): this season's rows that aren't ordered yet (Exported? unchecked) and have a
part number, quantity and unit cost. For each row it searches the part with scrapers.find
(web search + Mouser/DigiKey APIs + LLM page reading), always including the row's own
product link, and compares the unit price found at that quantity with the row's Unit cost:

  found price = the price at the row's own link if it could be read, else the best option
  alert when  |found - Unit cost| / Unit cost >= PRICE_CHECK_THRESHOLD_PCT
         and  |found - Unit cost| x Quant. >= PRICE_CHECK_MIN_DELTA_USD

Which rows: new Purchasing rows, however they got there (PERBot's /parts, typed in Notion,
imported). listener.py hears about them from Notion's webhooks and queues them in
PERBot's Job Log ("price-check-queue <page id>", the same entry PERBot's /parts writes),
then checks the queue one row at a time with Checker.drain. This file holds the check, plus
a CLI: --queue does one pass by hand (queue complete rows created in the last
PRICE_CHECK_LOOKBACK_HOURS that were never queued, then check up to PRICE_CHECK_BATCH queued
rows and mark them done).

The alert is a Slack DM to the row's Point of contact (Notion person -> Slack user by email,
then an unambiguous name match, as PERBot's identity.ts does). Each alert is claimed once in
PERBot's Job Log ("price-alert <row id> <unit cost> x<quant>"), so a row only alerts again
after someone changes its Unit cost or Quant. The DM carries Use / Keep buttons whose clicks
PERBot handles (see alert.py).

Environment (same names as PERBot; .env is read too):
  NOTION_TOKEN or NOTION_API_KEY   PERBot's Notion integration token
  SLACK_BOT_TOKEN                  PERBot's bot token (chat:write, users:read, users:read.email)
  BENBUYS_PURCHASING_DS_ID         Purchasing data source (default: PERBot's)
  BENBUYS_SEASON                   default REV12
  NOTION_JOB_LOG_DS_ID             Job Log data source (default: PERBot's; "" = no dedup)
  NOTION_API_VERSION               default 2026-03-11
  PRICE_CHECK_THRESHOLD_PCT        default 10
  PRICE_CHECK_MIN_DELTA_USD        default 0 (order-total change needed to alert)
  PRICE_CHECK_CHANNEL              post here when the orderer can't be matched (default: log only)
  PRICE_CHECK_POOL                 search results read per row (default 15, like PARTS_POOL)
  PRICE_CHECK_BATCH                queued rows checked per run (default 10)
  PRICE_CHECK_LOOKBACK_HOURS       queue new rows created this recently (default 168; 0 = off)
  plus the search / distributor / LLM keys the scrapers use.

Usage:
    python -m pricecheck.check --queue -v              # one pass: queue new rows + check the queue
    python -m pricecheck.check --enqueue <notion url>  # queue a row by hand
    python -m pricecheck.check --row <notion url/id>   # check one row now
    python -m pricecheck.check --all --dry-run -v      # sweep every open row, print alerts only
    python -m pricecheck.check --all --limit 20        # sweep the 20 oldest open rows
"""
import argparse
import re
import sys
from urllib.parse import urlparse

from scrapers import llm_scraper
from scrapers.env import load_env

from .alert import alert_blocks, format_alert, money
from .comparison import compare
from .config import Config
from .joblog import claim_once, discover, enqueue, mark_done, queued
from .notion import Notion, open_rows, parse_row, skip_reason
from .slack import Slack, slack_user

# A sweep stops after this many rows in a row got no price because search / the LLM was
# failing (quota, outage), instead of running through every row for nothing.
MAX_OUTAGE_ROWS = 3


def _outage(results):
    """True when nothing got a price and the LLM failed on the pages: a quota / outage, not
    a part nobody sells, so the row should be tried again later."""
    return not any(r.get("subtotal") is not None for r in results) and \
        any("llm:" in (r.get("fetch_error") or "") for r in results)


# ---------------------------------------------------------------- run
class Checker:
    """Checks one row at a time (used by --queue, --row and --all).

    check(row) returns "alerted", "ok" (priced, within thresholds, or already alerted),
    "skipped" (see skip_reason) or "retry" (search / the LLM failed: try again later).
    `find(row)` returns llm_scraper.find-style scored results.
    """

    def __init__(self, cfg, notion, slack, find, dry_run=False, verbose=False):
        self.cfg, self.notion, self.slack, self.find = cfg, notion, slack, find
        self.dry_run, self.verbose = dry_run, verbose
        self._emails = None

    def notion_emails(self):
        """Notion user id -> email, fetched once (people properties may omit emails)."""
        if self._emails is None:
            try:
                self._emails = self.notion.users()
            except RuntimeError as e:
                print(f"Notion users unavailable ({e}); matching by name.", file=sys.stderr)
                self._emails = {}
        return self._emails

    def check(self, row, label=None):
        label = label or f"{row.part_number} x{row.units} @ ${money(row.unit_price)}"
        reason = skip_reason(row, self.cfg)
        if reason:
            if self.verbose:
                print(f"{label}: skipped, {reason}", file=sys.stderr)
            return "skipped"
        try:
            results = self.find(row)
            failed = "LLM failed on every page" if _outage(results) else None
        except Exception as e:  # one bad row must not stop the rest
            failed = f"search failed: {e}"
        if failed:
            print(f"{label}: {failed}", file=sys.stderr)
            return "retry"
        f = compare(row, results, self.cfg)
        if f is None:
            if self.verbose:
                print(f"{label}: ok", file=sys.stderr)
            return "ok"
        return "alerted" if self._alert(f, label) else "ok"

    def drain(self, entries):
        """Check queued rows [(Job Log entry id, page id)] in order. Entries are marked done
        once their row is checked, skipped or gone; failures stay queued, and the run stops
        after MAX_OUTAGE_ROWS of them in a row. An entry id of None (a dry-run preview of a
        row discovery would queue) is checked but not marked. Returns {status: count}."""
        counts, outages = {}, 0
        for i, (entry, pid) in enumerate(entries, 1):
            try:
                row = parse_row(self.notion.page(pid))
            except RuntimeError as e:
                gone = "HTTP 404" in str(e)
                print(f"[{i}/{len(entries)}] {pid}: {'row deleted' if gone else e}", file=sys.stderr)
                status = "gone" if gone else "retry"
            else:
                status = self.check(row, f"[{i}/{len(entries)}] {row.part_number} x{row.units} "
                                         f"@ ${money(row.unit_price)}")
            counts[status] = counts.get(status, 0) + 1
            if status == "retry":
                outages += 1
                if outages >= MAX_OUTAGE_ROWS:
                    print(f"Stopping: {outages} rows in a row failed (LLM quota or search "
                          "outage?). They stay queued.", file=sys.stderr)
                    break
                continue
            outages = 0
            if entry and not self.dry_run:
                try:
                    mark_done(self.notion, entry)
                except RuntimeError as e:
                    print(f"  couldn't mark queue entry {entry} done: {e}", file=sys.stderr)
        return counts

    def check_all(self, rows):
        """Sweep `rows` in order; stops after MAX_OUTAGE_ROWS retries in a row.
        Returns {status: count}."""
        counts, outages = {}, 0
        for i, row in enumerate(rows, 1):
            status = self.check(row, f"[{i}/{len(rows)}] {row.part_number} x{row.units} "
                                     f"@ ${money(row.unit_price)}")
            counts[status] = counts.get(status, 0) + 1
            outages = outages + 1 if status == "retry" else 0
            if outages >= MAX_OUTAGE_ROWS:
                print(f"Stopping: {outages} rows in a row failed (LLM quota or search "
                      "outage?).", file=sys.stderr)
                break
        return counts

    def _alert(self, f, label):
        """Claim the alert in the Job Log, then DM the row's contacts (or print it).
        False if it was already sent."""
        row, text, cfg = f.row, format_alert(f), self.cfg
        key = f"price-alert {row.id} {row.unit_cost:g} x{row.quantity}"
        if not claim_once(self.notion, cfg, key, dry_run=self.dry_run):
            if self.verbose:
                print(f"{label}: {f.delta_pct:+.0f}%, already alerted", file=sys.stderr)
            return False
        targets = [] if self.slack is None else \
            [u for u in (slack_user(self.slack, c, self.notion_emails) for c in row.contacts) if u]
        if not targets and cfg.fallback_channel:
            targets = [cfg.fallback_channel]
        print(f"{label}: {f.delta_pct:+.0f}% -> {', '.join(targets) or 'nobody to DM'}",
              file=sys.stderr)
        blocks = alert_blocks(f, text)
        if self.dry_run:
            buttons = [e for b in blocks[1:] for e in b["elements"]]
            print(text + ("\n[" + " | ".join(b["text"]["text"] for b in buttons) + "]"
                          if buttons else "") + "\n")
            return True
        if not targets:
            print(f"  no Slack match for {[c['name'] for c in row.contacts]} and no "
                  f"PRICE_CHECK_CHANNEL - alert not sent:\n{text}", file=sys.stderr)
        for t in targets:
            try:
                self.slack.post(t, text, blocks)
            except RuntimeError as e:
                print(f"  Slack post to {t} failed: {e}", file=sys.stderr)
        return True


def make_find(llm, cfg, verbose=False):
    """find(row) for Checker: search the part at the row's quantity, always reading its link."""
    def find(row):
        extra = [{"title": row.name or row.part_number, "url": row.link}] if row.link else []
        return llm_scraper.find(row.part_number, row.units, llm, pool=cfg.pool, extra=extra,
                             verbose=verbose)[2]
    return find


def _page_id(s):
    """A Notion page id from an id or a notion.so URL (".../Title-<32 hex>?pvs=4")."""
    tail = urlparse(s).path.rstrip("/").rsplit("/", 1)[-1] if "://" in s else s.strip()
    m = re.search(r"([0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
                  tail, re.I)
    return m.group(1) if m else s


def main():
    p = argparse.ArgumentParser(
        prog="python -m pricecheck.check",
        description="Price-check Purchasing rows and DM the orderer when the price moved.")
    which = p.add_mutually_exclusive_group(required=True)
    which.add_argument("--queue", action="store_true",
                       help="queue new rows, then check up to PRICE_CHECK_BATCH queued rows")
    which.add_argument("--enqueue", metavar="ROW", help="queue a Purchasing page (Notion URL or id)")
    which.add_argument("--row", help="check this Purchasing page now (Notion URL or id)")
    which.add_argument("--all", action="store_true", help="sweep every open row, oldest first")
    p.add_argument("--limit", type=int, help="at most N rows (--queue: default PRICE_CHECK_BATCH)")
    p.add_argument("--dry-run", action="store_true",
                   help="print alerts instead of DMing; nothing written to Notion")
    p.add_argument("--provider", choices=list(llm_scraper.PROVIDERS), help="LLM provider")
    p.add_argument("--model", help="LLM model id")
    p.add_argument("--rpm", type=float, default=15, help="max LLM requests per minute")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()
    load_env()
    cfg = Config.from_env(need_slack=not (a.dry_run or a.enqueue))
    notion = Notion(cfg.notion_token, cfg.notion_version)
    if a.enqueue:
        pid = _page_id(a.enqueue)
        print(f"{pid}: {'queued' if enqueue(notion, cfg, pid) else 'already queued'}", file=sys.stderr)
        return
    slack = Slack(cfg.slack_token) if cfg.slack_token else None
    if a.queue:
        found = discover(notion, cfg, dry_run=a.dry_run)
        if found:
            print(f"Price check: {len(found)} new row(s) "
                  f"{'would be ' if a.dry_run else ''}queued.", file=sys.stderr)
        entries = queued(notion, cfg, a.limit or cfg.batch)
        if a.dry_run:  # nothing was written: preview the rows discovery found too
            entries += [(None, pid) for pid in found]
            entries = entries[:a.limit or cfg.batch]
        if not entries:  # nothing to do: don't touch the LLM
            print("Price check: queue is empty.", file=sys.stderr)
            return
    try:
        llm = llm_scraper.LLM(a.provider, a.model, a.rpm, a.verbose)
    except llm_scraper.LLMError as e:
        sys.exit(str(e))
    checker = Checker(cfg, notion, slack, make_find(llm, cfg, a.verbose), a.dry_run, a.verbose)
    thresholds = (f"alert at ±{cfg.threshold_pct:g}%"
                  f"{f' and ${cfg.min_delta_usd:g}' if cfg.min_delta_usd else ''}"
                  f"{' (dry run)' if a.dry_run else ''}")
    if a.queue:
        print(f"Price check: {len(entries)} queued row(s), {thresholds}", file=sys.stderr)
        counts = checker.drain(entries)
    else:
        rows = [parse_row(notion.page(_page_id(a.row)))] if a.row else open_rows(notion, cfg, a.limit)
        print(f"Price check: {len(rows)} row(s), {thresholds}", file=sys.stderr)
        counts = checker.check_all(rows)
    print("Price check: " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())),
          file=sys.stderr)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
price_check.py - re-price open Purchasing (PEFS) rows and DM the orderer when a price moved.

Runs over the same Notion database PERBot writes to (/parts "Add to Purchasing") and reads
(/benbuys prep): this season's rows that aren't ordered yet (Exported? unchecked) and have a
part number, quantity and unit cost. For each row it searches the part with scraper2.find
(web search + Mouser/DigiKey APIs + LLM page reading), always including the row's own
product link, and compares the unit price found at that quantity with the row's Unit cost:

  found price = the price at the row's own link if it could be read, else the best option
  alert when  |found - Unit cost| / Unit cost >= PRICE_CHECK_THRESHOLD_PCT
         and  |found - Unit cost| x Quant. >= PRICE_CHECK_MIN_DELTA_USD

Which rows: every Purchasing row, however it got there (PERBot's /parts, typed in Notion,
imported), through a queue in PERBot's Job Log. Each run of the workflow
(.github/workflows/price-check.yml, every 30 min) with --queue:

  1. finds open, complete rows created in the last PRICE_CHECK_LOOKBACK_HOURS (default 168)
     that were never queued, and queues them: a "price-check-queue <page id>" entry in the
     Job Log (claim_once, the same key PERBot's claimOnce writes, so nothing is queued twice);
  2. checks up to PRICE_CHECK_BATCH queued rows, oldest first, one at a time, and marks each
     entry done (Job "price-check-done"; the entry stays, so the row isn't queued again).

A burst of new rows drains a batch per run instead of hitting the search / LLM limits at
once. If search or the LLM is failing, entries stay queued and the run stops after 3.
PERBot's /parts also queues its rows and sends a repository_dispatch, which just starts a
run straight away instead of waiting for the schedule.

The alert is a Slack DM to the row's Point of contact (Notion person -> Slack user by email,
then an unambiguous name match, as PERBot's identity.ts does). Each alert is claimed once in
PERBot's Job Log ("price-alert <row id> <unit cost> x<quant>"), so a row only alerts again
after someone changes its Unit cost or Quant.

Environment (same names as PERBot; .env is read like scraper.py):
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
  plus the search / distributor / LLM keys scraper2.py uses.

Usage:
    python price_check.py --queue -v              # find new rows + drain (what the workflow runs)
    python price_check.py --enqueue <notion url>  # queue a row by hand
    python price_check.py --row <notion url/id>   # check one row now
    python price_check.py --all --dry-run -v      # sweep every open row, print alerts only
    python price_check.py --all --limit 20        # sweep the 20 oldest open rows
"""
import argparse
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests

import scraper2
from ranking import Scorer
from scraper import _same_url, load_env

NOTION_API = "https://api.notion.com/v1"
SLACK_API = "https://slack.com/api"
TIMEOUT = 30

# PERBot's defaults (PERBot/src/config.ts), so both apps point at the same databases.
DEFAULT_PURCHASING_DS = "39e560bc-c039-83db-9f16-076cb18f7d7b"
DEFAULT_JOB_LOG_DS = "db0c6a0d-e10b-4edf-97fb-6f381d15a465"

# Purchasing columns (PERBot/src/benbuys/notion.ts, partFinder/notion.ts).
COL_PART = "Cat. or model no."
COL_QTY = "Quant."
COL_UNIT_COST = "Unit cost"
COL_LINK = "Link to product webpage"
COL_CONTACT = "Point of contact"
COL_NOTES = "Notes (internal)"
COL_SEASON = "Season"
COL_EXPORTED = "Exported?"
# Job Log entries for rows to check ("price-check-queue <page id>", Job = QUEUE_JOB); once
# checked, Job becomes DONE_JOB and the entry stays as the "already seen" record.
QUEUE_JOB = "price-check-queue"
DONE_JOB = "price-check-done"
# A sweep stops after this many rows in a row got no price because search / the LLM was
# failing (quota, outage), instead of running through every row for nothing.
MAX_OUTAGE_ROWS = 3


@dataclass
class Config:
    notion_token: str
    slack_token: str
    purchasing_ds: str = DEFAULT_PURCHASING_DS
    season: str = "REV12"
    job_log_ds: str = DEFAULT_JOB_LOG_DS
    notion_version: str = "2026-03-11"
    threshold_pct: float = 10.0
    min_delta_usd: float = 0.0
    fallback_channel: str = ""
    pool: int = 15
    batch: int = 10
    lookback_hours: float = 168

    @classmethod
    def from_env(cls, need_slack=True):
        """Read the PERBot-compatible environment; exits with a message if a token is missing."""
        def env(name, default=""):
            return (os.getenv(name) or "").strip() or default

        def number(name, default):
            try:
                return float(env(name, str(default)))
            except ValueError:
                sys.exit(f"Environment variable {name} must be a number.")

        notion = env("NOTION_TOKEN") or env("NOTION_API_KEY")
        if not notion:
            sys.exit("Missing required environment variable: one of NOTION_TOKEN / NOTION_API_KEY")
        slack = env("SLACK_BOT_TOKEN")
        if need_slack and not slack:
            sys.exit("Missing required environment variable: SLACK_BOT_TOKEN")
        return cls(
            notion_token=notion, slack_token=slack,
            purchasing_ds=env("BENBUYS_PURCHASING_DS_ID", DEFAULT_PURCHASING_DS),
            season=env("BENBUYS_SEASON", "REV12"),
            # Set but empty disables dedup, as in PERBot.
            job_log_ds=os.getenv("NOTION_JOB_LOG_DS_ID", DEFAULT_JOB_LOG_DS).strip(),
            notion_version=env("NOTION_API_VERSION", "2026-03-11"),
            threshold_pct=number("PRICE_CHECK_THRESHOLD_PCT", 10),
            min_delta_usd=number("PRICE_CHECK_MIN_DELTA_USD", 0),
            fallback_channel=env("PRICE_CHECK_CHANNEL"),
            pool=int(number("PRICE_CHECK_POOL", 15)),
            batch=max(1, int(number("PRICE_CHECK_BATCH", 10))),
            lookback_hours=number("PRICE_CHECK_LOOKBACK_HOURS", 168),
        )


# ---------------------------------------------------------------- Notion
class Notion:
    """The few Notion API calls this job needs (data sources API, as PERBot uses)."""

    def __init__(self, token, version):
        self.headers = {"Authorization": f"Bearer {token}", "Notion-Version": version,
                        "Content-Type": "application/json"}

    def _call(self, method, path, body=None):
        r = requests.request(method, NOTION_API + path, headers=self.headers, json=body,
                             timeout=TIMEOUT)
        if r.status_code != 200:
            raise RuntimeError(f"Notion {method} {path}: HTTP {r.status_code} {r.text[:300]}")
        return r.json()

    def query(self, data_source_id, filter_=None, page_size=100, sorts=None, limit=None):
        """Pages of a data source matching `filter_`, following pagination up to `limit`."""
        pages, cursor = [], None
        while True:
            body = {"page_size": page_size, **({"filter": filter_} if filter_ else {}),
                    **({"sorts": sorts} if sorts else {}),
                    **({"start_cursor": cursor} if cursor else {})}
            res = self._call("POST", f"/data_sources/{data_source_id}/query", body)
            pages += res.get("results", [])
            cursor = res.get("next_cursor") if res.get("has_more") else None
            if not cursor or page_size == 1 or (limit and len(pages) >= limit):
                return pages[:limit] if limit else pages

    def data_source(self, data_source_id):
        """The data source object: its "properties" (column name -> {"id", "type", ...}) and
        "parent" (the database it belongs to)."""
        return self._call("GET", f"/data_sources/{data_source_id}")

    def page(self, page_id):
        return self._call("GET", f"/pages/{page_id}")

    def update(self, page_id, properties):
        return self._call("PATCH", f"/pages/{page_id}", {"properties": properties})

    def create(self, data_source_id, properties):
        return self._call("POST", "/pages", {
            "parent": {"type": "data_source_id", "data_source_id": data_source_id},
            "properties": properties})

    def users(self):
        """Workspace people as {id: email} (needs the integration's user-email capability)."""
        out, cursor = {}, None
        while True:
            res = self._call("GET", "/users?page_size=100" + (f"&start_cursor={cursor}" if cursor else ""))
            for u in res.get("results", []):
                if u.get("type") == "person":
                    out[u["id"]] = (u.get("person") or {}).get("email")
            cursor = res.get("next_cursor") if res.get("has_more") else None
            if not cursor:
                return out


def claim_once(notion, cfg, key, job="price-check", dry_run=False):
    """PERBot's utils/schedule.ts claimOnce: True if this is the first claim of `key` in the
    Job Log (go ahead and alert), False if it's already there. Claimed BEFORE sending, so a
    crash mid-send counts as sent rather than double-DMing someone."""
    if not cfg.job_log_ds:
        print(f'Job log: NOTION_JOB_LOG_DS_ID not set - cannot dedupe "{key}".', file=sys.stderr)
        return True
    if notion.query(cfg.job_log_ds, {"property": "Run key", "title": {"equals": key}}, page_size=1):
        return False
    if not dry_run:
        notion.create(cfg.job_log_ds, {
            "Run key": {"title": [{"text": {"content": key}}]},
            "Job": {"rich_text": [{"text": {"content": job}}]},
            "Ran at": {"date": {"start": datetime.now(timezone.utc).isoformat()}},
        })
    return True


# ---------------------------------------------------------------- queue (Job Log)
def queue_key(page_id):
    return f"{QUEUE_JOB} {page_id}"


def enqueue(notion, cfg, page_id):
    """Queue a row, as PERBot does (no-op if it's already queued). True if newly queued."""
    return claim_once(notion, cfg, queue_key(page_id), job=QUEUE_JOB)


def mark_done(notion, entry_id):
    """Mark a queue entry checked; it stays in the Job Log so the row isn't queued again."""
    notion.update(entry_id, {"Job": {"rich_text": [{"text": {"content": DONE_JOB}}]}})


def discover(notion, cfg, now=None, dry_run=False):
    """Queue open, complete Purchasing rows created in the last cfg.lookback_hours that were
    never queued (rows from /parts are usually queued by PERBot already). Returns the page
    ids newly queued (with dry_run: the ones that would be)."""
    if cfg.lookback_hours <= 0:
        return []
    since = (now or datetime.now(timezone.utc)) - timedelta(hours=cfg.lookback_hours)
    pages = notion.query(cfg.purchasing_ds, {"and": _open_filter(cfg) + [
        {"timestamp": "created_time", "created_time": {"on_or_after": since.isoformat()}}]},
        sorts=[{"timestamp": "created_time", "direction": "ascending"}])
    return [pg["id"] for pg in pages
            if claim_once(notion, cfg, queue_key(pg["id"]), job=QUEUE_JOB, dry_run=dry_run)]


def queued(notion, cfg, limit=None):
    """Queued rows, oldest first: [(Job Log entry id, Purchasing page id)]."""
    if not cfg.job_log_ds:
        sys.exit("NOTION_JOB_LOG_DS_ID is empty: the queue lives in the Job Log.")
    entries = notion.query(cfg.job_log_ds, {"property": "Job", "rich_text": {"equals": QUEUE_JOB}},
                           sorts=[{"property": "Ran at", "direction": "ascending"}], limit=limit)
    out = []
    for e in entries:
        key = _text((e.get("properties") or {}).get("Run key"))
        if key.startswith(QUEUE_JOB + " "):
            out.append((e["id"], key[len(QUEUE_JOB) + 1:].strip()))
    return out


# ---------------------------------------------------------------- Purchasing rows
@dataclass
class Row:
    id: str
    url: str
    name: str
    part_number: str
    quantity: int
    unit_cost: float
    link: str
    contacts: list = field(default_factory=list)  # [{"id", "name", "email"}]
    season: str = ""
    exported: bool = False
    parent_ds: str = ""   # the data source the page lives in
    # /parts writes pack listings as Quant. = packs and Unit cost = price per pack, and
    # says "Sold in packs of N" in the notes; prices are compared per unit.
    pack_size: int = 1

    @property
    def units(self):
        return self.quantity * self.pack_size

    @property
    def unit_price(self):
        return self.unit_cost / self.pack_size


PACK_NOTE_RE = re.compile(r"Sold in packs of (\d+)")


def _text(prop):
    return "".join(t.get("plain_text", "") for t in (prop or {}).get("rich_text")
                   or (prop or {}).get("title") or []).strip()


def parse_row(page):
    """A Purchasing page as a Row (field meanings as in PERBot's parsePurchasingRow)."""
    p = page.get("properties", {})
    title = next((v for v in p.values() if v.get("type") == "title"), {})
    return Row(
        id=page["id"], url=page.get("url", ""), name=_text(title),
        part_number=_text(p.get(COL_PART)),
        quantity=int((p.get(COL_QTY) or {}).get("number") or 0),
        unit_cost=float((p.get(COL_UNIT_COST) or {}).get("number") or 0),
        link=(p.get(COL_LINK) or {}).get("url") or "",
        contacts=[{"id": u.get("id"), "name": u.get("name") or "",
                   "email": (u.get("person") or {}).get("email")}
                  for u in (p.get(COL_CONTACT) or {}).get("people") or []],
        pack_size=int(m.group(1)) if (m := PACK_NOTE_RE.search(_text(p.get(COL_NOTES)))) else 1,
        season=((p.get(COL_SEASON) or {}).get("select") or {}).get("name") or "",
        exported=bool((p.get(COL_EXPORTED) or {}).get("checkbox")),
        parent_ds=(page.get("parent") or {}).get("data_source_id") or "",
    )


def _same_id(a, b):
    return a.replace("-", "").lower() == b.replace("-", "").lower()


def skip_reason(row, cfg):
    """Why this row isn't price-checked (not Purchasing, ordered, other season, incomplete),
    or None if it should be."""
    if row.parent_ds and not _same_id(row.parent_ds, cfg.purchasing_ds):
        return "not a Purchasing row"
    if row.exported:
        return "already ordered (Exported?)"
    if row.season != cfg.season:
        return f"season {row.season or 'empty'}, not {cfg.season}"
    if not (row.part_number and row.quantity > 0 and row.unit_cost > 0):
        return "part number, Quant. or Unit cost missing"
    return None


def _open_filter(cfg):
    """This season's not-yet-ordered rows with enough filled in to price-check."""
    return [
        {"property": COL_SEASON, "select": {"equals": cfg.season}},
        {"property": COL_EXPORTED, "checkbox": {"equals": False}},
        {"property": COL_PART, "rich_text": {"is_not_empty": True}},
        {"property": COL_QTY, "number": {"greater_than": 0}},
        {"property": COL_UNIT_COST, "number": {"greater_than": 0}},
    ]


def open_rows(notion, cfg, limit=None):
    """Every open row (oldest first), for a manual sweep."""
    return [parse_row(pg) for pg in notion.query(cfg.purchasing_ds, {"and": _open_filter(cfg)},
                                                 sorts=[{"timestamp": "created_time",
                                                         "direction": "ascending"}],
                                                 limit=limit)]


def _outage(results):
    """True when nothing got a price and the LLM failed on the pages: a quota / outage, not
    a part nobody sells, so the row should be tried again later."""
    return not any(r.get("subtotal") is not None for r in results) and \
        any("llm:" in (r.get("fetch_error") or "") for r in results)


# ---------------------------------------------------------------- comparing
@dataclass
class Finding:
    row: Row
    found: dict            # the listing whose price is compared with the row
    at_link: bool          # found is the row's own product link
    delta_pct: float       # (found - sheet) / sheet x 100
    cheaper: dict = None   # a better-priced option than `found`, if one was seen

    @property
    def found_unit(self):
        return self.found["unit_cost"]


def _comparable(r):
    """A priced USD listing of the right part with enough stock."""
    return (r.get("unit_cost") and r.get("score") is not None
            and (r.get("currency") or "USD") == "USD"
            and r.get("enough_stock") is not False
            and not any(n == "part number not in listing" or n.startswith("priced in")
                        for n in r.get("notes", [])))


def compare(row, results, cfg):
    """A Finding when the found price moved past the thresholds, else None."""
    link = next((r for r in results if row.link and _same_url(r["url"], row.link)
                 and r.get("unit_cost") and (r.get("currency") or "USD") == "USD"), None)
    ranked = [r for r in Scorer.pick_top(results, len(results)) if _comparable(r)]
    best = ranked[0] if ranked else None
    found = link or best
    if not found:
        return None
    delta = found["unit_cost"] - row.unit_price
    pct = delta / row.unit_price * 100
    if abs(pct) < cfg.threshold_pct or abs(delta) * row.units < cfg.min_delta_usd:
        return None
    cheaper = min((r for r in ranked if r is not found), key=lambda r: r["unit_cost"], default=None)
    if cheaper and cheaper["unit_cost"] >= min(found["unit_cost"], row.unit_price):
        cheaper = None
    return Finding(row, found, link is not None, round(pct, 1), cheaper)


def money(n):
    """Like PERBot's partFinder money(): 2 decimals, up to 4 for sub-dollar prices (0.0425)."""
    if n >= 1:
        return f"{n:,.2f}"
    s = f"{n:.4f}".rstrip("0")
    return s + "0" * max(0, 2 - len(s.split(".")[1]))


def _flags(r):
    return [n for n in r.get("notes", []) if n != "shipping cost estimated"]


def price_break(r):
    """" (25+ price)" when a quantity price break set this listing's unit price, else ""."""
    bought = (r.get("packs_needed") or 0) * (r.get("pack_size") or 1)
    tier = max((b["min_qty"] for b in r.get("price_breaks") or [] if b["min_qty"] <= bought),
               default=None)
    return f" ({tier}+ price)" if tier and tier > 1 else ""


def format_alert(f):
    """Slack mrkdwn for one alert, in PERBot's style (emoji lead, <url|label> links)."""
    row, qty = f.row, f.row.units
    up = f.delta_pct > 0
    where = "at your link" if f.at_link else "best option found"
    supplier = f.found.get("supplier") or "listing"
    lines = [
        f"{':chart_with_upwards_trend:' if up else ':chart_with_downwards_trend:'} "
        f"*Price {'went up' if up else 'dropped'} on <{row.url}|{row.name or row.part_number}>*"
        f" (`{row.part_number}` ×{qty})",
        f"• In Notion: ${money(row.unit_price)}/unit (${money(row.unit_price * qty)})"
        + (f", ${money(row.unit_cost)}/pack of {row.pack_size}" if row.pack_size > 1 else ""),
        f"• Now ({where}): ${money(f.found_unit)}/unit{price_break(f.found)} "
        f"(${money(f.found_unit * qty)}) at "
        f"<{f.found['url']}|{supplier}> - *{f.delta_pct:+.0f}%*",
    ]
    if f.cheaper:
        c = f.cheaper
        lines.append(f"• Cheaper option: ${money(c['unit_cost'])}/unit at "
                     f"<{c['url']}|{c.get('supplier') or 'listing'}>"
                     + (f" ({_flags(c)[0]})" if _flags(c) else ""))
    if _flags(f.found):
        lines.append(f"• Flags: {'; '.join(_flags(f.found))}")
    lines.append("_Update `Unit cost` in Notion if this is right. Prices are read "
                 "automatically from the listing, so check before ordering._")
    return "\n".join(lines)


# ---------------------------------------------------------------- Slack
class Slack:
    def __init__(self, token):
        self.headers = {"Authorization": f"Bearer {token}"}
        self._directory = None

    def _call(self, method, http="post", **params):
        fn = requests.post if http == "post" else requests.get
        kw = {"json": params} if http == "post" else {"params": params}
        r = fn(f"{SLACK_API}/{method}", headers=self.headers, timeout=TIMEOUT, **kw)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Slack {method}: {data.get('error', r.status_code)}")
        return data

    def post(self, channel, text):
        # A user id as channel DMs that user (chat:write only, as PERBot's jobs do).
        self._call("chat.postMessage", channel=channel, text=text, unfurl_links=False)

    def lookup_email(self, email):
        try:
            return self._call("users.lookupByEmail", http="get", email=email)["user"]["id"]
        except (RuntimeError, KeyError):
            return None

    def directory(self):
        """Slack members as [(id, [names], email)] (cached)."""
        if self._directory is None:
            self._directory, cursor = [], None
            while True:
                res = self._call("users.list", http="get", limit=200,
                                 **({"cursor": cursor} if cursor else {}))
                for m in res.get("members", []):
                    if m.get("deleted") or m.get("is_bot") or m.get("id") == "USLACKBOT":
                        continue
                    prof = m.get("profile") or {}
                    names = [n for n in (prof.get("real_name") or m.get("real_name"),
                                         prof.get("display_name"), m.get("name")) if n]
                    if names:
                        self._directory.append((m["id"], names, (prof.get("email") or "").lower() or None))
                cursor = (res.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
        return self._directory


def _norm(s):
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def match_by_name(name, directory):
    """PERBot's identity.ts matchByName: exact name, unique token subset, or unique surname +
    first initial. Ambiguous matches return None (never guessed)."""
    norm = _norm(name)
    if not norm:
        return None
    entries = [(uid, {_norm(n) for n in names}, {t for n in names for t in _norm(n).split()})
               for uid, names, _ in directory]
    exact = [uid for uid, ex, _ in entries if norm in ex]
    if exact:
        return exact[0] if len(exact) == 1 else None
    q = norm.split()
    subset = [uid for uid, _, toks in entries if all(t in toks for t in q)]
    if subset:
        return subset[0] if len(subset) == 1 else None
    if len(q) >= 2:
        nick = [uid for uid, _, toks in entries
                if q[-1] in toks and any(t != q[-1] and t[0] == q[0][0] for t in toks)]
        if len(nick) == 1:
            return nick[0]
    return None


def slack_user(slack, contact, notion_emails):
    """Notion person -> Slack user id: email first, then a conservative name match."""
    email = (contact.get("email") or notion_emails().get(contact["id"]) or "").lower()
    if email:
        hit = next((uid for uid, _, e in slack.directory() if e == email), None) \
            or slack.lookup_email(email)
        if hit:
            return hit
    return match_by_name(contact.get("name", ""), slack.directory())


# ---------------------------------------------------------------- run
class Checker:
    """Checks one row at a time (used by --queue, --row and --all).

    check(row) returns "alerted", "ok" (priced, within thresholds, or already alerted),
    "skipped" (see skip_reason) or "retry" (search / the LLM failed: try again later).
    `find(row)` returns scraper2.find-style scored results.
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
        if self.dry_run:
            print(text + "\n")
            return True
        if not targets:
            print(f"  no Slack match for {[c['name'] for c in row.contacts]} and no "
                  f"PRICE_CHECK_CHANNEL - alert not sent:\n{text}", file=sys.stderr)
        for t in targets:
            try:
                self.slack.post(t, text)
            except RuntimeError as e:
                print(f"  Slack post to {t} failed: {e}", file=sys.stderr)
        return True


def make_find(llm, cfg, verbose=False):
    """find(row) for Checker: search the part at the row's quantity, always reading its link."""
    def find(row):
        extra = [{"title": row.name or row.part_number, "url": row.link}] if row.link else []
        return scraper2.find(row.part_number, row.units, llm, pool=cfg.pool, extra=extra,
                             verbose=verbose)[2]
    return find


def _page_id(s):
    """A Notion page id from an id or a notion.so URL (".../Title-<32 hex>?pvs=4")."""
    tail = urlparse(s).path.rstrip("/").rsplit("/", 1)[-1] if "://" in s else s.strip()
    m = re.search(r"([0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
                  tail, re.I)
    return m.group(1) if m else s


def main():
    p = argparse.ArgumentParser(description="Price-check Purchasing rows and DM the orderer "
                                            "when the price moved.")
    which = p.add_mutually_exclusive_group(required=True)
    which.add_argument("--queue", action="store_true",
                       help="check the next PRICE_CHECK_BATCH rows PERBot queued (the workflow)")
    which.add_argument("--enqueue", metavar="ROW", help="queue a Purchasing page (Notion URL or id)")
    which.add_argument("--row", help="check this Purchasing page now (Notion URL or id)")
    which.add_argument("--all", action="store_true", help="sweep every open row, oldest first")
    p.add_argument("--limit", type=int, help="at most N rows (--queue: default PRICE_CHECK_BATCH)")
    p.add_argument("--dry-run", action="store_true",
                   help="print alerts instead of DMing; nothing written to Notion")
    p.add_argument("--provider", choices=list(scraper2.PROVIDERS), help="LLM provider")
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
        llm = scraper2.LLM(a.provider, a.model, a.rpm, a.verbose)
    except scraper2.LLMError as e:
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

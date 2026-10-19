"""PERBot's Job Log in Notion: alert dedup (claim_once) and the queue of rows to check.

A queued row is a Job Log entry "price-check-queue <page id>" with Job = price-check-queue,
the same entry PERBot's /parts writes. Once checked, Job becomes price-check-done and the
entry stays as the "already seen" record, so a row is never queued twice.
"""
import sys
from datetime import datetime, timedelta, timezone

from .notion import open_filter, plain_text

QUEUE_JOB = "price-check-queue"
DONE_JOB = "price-check-done"


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
    pages = notion.query(cfg.purchasing_ds, {"and": open_filter(cfg) + [
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
        key = plain_text((e.get("properties") or {}).get("Run key"))
        if key.startswith(QUEUE_JOB + " "):
            out.append((e["id"], key[len(QUEUE_JOB) + 1:].strip()))
    return out

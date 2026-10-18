"""
Purchasing price check: re-price new Purchasing (PEFS) rows in Notion and DM the orderer
on Slack when the price moved.

    listener.py    the app: Notion webhooks -> settle -> queue -> one worker checks rows
    check.py       Checker (check one row, drain the queue) and the manual CLI
    comparison.py  the row's Unit cost vs. the prices found (Finding)
    alert.py       the Slack message of an alert (text + Use / Keep buttons)
    notion.py      Notion client, Purchasing columns and rows
    joblog.py      PERBot's Job Log: the queue and alert dedup
    ledger.py      the savings ledger (💸 Price Check Savings): one row per alert
    slack.py       Slack client and Notion person -> Slack user matching
    config.py      settings from the environment
    api.py         HTTP retries when Notion or Slack rate limit

Run from the project root:

    python -m pricecheck                     # the listener (what Render runs)
    python -m pricecheck.check --row <url>   # check one row now
"""
from importlib import import_module
from typing import Any

# Public names -> the module that defines them, imported on first use (PEP 562) so
# `python -m pricecheck.check` doesn't import its own module twice.
_EXPORTS = {
    # running it
    "Checker": "check", "make_find": "check", "Config": "config",
    # comparing and alerting
    "Finding": "comparison", "compare": "comparison", "format_alert": "alert", "money": "alert",
    "alert_buttons": "alert", "alert_blocks": "alert", "alert_links": "alert",
    # the savings ledger
    "ledger_props": "ledger", "log_alert": "ledger",
    # Notion
    "Notion": "notion", "Row": "notion", "parse_row": "notion", "skip_reason": "notion",
    "open_rows": "notion",
    # the Job Log queue
    "claim_once": "joblog", "enqueue": "joblog", "discover": "joblog", "queued": "joblog",
    "mark_done": "joblog",
    # Slack
    "Slack": "slack", "slack_user": "slack", "match_by_name": "slack",
}
__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(f".{_EXPORTS[name]}", __name__), name)


def __dir__() -> list[str]:
    return sorted([*globals(), *__all__])

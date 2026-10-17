"""HTTP calls to Notion and Slack, retried when rate limited."""
import sys
import time

TIMEOUT = 30
# Notion and Slack rate limits are shared with PERBot (same tokens). A 429 waits for
# Retry-After (capped) and tries again, so an alert isn't lost to a busy moment.
RATE_LIMIT_RETRIES = 3
MAX_RETRY_WAIT = 30


def send(fn, *args, **kw):
    """Make an HTTP call, retrying HTTP 429 after its Retry-After (seconds, capped)."""
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        r = fn(*args, **kw)
        if r.status_code != 429 or attempt == RATE_LIMIT_RETRIES:
            return r
        try:
            wait = float(r.headers.get("Retry-After") or 2 ** attempt)
        except ValueError:
            wait = 2 ** attempt
        print(f"rate limited, retrying in {min(wait, MAX_RETRY_WAIT):g}s", file=sys.stderr)
        time.sleep(min(wait, MAX_RETRY_WAIT))

"""Settings, read from the environment (the same names PERBot uses)."""
import os
import sys
from dataclasses import dataclass

# PERBot's defaults (PERBot/src/config.ts), so both apps point at the same databases.
DEFAULT_PURCHASING_DS = "39e560bc-c039-83db-9f16-076cb18f7d7b"
DEFAULT_JOB_LOG_DS = "db0c6a0d-e10b-4edf-97fb-6f381d15a465"
# 💸 Price Check Savings (under Purchasing — PEFS); PERBot reads and updates it too.
DEFAULT_LEDGER_DS = "dd1818e3-82cd-461b-8071-12af3b0f1341"


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
    ledger_ds: str = DEFAULT_LEDGER_DS

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
            # Set but empty turns the savings ledger off.
            ledger_ds=os.getenv("PRICE_CHECK_LEDGER_DS_ID", DEFAULT_LEDGER_DS).strip(),
        )

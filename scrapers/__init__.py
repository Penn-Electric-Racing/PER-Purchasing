"""
Part search: find where to buy a part and what an order would cost.

    websearch.py      web search backends (Brave, Serper, Google CSE, DuckDuckGo) and page fetching
    distributors.py   Mouser and DigiKey APIs (exact prices, stock, minimum order)
    regex_scraper.py  reads each page with JSON-LD / meta tags / regex (CLI)
    llm_scraper.py    reads each page with an LLM (CLI); find() is the price check's search
    ranking.py        scores the options (Weights) and picks the best (Scorer)
    env.py            loads API keys from .env
    listing.py        type aliases: Listing (one purchase option), PriceBreaks

Run a scraper from the project root:

    python -m scrapers.llm_scraper "LM7805" -q 50
    python -m scrapers.regex_scraper "LM7805" -q 50
"""
from importlib import import_module
from typing import Any

# Public names -> the module that defines them. They are imported on first use (PEP 562),
# so `python -m scrapers.llm_scraper` doesn't import its own module twice.
_EXPORTS = {
    # search
    "search": "websearch", "BackendError": "websearch",
    # distributor APIs
    "lookup": "distributors", "APIError": "distributors",
    # LLM scraper
    "find": "llm_scraper", "LLM": "llm_scraper", "LLMError": "llm_scraper",
    "PROVIDERS": "llm_scraper",
    # regex scraper
    "enrich": "regex_scraper", "part_info": "regex_scraper", "apply_qty": "regex_scraper",
    # ranking
    "Scorer": "ranking", "Weights": "ranking",
    # setup
    "load_env": "env",
}
__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(f".{_EXPORTS[name]}", __name__), name)


def __dir__() -> list[str]:
    return sorted([*globals(), *__all__])

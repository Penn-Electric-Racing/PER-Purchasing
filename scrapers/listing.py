"""Type aliases for the data the scrapers pass around."""
from typing import Any

# One purchase option: a search result plus everything read about it (title, url, snippet,
# cost, currency, supplier, stock, shipping, price_breaks, ...), then the quantity maths
# (unit_cost, subtotal, ...) and the ranking (score, notes, ...). See regex_scraper._blank_info.
Listing = dict[str, Any]

# Quantity price breaks, lowest quantity first: [(min quantity, unit price), ...].
PriceBreaks = list[tuple[int, float]]

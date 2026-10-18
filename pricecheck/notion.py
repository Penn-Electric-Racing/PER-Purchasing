"""The Purchasing (PEFS) database: a small Notion client, its columns, and its rows."""
import re
from dataclasses import dataclass, field
from typing import Any

import requests

from .api import TIMEOUT, send
from .config import Config

# A Notion API object (page, data source, query filter...) as parsed JSON.
Page = dict[str, Any]
# A page's properties: {column name: Notion property value}.
Properties = dict[str, Any]

NOTION_API = "https://api.notion.com/v1"

# Purchasing columns (PERBot/src/benbuys/notion.ts, partFinder/notion.ts).
COL_PART = "Cat. or model no."
COL_QTY = "Quant."
COL_UNIT_COST = "Unit cost"
COL_LINK = "Link to product webpage"
COL_CONTACT = "Point of contact"
COL_NOTES = "Notes (internal)"
COL_SEASON = "Season"
COL_EXPORTED = "Exported?"


# ---------------------------------------------------------------- Notion
class Notion:
    """The few Notion API calls this job needs (data sources API, as PERBot uses)."""

    def __init__(self, token: str, version: str) -> None:
        self.headers = {"Authorization": f"Bearer {token}", "Notion-Version": version,
                        "Content-Type": "application/json"}

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Page:
        r = send(requests.request, method, NOTION_API + path, headers=self.headers, json=body,
                  timeout=TIMEOUT)
        if r.status_code != 200:
            raise RuntimeError(f"Notion {method} {path}: HTTP {r.status_code} {r.text[:300]}")
        return r.json()

    def query(self, data_source_id: str, filter_: dict[str, Any] | None = None,
              page_size: int = 100, sorts: list[dict[str, Any]] | None = None,
              limit: int | None = None) -> list[Page]:
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

    def data_source(self, data_source_id: str) -> Page:
        """The data source object: its "properties" (column name -> {"id", "type", ...}) and
        "parent" (the database it belongs to)."""
        return self._call("GET", f"/data_sources/{data_source_id}")

    def page(self, page_id: str) -> Page:
        return self._call("GET", f"/pages/{page_id}")

    def update(self, page_id: str, properties: Properties) -> Page:
        return self._call("PATCH", f"/pages/{page_id}", {"properties": properties})

    def create(self, data_source_id: str, properties: Properties) -> Page:
        return self._call("POST", "/pages", {
            "parent": {"type": "data_source_id", "data_source_id": data_source_id},
            "properties": properties})

    def users(self) -> dict[str, str | None]:
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
    contacts: list[dict[str, Any]] = field(default_factory=list)  # [{"id", "name", "email"}]
    season: str = ""
    exported: bool = False
    parent_ds: str = ""   # the data source the page lives in
    # /parts writes pack listings as Quant. = packs and Unit cost = price per pack, and
    # says "Sold in packs of N" in the notes; prices are compared per unit.
    pack_size: int = 1

    @property
    def units(self) -> int:
        return self.quantity * self.pack_size

    @property
    def unit_price(self) -> float:
        return self.unit_cost / self.pack_size


PACK_NOTE_RE = re.compile(r"Sold in packs of (\d+)")


def plain_text(prop: dict[str, Any] | None) -> str:
    return "".join(t.get("plain_text", "") for t in (prop or {}).get("rich_text")
                   or (prop or {}).get("title") or []).strip()


def parse_row(page: Page) -> "Row":
    """A Purchasing page as a Row (field meanings as in PERBot's parsePurchasingRow)."""
    p = page.get("properties", {})
    title: dict[str, Any] = next((v for v in p.values() if v.get("type") == "title"), {})
    return Row(
        id=page["id"], url=page.get("url", ""), name=plain_text(title),
        part_number=plain_text(p.get(COL_PART)),
        quantity=int((p.get(COL_QTY) or {}).get("number") or 0),
        unit_cost=float((p.get(COL_UNIT_COST) or {}).get("number") or 0),
        link=(p.get(COL_LINK) or {}).get("url") or "",
        contacts=[{"id": u.get("id"), "name": u.get("name") or "",
                   "email": (u.get("person") or {}).get("email")}
                  for u in (p.get(COL_CONTACT) or {}).get("people") or []],
        pack_size=int(m.group(1)) if (m := PACK_NOTE_RE.search(plain_text(p.get(COL_NOTES)))) else 1,
        season=((p.get(COL_SEASON) or {}).get("select") or {}).get("name") or "",
        exported=bool((p.get(COL_EXPORTED) or {}).get("checkbox")),
        parent_ds=(page.get("parent") or {}).get("data_source_id") or "",
    )


def _same_id(a: str, b: str) -> bool:
    return a.replace("-", "").lower() == b.replace("-", "").lower()


def skip_reason(row: Row, cfg: Config) -> str | None:
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


def open_filter(cfg: Config) -> list[dict[str, Any]]:
    """This season's not-yet-ordered rows with enough filled in to price-check."""
    return [
        {"property": COL_SEASON, "select": {"equals": cfg.season}},
        {"property": COL_EXPORTED, "checkbox": {"equals": False}},
        {"property": COL_PART, "rich_text": {"is_not_empty": True}},
        {"property": COL_QTY, "number": {"greater_than": 0}},
        {"property": COL_UNIT_COST, "number": {"greater_than": 0}},
    ]


def open_rows(notion: Notion, cfg: Config, limit: int | None = None) -> list[Row]:
    """Every open row (oldest first), for a manual sweep."""
    return [parse_row(pg) for pg in notion.query(cfg.purchasing_ds, {"and": open_filter(cfg)},
                                                 sorts=[{"timestamp": "created_time",
                                                         "direction": "ascending"}],
                                                 limit=limit)]

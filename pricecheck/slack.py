"""Slack: post the alert, and find the Slack user for a Notion person."""
import re
import unicodedata
from typing import Any, Callable

import requests

from .api import TIMEOUT, send

SLACK_API = "https://slack.com/api"

# A Slack member: (user id, [real name, display name, handle], email or None).
Member = tuple[str, list[str], str | None]


class Slack:
    def __init__(self, token: str) -> None:
        self.headers = {"Authorization": f"Bearer {token}"}
        self._directory: list[Member] | None = None

    def _call(self, method: str, http: str = "post", **params: Any) -> dict[str, Any]:
        fn = requests.post if http == "post" else requests.get
        kw = {"json": params} if http == "post" else {"params": params}
        r = send(fn, f"{SLACK_API}/{method}", headers=self.headers, timeout=TIMEOUT, **kw)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Slack {method}: {data.get('error', r.status_code)}")
        return data

    def post(self, channel: str, text: str, blocks: list[dict[str, Any]] | None = None) -> None:
        # A user id as channel DMs that user (chat:write only, as PERBot's jobs do). With
        # blocks, `text` is the notification / fallback.
        params: dict[str, Any] = {"blocks": blocks} if blocks else {}
        self._call("chat.postMessage", channel=channel, text=text, unfurl_links=False, **params)

    def lookup_email(self, email: str) -> str | None:
        try:
            return self._call("users.lookupByEmail", http="get", email=email)["user"]["id"]
        except (RuntimeError, KeyError):
            return None

    def directory(self) -> list[Member]:
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


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def match_by_name(name: str, directory: list[Member]) -> str | None:
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


def slack_user(slack: Slack, contact: dict[str, Any],
               notion_emails: Callable[[], dict[str, str | None]]) -> str | None:
    """Notion person -> Slack user id: email first, then a conservative name match."""
    email = (contact.get("email") or notion_emails().get(contact["id"]) or "").lower()
    if email:
        hit = next((uid for uid, _, e in slack.directory() if e == email), None) \
            or slack.lookup_email(email)
        if hit:
            return hit
    return match_by_name(contact.get("name", ""), slack.directory())

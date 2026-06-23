"""Hacker News endpoint map (reference-pack introspection stub).

Hacker News has no auth-gated object catalog, so this reference pack documents
its handful of endpoints here instead of shipping live ``*_list_objects`` /
``*_describe_object`` tools (those land with the curated first wave). This file
is inert data — the pack loader records it but never imports it; ``SKILL.md``
points the agent here when authoring against HN.

The Hacker News API (https://github.com/HackerNews/API) is public, read-only,
and unauthenticated. Base URL: ``https://hacker-news.firebaseio.com/v0``.
"""

from __future__ import annotations

# A small, documented endpoint catalog. Keep in sync with scripts/__init__.py.
HN_ENDPOINTS: dict[str, dict[str, str]] = {
    "topstories": {
        "path": "/topstories.json",
        "returns": "Up to 500 item ids, ranked. Fetch each via /item/<id>.json.",
    },
    "newstories": {
        "path": "/newstories.json",
        "returns": "Up to 500 newest item ids.",
    },
    "item": {
        "path": "/item/<id>.json",
        "returns": "One item: story | comment | job | poll | pollopt "
        "(fields: id, type, by, time, title, url, score, descendants, kids, text).",
    },
    "user": {
        "path": "/user/<id>.json",
        "returns": "One user: id, created, karma, about, submitted (item ids).",
    },
    "maxitem": {
        "path": "/maxitem.json",
        "returns": "The current largest item id (walk backwards to sweep recent items).",
    },
}


def list_objects() -> list[str]:
    """Stub: the queryable HN object names (the endpoint keys)."""
    return sorted(HN_ENDPOINTS)


def describe_object(name: str) -> dict[str, str]:
    """Stub: describe one HN endpoint (path + what it returns)."""
    return HN_ENDPOINTS.get(name, {"path": "", "returns": f"Unknown HN object {name!r}."})

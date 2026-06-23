"""Hacker News API dlt source (Carve reference curated source).

Inert on disk: the pack loader records this path but never imports it. It is
copied into ``el/<component>/`` by ``dlt_library.copy``, which prepends a Carve
provenance header and substitutes the ``__DESTINATION__`` / ``__SCHEMA__``
placeholders below. The Hacker News API (https://github.com/HackerNews/API) is
public and unauthenticated, so a copied pack runs immediately against DuckDB.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import dlt
from dlt.sources.helpers import requests

# Substituted by dlt_library.copy from the `customization` it is passed; the
# defaults make the pack runnable as-is against the creds-free DuckDB substrate.
DESTINATION = "__DESTINATION__"
SCHEMA = "__SCHEMA__"

API_BASE = "https://hacker-news.firebaseio.com/v0"


@dlt.source(name="hacker_news")
def hacker_news(max_items: int = 50) -> Any:
    """The Hacker News source: top stories + arbitrary items by id."""
    return [top_stories(max_items=max_items), items]


@dlt.resource(name="top_stories", write_disposition="replace")
def top_stories(max_items: int = 50) -> Iterator[dict[str, Any]]:
    """Yield the current top-story item objects (capped at ``max_items``)."""
    ids = requests.get(f"{API_BASE}/topstories.json").json() or []
    for item_id in ids[:max_items]:
        item = requests.get(f"{API_BASE}/item/{item_id}.json").json()
        if item:
            yield item


@dlt.resource(name="items", write_disposition="append")
def items(item_ids: list[int] | None = None) -> Iterator[dict[str, Any]]:
    """Yield arbitrary items (stories/comments/jobs/polls) fetched by id."""
    for item_id in item_ids or []:
        item = requests.get(f"{API_BASE}/item/{item_id}.json").json()
        if item:
            yield item


def run() -> None:
    """Run the pipeline into the customized destination/schema (DuckDB by default)."""
    pipeline = dlt.pipeline(
        pipeline_name="hacker_news",
        destination=DESTINATION if DESTINATION != "__" + "DESTINATION__" else "duckdb",
        dataset_name=SCHEMA if SCHEMA != "__" + "SCHEMA__" else "hacker_news",
    )
    pipeline.run(hacker_news())


if __name__ == "__main__":
    run()

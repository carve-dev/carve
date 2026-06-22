"""Web tools: ``web_fetch`` and ``web_search``, bounded.

These let an agent read dlt/dbt/source-API documentation. Both delegate
to an **injected backend** (a fetcher / searcher callable) rather than
hardcoding an HTTP client or a search provider — that keeps the tools
testable without a network and lets the host wire whichever provider it
has configured. Output is capped before it enters the transcript.

If no backend is injected the tool returns an actionable ``tool_error``
("web access is not configured") rather than failing the run — an agent
that can't fetch docs should fall back to what it knows, not crash.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from carve.core.agents.tools import Tool, ToolExecutionError, ToolInput, ToolResult

# Injected backends. A fetcher takes a URL and returns page text; a
# searcher takes a query and returns a list of {title, url, snippet}.
Fetcher = Callable[[str], str]
Searcher = Callable[[str], list[dict[str, str]]]

_MAX_FETCH_CHARS = 20_000
_MAX_SEARCH_RESULTS = 10


WEB_FETCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "Absolute http(s) URL to fetch."},
    },
    "required": ["url"],
}

WEB_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Search query."},
    },
    "required": ["query"],
}


def make_web_fetch_tool(fetcher: Fetcher | None = None) -> Tool:
    """Build a ``web_fetch`` tool over an injected ``fetcher``."""

    def _execute(input_: ToolInput) -> ToolResult:
        url = input_.get("url")
        if not isinstance(url, str) or not url:
            raise ToolExecutionError("`url` must be a non-empty string.")
        if not (url.startswith("http://") or url.startswith("https://")):
            raise ToolExecutionError("`url` must be an absolute http(s) URL.")
        if fetcher is None:
            raise ToolExecutionError(
                "web_fetch is not configured in this runtime; proceed "
                "without fetching or ask the user for the content."
            )
        try:
            text = fetcher(url)
        except Exception as exc:  # surfaced as a recoverable tool error
            raise ToolExecutionError(f"web_fetch failed for {url}: {exc}") from exc
        truncated = len(text) > _MAX_FETCH_CHARS
        return {
            "url": url,
            "content": text[:_MAX_FETCH_CHARS],
            "truncated": truncated,
        }

    return Tool(
        name="web_fetch",
        description=(
            "Fetch the text content of an http(s) URL — use for reading "
            "dlt/dbt or source-API documentation. Output is capped."
        ),
        input_schema=WEB_FETCH_SCHEMA,
        executor=_execute,
    )


def make_web_search_tool(searcher: Searcher | None = None) -> Tool:
    """Build a ``web_search`` tool over an injected ``searcher``."""

    def _execute(input_: ToolInput) -> ToolResult:
        query = input_.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ToolExecutionError("`query` must be a non-empty string.")
        if searcher is None:
            raise ToolExecutionError(
                "web_search is not configured in this runtime; proceed without searching."
            )
        try:
            results = searcher(query)
        except Exception as exc:  # surfaced as a recoverable tool error
            raise ToolExecutionError(f"web_search failed: {exc}") from exc
        bounded = list(results)[:_MAX_SEARCH_RESULTS]
        return {"query": query, "results": bounded, "count": len(bounded)}

    return Tool(
        name="web_search",
        description=(
            "Search the web for documentation and references. Returns a "
            "bounded list of {title, url, snippet}."
        ),
        input_schema=WEB_SEARCH_SCHEMA,
        executor=_execute,
    )


__all__ = [
    "WEB_FETCH_SCHEMA",
    "WEB_SEARCH_SCHEMA",
    "Fetcher",
    "Searcher",
    "make_web_fetch_tool",
    "make_web_search_tool",
]

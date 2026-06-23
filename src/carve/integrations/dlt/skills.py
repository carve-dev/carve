"""Callable Tools the DLT engineer is granted (the lightweight, non-warehouse skills).

These are the domain skills from the dlt-engineer spec that need only the project
tree or the network — so they're plain callable :class:`~carve.core.agents.tools.Tool`s
bound through the harness grant→executor binder (injected into the engineer's runtime
tool set), not warehouse-coupled ``@skill`` functions. This module ships:

- ``existing_dlt_inspect`` — read the project's `el/` components (+ provenance) for
  brownfield pattern discovery.
- ``rest_api_explore`` — a bounded HTTP probe for unfamiliar REST APIs.

`dbt_source_lookup` (needs dbt-project resolution) and `dlt_library` (needs the curated
source corpus) ship in their own units.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from carve.core.agents.tools import Tool, ToolExecutionError, ToolInput, ToolResult
from carve.integrations.provenance import is_carve_generated

# ---------------------------------------------------------------------------
# existing_dlt_inspect
# ---------------------------------------------------------------------------

_INSPECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "op": {
            "type": "string",
            "enum": ["list", "read"],
            "description": "list the el/ components, or read one component's files.",
        },
        "name": {"type": "string", "description": "Component name (for op=read)."},
    },
    "required": ["op"],
}


def make_existing_dlt_inspect_tool(
    project_dir: Path, *, name: str = "existing_dlt_inspect"
) -> Tool:
    """Build the ``existing_dlt_inspect`` tool over ``project_dir``'s `el/` tree."""
    el_dir = (project_dir / "el").resolve()

    def _list() -> ToolResult:
        if not el_dir.is_dir():
            return {"pipelines": []}
        pipelines = []
        for child in sorted(el_dir.iterdir()):
            init = child / "__init__.py"
            if not (child.is_dir() and init.is_file()):
                continue
            try:
                content = init.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                content = ""
            pipelines.append(
                {
                    "name": child.name,
                    "provenance": "carve-generated"
                    if is_carve_generated(content)
                    else "user-authored",
                }
            )
        return {"pipelines": pipelines}

    def _read(component: str) -> ToolResult:
        comp_dir = (el_dir / component).resolve()
        # Path-confinement: never read outside el/<name>/.
        if el_dir not in comp_dir.parents and comp_dir != el_dir:
            raise ToolExecutionError(f"Component {component!r} is outside the el/ tree.")
        if not comp_dir.is_dir():
            raise ToolExecutionError(f"No el/ component named {component!r}.")
        files: dict[str, str] = {}
        for fname in ("__init__.py", "requirements.txt"):
            fpath = comp_dir / fname
            if fpath.is_file():
                try:
                    files[fname] = fpath.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    raise ToolExecutionError(f"Could not read {fname}: {exc}") from exc
        return {"name": component, "files": files}

    def _execute(input_: ToolInput) -> ToolResult:
        op = input_.get("op")
        if op == "list":
            return _list()
        if op == "read":
            component = input_.get("name")
            if not isinstance(component, str) or not component.strip():
                raise ToolExecutionError("op=read requires a 'name'.")
            return _read(component.strip())
        raise ToolExecutionError(f"Unknown existing_dlt_inspect op {op!r}; use list/read.")

    return Tool(
        name=name,
        description=(
            "Inspect the project's existing dlt components under el/: list them with "
            "provenance (carve-generated vs user-authored), or read one component's "
            "__init__.py + requirements.txt to match its patterns."
        ),
        input_schema=_INSPECT_SCHEMA,
        executor=_execute,
    )


# ---------------------------------------------------------------------------
# rest_api_explore
# ---------------------------------------------------------------------------

# A probe fetcher: (url, method) -> (status_code, body). Injected so tests run
# without a network; the default uses urllib with a per-request timeout.
RestFetcher = Callable[[str, str], "tuple[int, str]"]

DEFAULT_MAX_REQUESTS = 20
DEFAULT_TIMEOUT_S = 10
DEFAULT_MAX_BODY = 50 * 1024
_READ_METHODS = ("OPTIONS", "GET")

_EXPLORE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "base_url": {"type": "string", "description": "The API base URL (https)."},
        "endpoints": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional relative endpoints to sample (GET).",
        },
    },
    "required": ["base_url"],
}


def _default_rest_fetcher(timeout: int, max_body: int) -> RestFetcher:
    def _fetch(url: str, method: str) -> tuple[int, str]:
        import urllib.request

        request = urllib.request.Request(url, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                body = resp.read(max_body + 1).decode("utf-8", "replace")
                return resp.status, body
        except Exception as exc:  # surface as a probe error, not a crash
            return 0, f"{type(exc).__name__}: {exc}"

    return _fetch


def make_rest_api_explore_tool(
    *,
    fetcher: RestFetcher | None = None,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    max_body: int = DEFAULT_MAX_BODY,
    name: str = "rest_api_explore",
) -> Tool:
    """Build the bounded ``rest_api_explore`` probe.

    Read-only (OPTIONS/GET), capped at ``max_requests`` requests, each body
    truncated to ``max_body`` bytes. Never issues a write verb. Every request is
    returned in the result for audit (the dlt-security reviewer checks bounds).
    """
    fetch = fetcher or _default_rest_fetcher(timeout_s, max_body)

    def _execute(input_: ToolInput) -> ToolResult:
        base_url = input_.get("base_url")
        if not isinstance(base_url, str) or not base_url.lower().startswith(
            ("http://", "https://")
        ):
            raise ToolExecutionError("rest_api_explore requires an http(s) 'base_url'.")
        base = base_url.rstrip("/")
        endpoints = input_.get("endpoints") or []
        if not isinstance(endpoints, list):
            raise ToolExecutionError("'endpoints' must be a list of relative paths.")

        # Probe plan: OPTIONS /, then schema discovery, then sampled endpoints —
        # bounded by max_requests.
        plan: list[tuple[str, str]] = [(base + "/", "OPTIONS")]
        plan += [(f"{base}/{p}", "GET") for p in ("openapi.json", "swagger.json")]
        plan += [(f"{base}/{str(e).lstrip('/')}", "GET") for e in endpoints]

        results: list[dict[str, Any]] = []
        for url, method in plan[:max_requests]:
            if method not in _READ_METHODS:  # defensive — the plan only ever has reads
                continue
            status, body = fetch(url, method)
            truncated = len(body) > max_body
            results.append(
                {
                    "url": url,
                    "method": method,
                    "status": status,
                    "body": body[:max_body],
                    "truncated": truncated,
                }
            )
        return {
            "base_url": base,
            "requests_made": len(results),
            "request_cap": max_requests,
            "results": results,
        }

    return Tool(
        name=name,
        description=(
            "Bounded read-only probe of an unfamiliar REST API: OPTIONS /, then "
            "openapi/swagger discovery, then sampled GET endpoints. Capped requests, "
            "truncated bodies, never a write verb. Use to discover endpoints/shape "
            "before authoring a dlt REST source."
        ),
        input_schema=_EXPLORE_SCHEMA,
        executor=_execute,
    )


__all__ = [
    "RestFetcher",
    "make_existing_dlt_inspect_tool",
    "make_rest_api_explore_tool",
]

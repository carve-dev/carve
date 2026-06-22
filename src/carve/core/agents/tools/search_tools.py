"""File-search tools: ``glob`` and ``grep``, bounded + secret-denied.

Both tools operate strictly under the project root, return bounded
result counts, and refuse to surface secret-bearing files: any path
matching the shared secret deny-list (`secrets_denylist`) is skipped
from results and, for ``grep``, never read. This is what stops even a
``read_only`` explorer from leaking ``.env`` / ``secrets.toml`` content
into a search answer.

Implementation is pure ``pathlib`` + a line scan — no subprocess (so no
``rg``/``grep`` binary dependency, and no second sandbox surface). The
caps keep both tools cheap on a large tree.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from carve.core.agents.tools import Tool, ToolExecutionError, ToolInput, ToolResult
from carve.core.agents.tools.secrets_denylist import is_secret_path

# Result + scan caps. `glob` returns at most this many paths; `grep`
# returns at most this many matching lines and scans at most this many
# files. Files larger than the byte cap are skipped (binaries / huge
# artifacts aren't useful grep targets and waste the budget).
_MAX_GLOB_RESULTS = 500
_MAX_GREP_MATCHES = 200
_MAX_GREP_FILES = 2_000
_MAX_FILE_BYTES = 2_000_000


def _resolve_dir(project_root: Path, sub: str | None) -> Path:
    """Resolve an optional subdir under the root, asserting containment."""
    if not sub:
        return project_root
    candidate = (project_root / sub).resolve()
    try:
        candidate.relative_to(project_root)
    except ValueError as exc:
        raise ToolExecutionError(f"Path {sub!r} is outside the project directory.") from exc
    return candidate


# ---------------------------------------------------------------------------
# glob
# ---------------------------------------------------------------------------


GLOB_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": "Glob pattern, e.g. '**/*.py' or 'el/*/main.py'.",
        },
        "path": {
            "type": "string",
            "description": "Optional subdirectory to search under (project-relative).",
        },
    },
    "required": ["pattern"],
}


def make_glob_tool(project_dir: Path) -> Tool:
    """Build a ``glob`` tool bound to ``project_dir``."""
    project_root = project_dir.resolve()

    def _execute(input_: ToolInput) -> ToolResult:
        pattern = input_.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ToolExecutionError("`pattern` must be a non-empty string.")
        sub = input_.get("path")
        base = _resolve_dir(project_root, sub if isinstance(sub, str) else None)

        matches: list[str] = []
        truncated = False
        for match in sorted(base.glob(pattern)):
            if not match.is_file():
                continue
            if is_secret_path(match):
                continue  # secret files never surface in results
            try:
                rel = match.resolve().relative_to(project_root)
            except ValueError:
                continue  # escaped the root via symlink — skip
            matches.append(str(rel))
            if len(matches) >= _MAX_GLOB_RESULTS:
                truncated = True
                break
        return {"matches": matches, "count": len(matches), "truncated": truncated}

    return Tool(
        name="glob",
        description=(
            "Find files matching a glob pattern under the project root "
            "(e.g. '**/*.sql'). Returns project-relative paths, bounded "
            "in count. Secret files (.env, secrets.toml, *.pem) are never "
            "returned."
        ),
        input_schema=GLOB_SCHEMA,
        executor=_execute,
    )


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------


GREP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": "Regular expression to search for.",
        },
        "path": {
            "type": "string",
            "description": "Optional subdirectory to search under (project-relative).",
        },
        "glob": {
            "type": "string",
            "description": "Optional file glob to restrict the search (default '**/*').",
        },
    },
    "required": ["pattern"],
}


def make_grep_tool(project_dir: Path) -> Tool:
    """Build a ``grep`` tool bound to ``project_dir``."""
    project_root = project_dir.resolve()

    def _execute(input_: ToolInput) -> ToolResult:
        pattern = input_.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ToolExecutionError("`pattern` must be a non-empty string.")
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise ToolExecutionError(f"Invalid regex: {exc}") from exc

        sub = input_.get("path")
        base = _resolve_dir(project_root, sub if isinstance(sub, str) else None)
        file_glob = input_.get("glob")
        file_glob = file_glob if isinstance(file_glob, str) and file_glob else "**/*"

        results: list[dict[str, Any]] = []
        files_scanned = 0
        truncated = False
        for candidate in sorted(base.glob(file_glob)):
            if not candidate.is_file():
                continue
            if is_secret_path(candidate):
                continue  # never read a secret file, even to grep it
            files_scanned += 1
            if files_scanned > _MAX_GREP_FILES:
                truncated = True
                break
            try:
                if candidate.stat().st_size > _MAX_FILE_BYTES:
                    continue
                rel = candidate.resolve().relative_to(project_root)
            except (OSError, ValueError):
                continue
            try:
                text = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue  # binary / unreadable — skip
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    results.append({"path": str(rel), "line": lineno, "text": line[:500]})
                    if len(results) >= _MAX_GREP_MATCHES:
                        truncated = True
                        break
            if truncated:
                break

        return {"matches": results, "count": len(results), "truncated": truncated}

    return Tool(
        name="grep",
        description=(
            "Search file contents under the project root with a regular "
            "expression. Returns matching lines (path + line number), "
            "bounded in count. Secret files (.env, secrets.toml, *.pem) "
            "are never read."
        ),
        input_schema=GREP_SCHEMA,
        executor=_execute,
    )


__all__ = [
    "GLOB_SCHEMA",
    "GREP_SCHEMA",
    "make_glob_tool",
    "make_grep_tool",
]

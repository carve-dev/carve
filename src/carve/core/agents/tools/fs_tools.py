"""Terminal-grade file-mutation tools: ``create_file`` and ``edit``.

These replace the raw ``write_file`` tool for agents. They reuse the
shipped ``make_write_file_tool`` discipline — resolve the path, follow
symlinks, assert containment under the project root, and (when supplied)
assert membership in an explicit ``allowed_paths`` set *before* any disk
I/O — and add the two contracts the harness spec calls for:

* ``create_file`` makes a **new** file. It refuses to overwrite an
  existing one (``edit`` is the tool for changing existing files), which
  keeps "create" honest and prevents an accidental clobber.
* ``edit`` does an exact string replace and **re-reads the file at apply
  time**, verifying ``old_string`` still matches the on-disk bytes before
  writing. This closes the read-at-turn-2 / edit-at-turn-20 TOCTOU: a
  stale ``old_string`` (the file changed since the agent read it) fails
  loudly instead of silently corrupting. A non-unique match fails unless
  ``replace_all`` is set, which reports the replacement count.

Both call an injected ``on_change`` callback after a successful write so
the loop can keep its **harness-tracked** ``files_changed`` log — the
model never self-reports which files it touched.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from carve.core.agents.permissions.gate import is_write_path_allowed
from carve.core.agents.tools import Tool, ToolExecutionError, ToolInput, ToolResult

# Called with the project-relative path string after a successful write.
# The loop passes its per-run logger; tools that don't track changes pass
# a no-op.
ChangeSink = Callable[[str], None]


def _noop_sink(_path: str) -> None:
    return None


def _resolve_writable(
    project_root: Path,
    allowed_paths: frozenset[Path] | None,
    path: str,
) -> Path:
    """Resolve ``path`` and assert it is writable, else raise.

    The single source of truth for the write-path decision is
    :func:`is_write_path_allowed` (the gate's clamp): resolve (symlinks
    followed), require project-root containment, and — when
    ``allowed_paths`` is provided — require exact membership. This wrapper
    only adds the actionable :class:`ToolExecutionError` messages and the
    allow-list listing; it does not re-implement the check.
    """
    if not isinstance(path, str) or not path:
        raise ToolExecutionError("`path` must be a non-empty string.")
    candidate = (project_root / path).resolve()
    if not is_write_path_allowed(
        candidate, project_root=project_root, allowed_paths=allowed_paths
    ):
        if not _is_relative_to(candidate, project_root):
            raise ToolExecutionError(
                f"Path {path!r} is outside the project directory."
            )
        # In-tree but not on the explicit allow-list.
        permitted = sorted(
            str(p.relative_to(project_root))
            if _is_relative_to(p.resolve(), project_root)
            else str(p)
            for p in (allowed_paths or frozenset())
        )
        raise ToolExecutionError(
            f"Path {path!r} is not on the write allow-list. "
            f"Allowed: {permitted}"
        )
    return candidate


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# create_file
# ---------------------------------------------------------------------------


CREATE_FILE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Project-relative path for the NEW file (UTF-8).",
        },
        "content": {
            "type": "string",
            "description": "Full contents of the new file.",
        },
    },
    "required": ["path", "content"],
}


def make_create_file_tool(
    project_dir: Path,
    *,
    allowed_paths: frozenset[Path] | None = None,
    on_change: ChangeSink = _noop_sink,
) -> Tool:
    """Build a ``create_file`` tool bound to ``project_dir``.

    ``allowed_paths`` (optional) narrows writes to an exact resolved set;
    ``None`` means project-root containment only. ``on_change`` is called
    with the relative path after a successful create (the harness change
    log).
    """
    project_root = project_dir.resolve()

    def _execute(input_: ToolInput) -> ToolResult:
        path = input_.get("path")
        content = input_.get("content")
        if not isinstance(content, str):
            raise ToolExecutionError("`content` must be a string.")
        target = _resolve_writable(project_root, allowed_paths, path)  # type: ignore[arg-type]
        if target.exists():
            raise ToolExecutionError(
                f"File already exists: {path}. Use `edit` to change an "
                "existing file; `create_file` only creates new files."
            )
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise ToolExecutionError(f"Failed to create {path}: {exc}") from exc
        on_change(str(path))
        return {"path": path, "bytes_written": len(content.encode("utf-8"))}

    return Tool(
        name="create_file",
        description=(
            "Create a NEW file at a project-relative path with the given "
            "contents. Fails if the file already exists — use `edit` to "
            "modify existing files. Creates parent directories as needed."
        ),
        input_schema=CREATE_FILE_SCHEMA,
        executor=_execute,
    )


# ---------------------------------------------------------------------------
# edit
# ---------------------------------------------------------------------------


EDIT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Project-relative path of the file to edit.",
        },
        "old_string": {
            "type": "string",
            "description": (
                "Exact text to replace. Must match the on-disk bytes "
                "uniquely unless replace_all is set."
            ),
        },
        "new_string": {
            "type": "string",
            "description": "Text to substitute for old_string.",
        },
        "replace_all": {
            "type": "boolean",
            "default": False,
            "description": (
                "Replace every occurrence (and report the count) instead "
                "of requiring a unique match."
            ),
        },
    },
    "required": ["path", "old_string", "new_string"],
}


def make_edit_tool(
    project_dir: Path,
    *,
    allowed_paths: frozenset[Path] | None = None,
    on_change: ChangeSink = _noop_sink,
) -> Tool:
    """Build an ``edit`` tool with re-read-at-apply TOCTOU protection.

    The file is re-read inside the executor; ``old_string`` is matched
    against the *current* on-disk content, not whatever the agent saw
    earlier. A zero-match (stale edit) or — without ``replace_all`` — a
    multi-match fails with an actionable error and no write occurs.
    """
    project_root = project_dir.resolve()

    def _execute(input_: ToolInput) -> ToolResult:
        path = input_.get("path")
        old_string = input_.get("old_string")
        new_string = input_.get("new_string")
        replace_all = bool(input_.get("replace_all", False))
        if not isinstance(old_string, str):
            raise ToolExecutionError("`old_string` must be a string.")
        if not isinstance(new_string, str):
            raise ToolExecutionError("`new_string` must be a string.")
        if old_string == new_string:
            raise ToolExecutionError(
                "`old_string` and `new_string` are identical; nothing to do."
            )

        target = _resolve_writable(project_root, allowed_paths, path)  # type: ignore[arg-type]
        if not target.exists() or not target.is_file():
            raise ToolExecutionError(
                f"File not found: {path}. `edit` cannot create files; use "
                "`create_file` for new files."
            )

        # Re-read at apply time — the TOCTOU close. The match is against
        # current bytes, so an edit authored against a stale view fails.
        try:
            current = target.read_text(encoding="utf-8")
        except OSError as exc:
            raise ToolExecutionError(f"Failed to read {path}: {exc}") from exc

        count = current.count(old_string)
        if count == 0:
            raise ToolExecutionError(
                f"`old_string` not found in {path}. The file may have "
                "changed since you last read it; re-read it and retry."
            )
        if count > 1 and not replace_all:
            raise ToolExecutionError(
                f"`old_string` matched {count} times in {path}. Provide a "
                "larger, unique `old_string`, or set replace_all=true to "
                "replace every occurrence."
            )

        updated = current.replace(old_string, new_string)
        try:
            target.write_text(updated, encoding="utf-8")
        except OSError as exc:
            raise ToolExecutionError(f"Failed to write {path}: {exc}") from exc

        on_change(str(path))
        return {
            "path": path,
            "replacements": count if replace_all else 1,
            "bytes_written": len(updated.encode("utf-8")),
        }

    return Tool(
        name="edit",
        description=(
            "Replace an exact string in an existing file. Re-reads the "
            "file at apply time and verifies old_string still matches the "
            "current bytes before writing (so a stale edit fails loudly). "
            "A non-unique match fails unless replace_all is set, which "
            "replaces every occurrence and reports the count."
        ),
        input_schema=EDIT_SCHEMA,
        executor=_execute,
    )


__all__ = [
    "CREATE_FILE_SCHEMA",
    "EDIT_SCHEMA",
    "ChangeSink",
    "make_create_file_tool",
    "make_edit_tool",
]

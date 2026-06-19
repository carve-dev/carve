"""Parse ``carve/hooks.toml`` into typed :class:`HookSpec` entries.

The file format::

    [[hook]]
    on = "pre_tool"
    match = { tool = "bash", command = "git commit*" }
    run = "sqlfluff lint --dialect snowflake {changed_sql}"

    [[hook]]
    on = "on_run_failed"
    run = "notify-slack {pipeline} {error}"

Each ``[[hook]]`` table has:

* ``on`` — one of the :class:`HookEvent` values (validated; an unknown
  event is a :class:`HookConfigError`).
* ``run`` — the shell command (required).
* ``match`` — an optional ``{tool, command}`` filter (a glob on the
  command). Absent ⇒ the hook matches every event of that type.

TOML is parsed with the stdlib :mod:`tomllib` (no code execution). The
parse is **fail-closed**: any structural problem raises and yields no
specs (the caller treats the whole file as invalid rather than running a
partially-parsed hook set).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from carve.core.hooks.events import HookEvent


class HookConfigError(Exception):
    """Raised when ``hooks.toml`` is malformed (fail-closed, no partial)."""


@dataclass(frozen=True)
class HookMatch:
    """An optional ``{tool, command}`` filter on a hook.

    * ``tool`` — only fire when the tool name equals this (``None`` ⇒ any
      tool).
    * ``command`` — an ``fnmatch`` glob the ``bash`` command must match
      (``None`` ⇒ any command). Only meaningful for ``bash``.
    """

    tool: str | None = None
    command: str | None = None


@dataclass(frozen=True)
class HookSpec:
    """One parsed ``[[hook]]`` entry."""

    event: HookEvent
    run: str
    match: HookMatch = HookMatch()


def load_hooks_config(path: Path) -> list[HookSpec]:
    """Load + parse ``hooks.toml`` at ``path``.

    A missing file is **not** an error — it means "no hooks" (returns an
    empty list). A present-but-malformed file raises
    :class:`HookConfigError`.
    """
    if not path.is_file():
        return []
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise HookConfigError(f"Cannot read {path}: {exc}") from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise HookConfigError(f"Malformed TOML in {path}: {exc}") from exc
    return parse_hooks_config(data, source=path)


def parse_hooks_config(
    data: dict[str, Any], *, source: Path | None = None
) -> list[HookSpec]:
    """Validate a parsed-TOML dict into :class:`HookSpec` entries.

    Split from :func:`load_hooks_config` so tests can drive it with an
    in-memory dict. Raises :class:`HookConfigError` on any structural
    problem (unknown event, missing ``run``, wrong shapes).
    """
    where = f" in {source}" if source is not None else ""
    hooks_raw = data.get("hook", [])
    if not isinstance(hooks_raw, list):
        raise HookConfigError(f"`hook` must be an array of tables{where}.")

    specs: list[HookSpec] = []
    for index, entry in enumerate(hooks_raw):
        specs.append(_parse_one(entry, index, where))
    return specs


def _parse_one(entry: Any, index: int, where: str) -> HookSpec:
    if not isinstance(entry, dict):
        raise HookConfigError(f"hook #{index} must be a table{where}.")

    on = entry.get("on")
    if not isinstance(on, str):
        raise HookConfigError(f"hook #{index} is missing a string `on`{where}.")
    try:
        event = HookEvent(on)
    except ValueError as exc:
        raise HookConfigError(
            f"hook #{index} has unknown event {on!r}{where}; expected one of "
            f"{[e.value for e in HookEvent]}."
        ) from exc

    run = entry.get("run")
    if not isinstance(run, str) or not run.strip():
        raise HookConfigError(
            f"hook #{index} ({on}) needs a non-empty string `run`{where}."
        )

    match = _parse_match(entry.get("match"), index, where)
    return HookSpec(event=event, run=run.strip(), match=match)


def _parse_match(raw: Any, index: int, where: str) -> HookMatch:
    if raw is None:
        return HookMatch()
    if not isinstance(raw, dict):
        raise HookConfigError(f"hook #{index} `match` must be a table{where}.")
    tool = raw.get("tool")
    command = raw.get("command")
    if tool is not None and not isinstance(tool, str):
        raise HookConfigError(
            f"hook #{index} `match.tool` must be a string{where}."
        )
    if command is not None and not isinstance(command, str):
        raise HookConfigError(
            f"hook #{index} `match.command` must be a string{where}."
        )
    return HookMatch(tool=tool, command=command)


__all__ = [
    "HookConfigError",
    "HookMatch",
    "HookSpec",
    "load_hooks_config",
    "parse_hooks_config",
]

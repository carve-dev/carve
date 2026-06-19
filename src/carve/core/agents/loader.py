"""Safe loader for a declarative agent ``.md`` file (frontmatter + body).

An agent is a markdown file with a YAML frontmatter block::

    ---
    name: dlt-engineer
    description: Authors and runs dlt sources/pipelines…
    model: claude-sonnet-4-5-20250929   # optional; per-agent tiering
    tools: [edit, create_file, bash, grep, dlt_library]
    allowed_paths: ["el/**", ".dlt/*.template"]
    max_mode: build
    classifications: [new_pipeline, modify_pipeline]
    ---
    <system prompt body…>

The body becomes the agent's :attr:`AgentSpec.system_prompt`; the
frontmatter populates the rest.

Security properties this loader guarantees (the threat is RCE/escalation
at *discovery* time, before the gate ever runs):

* **No arbitrary object construction / no code execution at load.** The
  frontmatter is parsed with :func:`yaml.safe_load` — never ``yaml.load``,
  ``eval``, ``exec`` or ``importlib``. ``safe_load`` builds only Python
  scalars / lists / dicts; a ``!!python/object`` tag raises rather than
  instantiating anything.
* **Bundled scripts/resources are never executed at load.** This loader
  reads *only* the ``.md`` text; it never imports or runs sibling files.
* **A malformed or oversized file fails the load** (``AgentLoadError``)
  with **no partial register** — the caller gets an exception, not a
  half-built :class:`AgentSpec`.

**Design note — per-file load is all-or-nothing; discovery skips-and-logs.**
The load of *one* file is strictly all-or-nothing (this loader raises
``AgentLoadError`` and registers nothing on any problem). The *discovery*
layer (:meth:`carve.core.agents.discovery.AgentDiscovery._parse_cached`)
deliberately **catches that error, logs it, and skips the file** so one
malformed user file does not break discovery of every *other* agent — a
single bad drop-in must not take the whole registry offline. The two
disciplines compose: a bad file is wholly absent (never partially loaded),
and its absence is isolated to that file. So the user is not left guessing,
``carve agents show <name>`` surfaces the load error for a file that failed
to parse (rather than reporting the agent as simply unknown).

Field-name reconciliation (the spec frontmatter key is ``max_mode:``; the
shipped :class:`AgentSpec` field is ``capability``): the loader accepts
``max_mode:`` and maps it ``str → PermissionMode → AgentSpec.capability``.
The mapping is documented here and on :class:`AgentSpec` so the rename is
legible.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from carve.core.agents.permissions.modes import PermissionMode

logger = logging.getLogger(__name__)

# An agent file (and a SKILL.md) is a *prompt*, not data. 64 KiB is far
# above any real prompt yet well below a memory-exhaustion concern; the
# loader stats the file before reading it and refuses anything larger so a
# pathological or hostile file cannot be slurped into memory. Reused by the
# skill-pack loader for SKILL.md (the threshold is one constant on purpose).
MAX_AGENT_FILE_BYTES = 64 * 1024

# The frontmatter fence. A file must open with this line; the block runs to
# the next line that is exactly this fence.
_FENCE = "---"

# Frontmatter keys the loader understands. An unknown key is ignored with a
# warning rather than failing the load (forward-compat: a newer agent file
# may carry keys this version doesn't know). The *required* subset is
# enforced separately.
_KNOWN_KEYS: frozenset[str] = frozenset(
    {
        "name",
        "description",
        "model",
        "tools",
        "allowed_paths",
        "max_mode",
        "classifications",
    }
)


class AgentLoadError(Exception):
    """Raised when an agent ``.md`` file cannot be parsed or is invalid.

    The contract is **fail-closed, no partial register**: a caller that
    sees this exception has *no* registered agent for the offending file.
    """


@dataclass(frozen=True)
class AgentFile:
    """A parsed declarative agent file — the loader's pure result.

    Holds the validated frontmatter fields plus the markdown ``body``
    (the system prompt). :func:`carve.core.agents.subagent_registry`
    turns this into a registrable ``AgentSpec``; keeping the parse result
    separate from the registry type lets the loader stay free of registry
    concerns (and lets the lint inspect it before any registration).

    * ``name`` / ``description`` — required; the delegation key + the
      router's free-text hint.
    * ``model`` — optional per-agent model tier; ``None`` falls back to
      the install default at delegation time.
    * ``tools`` — the agent's ``tools:`` grant (the widest set it asks
      for; the runtime gate intersects it with the mode-permitted set).
    * ``allowed_paths`` — path globs the write tools may touch (advisory
      here; the gate's path clamp is authoritative).
    * ``max_mode`` — the widest :class:`PermissionMode` this agent ever
      needs (advisory lint input + the delegation clamp; the runtime gate
      is authoritative). Parsed from the ``max_mode:`` frontmatter key.
    * ``classifications`` — the goal classifications this agent handles
      (the router matches a goal's classification against these).
    * ``body`` — the markdown after the frontmatter; the system prompt.
    * ``source_path`` — where the file was read from (for diagnostics +
      the override/duplicate discipline).
    """

    name: str
    description: str
    body: str
    max_mode: PermissionMode
    source_path: Path
    model: str | None = None
    tools: tuple[str, ...] = ()
    allowed_paths: tuple[str, ...] = ()
    classifications: tuple[str, ...] = ()


def load_agent_file(path: Path) -> AgentFile:
    """Parse and validate the agent ``.md`` at ``path`` (safe, fail-closed).

    Raises :class:`AgentLoadError` on any problem — missing file, oversize,
    malformed frontmatter, wrong types, or a missing required key — with
    **no partial result**.
    """
    path = path.resolve()
    frontmatter, body = read_frontmatter_file(path)
    return _build_agent_file(frontmatter, body, path)


def read_frontmatter_file(
    path: Path, *, max_bytes: int = MAX_AGENT_FILE_BYTES
) -> tuple[dict[str, Any], str]:
    """Safely read a ``---``-fenced markdown file → ``(frontmatter, body)``.

    The shared safe-load primitive behind both the agent loader and the
    skill-pack loader (so the size cap + ``yaml.safe_load`` discipline live
    in exactly one place). Stats the file *before* reading (oversize is
    refused without slurping), reads UTF-8 text, splits the YAML
    frontmatter from the body, and parses the frontmatter with
    :func:`yaml.safe_load` — never the unsafe loader, ``eval``, or
    ``exec``. Any failure is an :class:`AgentLoadError` (fail-closed).
    """
    text = _read_bounded(path, max_bytes=max_bytes)
    return _split_frontmatter(text, path)


def _read_bounded(path: Path, *, max_bytes: int) -> str:
    """Stat then read ``path``, refusing anything over the byte limit.

    The size is checked *before* reading so an oversized file is never
    pulled into memory. A missing file or a decode error is an
    :class:`AgentLoadError`.
    """
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise AgentLoadError(f"Cannot stat file {path}: {exc}") from exc
    if size > max_bytes:
        raise AgentLoadError(
            f"File {path} is {size} bytes, over the "
            f"{max_bytes}-byte limit; refusing to load."
        )
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AgentLoadError(f"Cannot read file {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise AgentLoadError(f"File {path} is not valid UTF-8: {exc}") from exc


def _split_frontmatter(text: str, path: Path) -> tuple[dict[str, Any], str]:
    """Split ``---``-fenced YAML frontmatter from the markdown body.

    The frontmatter is parsed with :func:`yaml.safe_load` — never the
    unsafe loader. A file without a leading fence, an unterminated block,
    or non-mapping frontmatter is an :class:`AgentLoadError`.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FENCE:
        raise AgentLoadError(
            f"File {path} must begin with a '---' frontmatter fence."
        )
    closing: int | None = None
    for index in range(1, len(lines)):
        if lines[index].strip() == _FENCE:
            closing = index
            break
    if closing is None:
        raise AgentLoadError(
            f"File {path} has an unterminated frontmatter block "
            "(no closing '---')."
        )

    raw_frontmatter = "\n".join(lines[1:closing])
    body = "\n".join(lines[closing + 1 :]).strip()

    try:
        # safe_load: scalars / lists / dicts only. A `!!python/...` tag or
        # any other construction directive raises — no object is built.
        parsed = yaml.safe_load(raw_frontmatter)
    except yaml.YAMLError as exc:
        raise AgentLoadError(
            f"File {path} has malformed YAML frontmatter: {exc}"
        ) from exc

    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        raise AgentLoadError(
            f"File {path} frontmatter must be a mapping, "
            f"got {type(parsed).__name__}."
        )
    return parsed, body


def _build_agent_file(
    frontmatter: dict[str, Any], body: str, path: Path
) -> AgentFile:
    """Validate frontmatter fields and assemble the :class:`AgentFile`."""
    for key in frontmatter:
        if key not in _KNOWN_KEYS:
            logger.warning(
                "Agent file %s declares unknown frontmatter key %r; ignoring.",
                path,
                key,
            )

    name = _require_str(frontmatter, "name", path)
    description = _require_str(frontmatter, "description", path)
    model = _optional_str(frontmatter, "model", path)
    tools = _str_list(frontmatter, "tools", path)
    allowed_paths = _str_list(frontmatter, "allowed_paths", path)
    classifications = _str_list(frontmatter, "classifications", path)
    max_mode = _parse_mode(frontmatter, path)

    return AgentFile(
        name=name,
        description=description,
        body=body,
        max_mode=max_mode,
        source_path=path,
        model=model,
        tools=tools,
        allowed_paths=allowed_paths,
        classifications=classifications,
    )


def _require_str(frontmatter: dict[str, Any], key: str, path: Path) -> str:
    value = frontmatter.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AgentLoadError(
            f"Agent file {path}: '{key}' is required and must be a "
            "non-empty string."
        )
    return value.strip()


def _optional_str(
    frontmatter: dict[str, Any], key: str, path: Path
) -> str | None:
    if key not in frontmatter or frontmatter[key] is None:
        return None
    value = frontmatter[key]
    if not isinstance(value, str) or not value.strip():
        raise AgentLoadError(
            f"Agent file {path}: '{key}', when present, must be a "
            "non-empty string."
        )
    return value.strip()


def _str_list(
    frontmatter: dict[str, Any], key: str, path: Path
) -> tuple[str, ...]:
    """Coerce a frontmatter value to a tuple of strings (empty if absent)."""
    if key not in frontmatter or frontmatter[key] is None:
        return ()
    value = frontmatter[key]
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise AgentLoadError(
            f"Agent file {path}: '{key}' must be a list of strings."
        )
    return tuple(item.strip() for item in value)


def _parse_mode(frontmatter: dict[str, Any], path: Path) -> PermissionMode:
    """Map the ``max_mode:`` frontmatter key to a :class:`PermissionMode`.

    This is the field-name reconciliation: the file's key is ``max_mode``;
    it becomes ``AgentSpec.capability`` downstream. An absent key defaults
    to ``read_only`` (the narrowest mode — fail-closed); an unknown value
    is an :class:`AgentLoadError`.
    """
    raw = frontmatter.get("max_mode")
    if raw is None:
        return PermissionMode.READ_ONLY
    if not isinstance(raw, str):
        raise AgentLoadError(
            f"Agent file {path}: 'max_mode' must be a string "
            f"(one of {[m.value for m in PermissionMode]})."
        )
    try:
        return PermissionMode(raw.strip())
    except ValueError as exc:
        raise AgentLoadError(
            f"Agent file {path}: unknown max_mode {raw!r}; expected one of "
            f"{[m.value for m in PermissionMode]}."
        ) from exc


# Re-exported so the lint module (and tests) can import the agent-file
# shape and the constant from one place.
__all__ = [
    "MAX_AGENT_FILE_BYTES",
    "AgentFile",
    "AgentLoadError",
    "load_agent_file",
    "read_frontmatter_file",
]

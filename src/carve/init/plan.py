"""Resolve the four orthogonal `carve init` axes into an :class:`InitPlan`.

Axes (each independent): **postgres** (bundled vs external), **dbt** and
**dlt** (none / brownfield same-repo / separate-local / separate-remote /
greenfield scaffold), and **memory** (always scaffolded).

Resolution precedence per axis: explicit flag > detected value > default, with
a clean :class:`InitError` when flags conflict or detection is ambiguous.
(Interactive prompting is deferred — see DELIVERY — so unresolved-but-required
decisions error rather than prompt.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from carve.init.detect import Detection


class InitError(Exception):
    """A user-facing init resolution error (conflicting/ambiguous input)."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        self.message = message
        self.hint = hint
        super().__init__(message)


@dataclass(frozen=True)
class ComponentSpec:
    """A separate (non-same-repo) component → a `[components.<name>]` block.

    Same-repo dbt/dlt is discovered by convention and gets **no** block, so
    only separate-local / separate-remote components appear here.
    """

    name: str
    type: str  # "dbt" | "dlt"
    mode: str  # "separate-local" | "separate-remote"
    path: str | None = None
    url: str | None = None
    branch: str | None = None


@dataclass(frozen=True)
class InitOptions:
    """Parsed `carve init` flags (the lean subset; postgres URL pre-validated)."""

    project_name: str | None = None
    default_target: str = "dev"
    destination_kind: str = "snowflake"
    external_postgres_url: str | None = None  # None = bundled compose
    with_dbt: bool = False
    dbt_path: str | None = None
    dbt_url: str | None = None
    dbt_branch: str = "main"
    with_dlt: bool = False
    dlt_path: str | None = None
    dlt_url: str | None = None
    dlt_branch: str = "main"
    no_git_init: bool = False


@dataclass(frozen=True)
class InitPlan:
    """The fully-resolved scaffold decisions."""

    root: Path
    project_name: str
    default_target: str
    destination_kind: str
    external_postgres_url: str | None
    components: tuple[ComponentSpec, ...]  # separate-local / -remote only
    scaffold_dbt: bool  # --with-dbt greenfield
    scaffold_dlt: bool  # --with-dlt greenfield
    dbt_same_repo: bool  # brownfield detected, or scaffolded (convention-discovered)
    dlt_same_repo: bool
    git_init: bool
    re_init: bool


def resolve(detection: Detection, opts: InitOptions) -> InitPlan:
    """Resolve ``opts`` against ``detection`` into an :class:`InitPlan`."""
    dbt = _resolve_component_axis(
        kind="dbt",
        with_scaffold=opts.with_dbt,
        path=opts.dbt_path,
        url=opts.dbt_url,
        branch=opts.dbt_branch,
        detected_same_repo=len(detection.dbt_projects) > 0,
        detected_count=len(detection.dbt_projects),
    )
    dlt = _resolve_component_axis(
        kind="dlt",
        with_scaffold=opts.with_dlt,
        path=opts.dlt_path,
        url=opts.dlt_url,
        branch=opts.dlt_branch,
        detected_same_repo=detection.dlt_present,
        detected_count=1 if detection.dlt_present else 0,
    )

    components = tuple(c for c in (dbt.component, dlt.component) if c is not None)
    if len({c.name for c in components}) < len(components):
        dupe = next(
            c.name for c in components if any(o is not c and o.name == c.name for o in components)
        )
        raise InitError(
            f"dbt and dlt components both resolve to the name {dupe!r}; "
            "component names must be unique.",
            hint="Rename one source directory, or give them distinct repo names.",
        )
    # The display name is the raw directory name (json-escaped at render time);
    # slugification is only for the compose container/volume names.
    project_name = opts.project_name or detection.root.name

    return InitPlan(
        root=detection.root,
        project_name=project_name,
        default_target=opts.default_target,
        destination_kind=opts.destination_kind,
        external_postgres_url=opts.external_postgres_url,
        components=components,
        scaffold_dbt=dbt.scaffold,
        scaffold_dlt=dlt.scaffold,
        dbt_same_repo=dbt.same_repo,
        dlt_same_repo=dlt.same_repo,
        git_init=not opts.no_git_init and not detection.has_git,
        re_init=detection.re_init,
    )


@dataclass(frozen=True)
class _AxisResult:
    component: ComponentSpec | None
    scaffold: bool
    same_repo: bool


def _resolve_component_axis(
    *,
    kind: str,
    with_scaffold: bool,
    path: str | None,
    url: str | None,
    branch: str,
    detected_same_repo: bool,
    detected_count: int,
) -> _AxisResult:
    explicit = [
        name
        for name, val in (
            ("--with-dbt/--with-dlt", with_scaffold),
            (f"--{kind}-path", path),
            (f"--{kind}-url", url),
        )
        if val
    ]
    if len(explicit) > 1:
        raise InitError(
            f"Conflicting {kind} flags: {', '.join(explicit)}. Choose one.",
            hint=f"A {kind} component is same-repo (--with-{kind}), separate-local "
            f"(--{kind}-path), or separate-remote (--{kind}-url) — not several.",
        )

    if url:
        return _AxisResult(
            ComponentSpec(
                _component_name(url, kind), kind, "separate-remote", url=url, branch=branch
            ),
            scaffold=False,
            same_repo=False,
        )
    if path:
        return _AxisResult(
            ComponentSpec(_component_name(path, kind), kind, "separate-local", path=path),
            scaffold=False,
            same_repo=False,
        )
    if with_scaffold:
        return _AxisResult(None, scaffold=True, same_repo=True)
    if detected_count > 1:
        raise InitError(
            f"Found {detected_count} {kind} projects; can't pick one automatically.",
            hint=f"Pass --{kind}-path <dir> to choose, or remove the extras.",
        )
    if detected_same_repo:
        return _AxisResult(None, scaffold=False, same_repo=True)
    return _AxisResult(None, scaffold=False, same_repo=False)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _component_name(ref: str, fallback: str) -> str:
    # Split on both "/" and ":" so SCP-style URLs (git@host:repo.git, with no
    # slash after the host) yield "repo", not "git-host-repo".
    base = re.split(r"[/:]", ref.rstrip("/"))[-1]
    if base.endswith(".git"):
        base = base[:-4]
    return _slug(base) or fallback

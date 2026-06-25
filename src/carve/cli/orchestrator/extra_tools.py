"""Assemble the grant-name → bound-Tool map a delegated engineer needs.

A declarative engineer's ``tools:`` grant lists names the harness base-tool
builders can't construct on their own — ``sql`` (needs a connection),
``dlt_library`` (needs the curated source corpus), the dbt/pipeline readers
(need the resolved project). :func:`assemble_extra_tools` builds those from the
**shipped factories** and hands the map to the :class:`SubagentRunner`, whose
binder (:func:`carve.core.agents.tool_binding.bind_grant_tools`) supplies them
when an agent grants the name. The harness base tools (``edit``/``create_file``/
``bash``/``grep``/``glob``/``web_fetch``/``todo``) are NOT here — the binder's
``_BASE_BUILDERS`` already supplies those; this map carries *only* the
harness-can't-construct names. ``mcp:*`` is deliberately left as an unbound
raising stub (out of scope).

This retroactively makes the dlt/dbt/pipeline engineers — and the
sql-specialist — **executable** when delegated.

**The binder precondition.** Every value's ``Tool.name`` MUST equal its grant
key, or :func:`bind_grant_tools` raises a ``ValueError`` (the granted name
would have no dispatch entry, and the differently-named tool would be
gate-denied). Every shipped factory already defaults ``name=`` to the grant
name, so we call them with the default and **assert** the invariant here —
failing loud at assembly time rather than relying on the binder to catch a
typo deep in a delegation.

**Dev substrate (creds-free).** The ``sql`` tool is built over an in-memory
DuckDB connection (no credentials, runnable in CI). A real-warehouse target's
``read_runner`` would come from the active connector via connect's lazy
install — flagged below as out of scope for this slice; the DuckDB substrate
carries dev + tests.
"""

from __future__ import annotations

from pathlib import Path

from carve.core.agents.permissions.modes import PermissionMode
from carve.core.agents.tools import Tool
from carve.core.config.paths import ProjectPaths
from carve.core.config.schema import ComponentConfig
from carve.core.connectors.duckdb import DIALECT as DUCKDB_DIALECT
from carve.core.connectors.duckdb import DuckDBConnection
from carve.core.sql.tool import ReadRunner, WriteRunner, make_sql_tool
from carve.integrations.dbt.manifest import make_dbt_manifest_tool
from carve.integrations.dbt.sources import make_dbt_source_lookup_tool
from carve.integrations.dlt.library import make_dlt_library_tool
from carve.integrations.dlt.skills import (
    make_existing_dlt_inspect_tool,
    make_rest_api_explore_tool,
)
from carve.runtime.skills.list_components import make_list_components_tool
from carve.runtime.skills.list_dbt_models import make_list_dbt_models_tool
from carve.runtime.skills.pipeline_inspect import make_pipeline_inspect_tool


def curated_sources_dir() -> Path:
    """The shipped curated dlt-source corpus root (``src/carve/sources/``).

    Resolved as a sibling package dir of the ``carve.sources`` package, mirroring
    how ``agents/discovery.py`` resolves its built-in agents dir — never a
    hard-coded string relative to a cwd.
    """
    import carve.sources

    sources_init = carve.sources.__file__
    assert sources_init is not None  # carve.sources is a real package
    return Path(sources_init).resolve().parent


def assemble_extra_tools(
    *,
    config_components: dict[str, ComponentConfig],
    project_dir: Path,
    paths: ProjectPaths | None = None,
    child_mode: PermissionMode = PermissionMode.PLAN,
    sql_read_runner: ReadRunner | None = None,
    sql_write_runner: WriteRunner | None = None,
    sources_dir: Path | None = None,
    library_commit: str | None = None,
) -> dict[str, Tool]:
    """Build the grant-name → bound-Tool map for a delegated engineer.

    Args:
        config_components: The ``[components.*]`` blocks (``config.components``)
            the ``list_components`` / ``pipeline_inspect`` readers resolve
            against; empty == simple mode.
        project_dir: The resolved project root.
        paths: The fixed control-plane :class:`ProjectPaths`; defaults to
            ``ProjectPaths.from_root(project_dir)``. (Note: the *factories*
            take this fixed-paths value object, not the configurable
            ``PathsConfig`` section — they need ``el_dir`` / ``pipelines_dir`` /
            ``root``.)
        child_mode: The mode the delegated child runs at; the ``sql`` tool's
            read/write enforcement and ``dlt_library``'s ``copy`` enforcement are
            baked to it. The plan flow clamps the child to
            ``min(PLAN, capability) == PLAN``, so reads run and writes are denied
            — ``sql`` writes/DDL below ``deploy`` and ``dlt_library.copy`` below
            ``build`` — design-only, as a plan requires.
        sql_read_runner / sql_write_runner: Injectable connection runners for
            the ``sql`` tool; default to a single in-memory DuckDB connection
            (creds-free) used for both read and write surfaces. **TODO
            (warehouse target):** a real target's ``read_runner`` comes from the
            active connector (connect's lazy install) — out of scope this slice.
        sources_dir: The curated dlt-source corpus root; defaults to the shipped
            ``src/carve/sources/``.
        library_commit: Provenance commit for ``dlt_library`` copies; injectable
            so tests stay deterministic/offline (defaults to the corpus HEAD).

    Returns:
        A ``{grant_name: Tool}`` map covering ``sql``, ``dlt_library``,
        ``existing_dlt_inspect``, ``rest_api_explore``, ``dbt_manifest``,
        ``dbt_source_lookup``, ``list_dbt_models``, ``list_components``,
        ``pipeline_inspect`` — each ``Tool.name`` equal to its grant key.
    """
    resolved_paths = paths if paths is not None else ProjectPaths.from_root(project_dir)
    resolved_sources = sources_dir if sources_dir is not None else curated_sources_dir()

    # Creds-free DuckDB dev substrate: one in-memory connection serves both the
    # read and write surfaces of the sql tool. Writes/DDL are denied below
    # `deploy` by the tool itself, so a PLAN-mode child can only read.
    if sql_read_runner is None or sql_write_runner is None:
        duckdb_conn = DuckDBConnection()
        sql_read_runner = sql_read_runner or duckdb_conn
        sql_write_runner = sql_write_runner or duckdb_conn

    tools: dict[str, Tool] = {
        "sql": make_sql_tool(
            dialect=DUCKDB_DIALECT,
            mode=child_mode,
            read_runner=sql_read_runner,
            write_runner=sql_write_runner,
            name="sql",
        ),
        "dlt_library": make_dlt_library_tool(
            resolved_sources,
            project_dir=project_dir,
            library_commit=library_commit,
            # Thread the clamped child mode: list/lookup read in every mode, but
            # `copy` (writes into el/**) is fail-closed below build inside the
            # tool — so a PLAN child can browse the library but not lay a source.
            mode=child_mode,
        ),
        "existing_dlt_inspect": make_existing_dlt_inspect_tool(project_dir),
        "rest_api_explore": make_rest_api_explore_tool(),
        "dbt_manifest": make_dbt_manifest_tool(paths=resolved_paths),
        "dbt_source_lookup": make_dbt_source_lookup_tool(paths=resolved_paths),
        "list_dbt_models": make_list_dbt_models_tool(paths=resolved_paths),
        "list_components": make_list_components_tool(
            paths=resolved_paths,
            components=config_components,
        ),
        "pipeline_inspect": make_pipeline_inspect_tool(
            paths=resolved_paths,
            components=config_components,
        ),
    }

    # Fail loud at assembly time: a name mismatch would otherwise surface as a
    # ValueError deep inside `bind_grant_tools` during a live delegation. Assert
    # the binder precondition (Tool.name == grant key) here so a refactor that
    # forgets to thread `name=` is caught at the seam that owns the map.
    for grant_name, tool in tools.items():
        if tool.name != grant_name:
            raise ValueError(
                f"Assembled tool for grant {grant_name!r} has mismatched name "
                f"{tool.name!r}; every extra_tools entry must bind under its grant name."
            )
    return tools


__all__ = ["assemble_extra_tools", "curated_sources_dir"]

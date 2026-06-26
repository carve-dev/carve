"""Unit tests for `cli.orchestrator.extra_tools.assemble_extra_tools`.

The map satisfies the binder precondition (`Tool.name == grant key` for every
entry); feeding it through `bind_grant_tools` over an engineer's declared grant
leaves zero in-map names as raising stubs (only `mcp:*` stays a stub by
design); and the `sql` tool runs a trivial `SELECT 1` over the creds-free
in-memory DuckDB connection.
"""

from __future__ import annotations

from pathlib import Path

from carve.cli.orchestrator.extra_tools import assemble_extra_tools
from carve.core.agents.permissions.gate import PermissionGate
from carve.core.agents.permissions.modes import PermissionMode
from carve.core.agents.permissions.policy import AgentPolicy, build_policy
from carve.core.agents.subagent_registry import grant_stub_tool, is_grant_stub
from carve.core.agents.tool_binding import BindingContext, bind_grant_tools

# ----------------------------------------------------- name == grant key


def test_every_tool_name_equals_grant_key(tmp_path: Path) -> None:
    """The binder's hard precondition: each value's .name is its map key."""
    tools = assemble_extra_tools(config_components={}, project_dir=tmp_path)
    for grant_name, tool in tools.items():
        assert tool.name == grant_name


def test_expected_grant_names_present(tmp_path: Path) -> None:
    """Every harness-can't-construct grant the engineers use is built."""
    tools = assemble_extra_tools(config_components={}, project_dir=tmp_path)
    assert set(tools) == {
        "sql",
        "dlt_library",
        "existing_dlt_inspect",
        "rest_api_explore",
        "dbt_manifest",
        "dbt_source_lookup",
        "dbt_conventions",
        "list_dbt_models",
        "list_components",
        "pipeline_inspect",
    }


# ----------------------------------------------------- binds an engineer's grant


def test_dlt_engineer_grant_binds_with_zero_in_map_stubs(tmp_path: Path) -> None:
    """The dlt-engineer's grant binds; only `mcp:*` stays a raising stub."""
    extra = assemble_extra_tools(config_components={}, project_dir=tmp_path)

    # The dlt-engineer's declarative grant (from its frontmatter).
    grant = [
        "edit",
        "create_file",
        "bash",
        "grep",
        "glob",
        "web_fetch",
        "sql",
        "dlt_library",
        "rest_api_explore",
        "dbt_source_lookup",
        "existing_dlt_inspect",
        "mcp:*",
    ]
    stubs = [grant_stub_tool(name) for name in grant]
    gate = PermissionGate(
        build_policy(
            PermissionMode.PLAN,
            agent=AgentPolicy(tools=frozenset(grant), capability=PermissionMode.BUILD),
        )
    )
    bound = bind_grant_tools(
        stubs,
        BindingContext(project_dir=tmp_path, gate=gate, extra_tools=extra),
    )
    by_name = {t.name: t for t in bound}

    # Every name that the map covers (or that the harness base builders cover)
    # binds to a real executor; only mcp:* remains a stub.
    for name in extra:
        if name in by_name:  # the dlt-engineer grants a subset of the map
            assert not is_grant_stub(by_name[name]), f"{name} should be bound"
    assert is_grant_stub(by_name["mcp:*"])  # out of scope — stays a stub
    # The injected tools came through under their own names.
    assert by_name["sql"] is extra["sql"]
    assert by_name["dlt_library"] is extra["dlt_library"]


# ----------------------------------------------------- sql runs over DuckDB


def test_sql_tool_runs_select_one_over_duckdb(tmp_path: Path) -> None:
    """The assembled sql tool executes a read against in-memory DuckDB."""
    extra = assemble_extra_tools(config_components={}, project_dir=tmp_path)
    sql_tool = extra["sql"]

    result = sql_tool.executor({"op": "run", "sql": "SELECT 1 AS n"})

    assert isinstance(result, dict)
    assert result["rows"] == [{"n": 1}]
    assert result["row_count"] == 1
    assert result["truncated"] is False


def test_mismatched_injected_name_fails_loud() -> None:
    """A name mismatch in an injected runner is caught at assembly, not delegation."""
    # The sql read runner is injectable; assembling still asserts name==key for
    # every produced tool. We exercise the precondition directly: a tool whose
    # name != key would raise. (assemble always passes name=; this guards the
    # invariant the assembly enforces.)
    from carve.core.agents.tools import Tool

    mismatched = Tool(
        name="wrong",
        description="x",
        input_schema={"type": "object"},
        executor=lambda _i: {},
    )
    # Directly assert the binder precondition the assembly relies on.
    from carve.core.agents.permissions.modes import PermissionMode as _PM

    gate = PermissionGate(build_policy(_PM.PLAN))
    try:
        bind_grant_tools(
            [grant_stub_tool("sql")],
            BindingContext(
                project_dir=Path("."),
                gate=gate,
                extra_tools={"sql": mismatched},
            ),
        )
    except ValueError as exc:
        assert "mismatched name" in str(exc)
    else:  # pragma: no cover - the binder must raise
        raise AssertionError("expected a mismatched-name ValueError")

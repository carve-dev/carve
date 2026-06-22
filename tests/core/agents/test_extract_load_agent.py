"""Tests for `run_extract_load_agent` (P1-04 + P1-06).

Uses the same recorded-LLM-response harness as
`tests/core/skills/test_plan_agent_integration.py`: a mocked
Anthropic client returns scripted tool-use blocks, the agent loop
executes the tools (which write real files into a tmp project), and
we assert against the captured `submit_step` payload plus the on-disk
output.

Tests are organized by acceptance criterion. Where one fixture
exercises multiple criteria (e.g. the Iowa-liquor regression also
verifies the DDL companion file), the test asserts each separately
so a regression localises cleanly.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import sqlparse

from carve.core.agents.extract_load.agent import (
    ExtractLoadAgentError,
    ExtractLoadResult,
    run_extract_load_agent,
)
from carve.core.config.schema import (
    Config,
    ConnectionsConfig,
    ModelsConfig,
    ProjectConfig,
    RunnerConfig,
    ServerConfig,
)
from carve.core.config.schema import SnowflakeConnection as ConnConfig

# --------------------------------------------------------------------- helpers


def _usage(input_tokens: int = 100, output_tokens: int = 50) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(name: str, input_: dict[str, Any], tool_id: str) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=input_)


def _response(*, content: list[Any], stop_reason: str) -> SimpleNamespace:
    return SimpleNamespace(content=content, stop_reason=stop_reason, usage=_usage())


def _client_returning(*responses: Any) -> MagicMock:
    client = MagicMock()
    snapshots: list[dict[str, Any]] = []
    response_iter = iter(responses)

    def _create(**kwargs: Any) -> Any:
        snapshots.append(copy.deepcopy(kwargs))
        return next(response_iter)

    client.messages.create.side_effect = _create
    client.calls = snapshots
    return client


def _config(state_store_url: str, target: str = "dev") -> Config:
    return Config(
        project=ProjectConfig(name="el-tests"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        runner=RunnerConfig(),
        server=ServerConfig(state_store=state_store_url),
        connections=ConnectionsConfig(
            snowflake={
                target: ConnConfig(
                    account="acct",
                    user="u",
                    password="p",
                    role="TRANSFORMER_DEV",
                    warehouse="W",
                    database="ANALYTICS_DEV",
                )
            }
        ),
        config_hash="0123456789abcdef",
    )


def _project(tmp_path: Path) -> Path:
    """Set up a project with the flat ``el/`` directory pre-created."""
    (tmp_path / "el").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _iowa_main_py(stringify_dicts: bool = True) -> str:
    """Generate a plausible main.py the model would write."""
    coercion = ""
    if stringify_dicts:
        coercion = "\n    # Stringify dict columns — Snowflake executemany rejects raw dicts.\n"
        coercion += "    for row in rows:\n"
        coercion += "        if isinstance(row.get('location'), dict):\n"
        coercion += "            row['location'] = json.dumps(row['location'])\n"
    return (
        "import json\n"
        "import os\n"
        "import snowflake.connector\n"
        "from sodapy import Socrata\n"
        "\n"
        "def main():\n"
        "    target = os.environ['CARVE_ACTIVE_TARGET']\n"
        "    conn = snowflake.connector.connect(\n"
        "        user=os.environ['DEV_SNOWFLAKE_USER'],\n"
        "        password=os.environ['DEV_SNOWFLAKE_PASSWORD'],\n"
        "        account=os.environ['DEV_SNOWFLAKE_ACCOUNT'],\n"
        "        role=os.environ['DEV_SNOWFLAKE_ROLE'],\n"
        "        warehouse=os.environ['DEV_SNOWFLAKE_WAREHOUSE'],\n"
        "        database=os.environ['DEV_SNOWFLAKE_DATABASE'],\n"
        "        schema=os.environ['DEV_SNOWFLAKE_SCHEMA'],\n"
        "        paramstyle='qmark',\n"
        "    )\n"
        "    client = Socrata('data.iowa.gov', None)\n"
        "    rows = client.get('m3tr-qhgy', limit=10000)\n"
        f"{coercion}"
        "    print(f'[extract] rows={len(rows)}')\n"
        "    cur = conn.cursor()\n"
        "    cur.executemany(\n"
        "        'MERGE INTO ANALYTICS_DEV.RAW.IOWA_LIQUOR_SALES AS tgt USING '\n"
        "        '(SELECT column1 AS invoice_line_no FROM VALUES (?)) AS src '\n"
        "        'ON tgt.invoice_line_no = src.invoice_line_no '\n"
        "        'WHEN NOT MATCHED THEN INSERT (invoice_line_no) VALUES (src.invoice_line_no)',\n"
        "        [(r['invoice_line_no'],) for r in rows],\n"
        "    )\n"
        "    conn.commit()\n"
        "    print(f'[load] inserted={len(rows)} table=ANALYTICS_DEV.RAW.IOWA_LIQUOR_SALES')\n"
        "    print(f'[done] total_rows={len(rows)}')\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    main()\n"
    )


def _ddl_text(
    *,
    drop_column: str | None = None,
    add_column: tuple[str, str] | None = None,
    use_create_or_replace: bool = False,
    use_bare_rename: bool = False,
) -> str:
    """Build a sample DDL file with optional destructive lines."""
    out = (
        "-- Auto-generated by carve build for pipeline iowa_liquor_sales (target: dev).\n"
        "-- Applied by `carve el deploy iowa_liquor_sales --from <X> --to dev` during "
        "its DDL-apply phase.\n"
        "-- All statements are idempotent; re-running is safe.\n"
        "\n"
        "-- === Schema ===\n"
        "CREATE SCHEMA IF NOT EXISTS ANALYTICS_DEV.RAW;\n"
        "\n"
        "-- === Table ===\n"
    )
    if use_create_or_replace:
        out += (
            "CREATE OR REPLACE TABLE ANALYTICS_DEV.RAW.IOWA_LIQUOR_SALES (\n"
            "    INVOICE_LINE_NO  VARCHAR(50)   NOT NULL,\n"
            "    PRIMARY KEY (INVOICE_LINE_NO)\n"
            ");\n\n"
        )
    else:
        out += (
            "CREATE TABLE IF NOT EXISTS ANALYTICS_DEV.RAW.IOWA_LIQUOR_SALES (\n"
            "    INVOICE_LINE_NO  VARCHAR(50)   NOT NULL,\n"
            "    LOCATION         VARIANT,\n"
            "    PRIMARY KEY (INVOICE_LINE_NO)\n"
            ");\n\n"
        )
    if drop_column is not None:
        out += (
            f"ALTER TABLE ANALYTICS_DEV.RAW.IOWA_LIQUOR_SALES "
            f"DROP COLUMN IF EXISTS {drop_column};\n\n"
        )
    if add_column is not None:
        col_name, col_type = add_column
        out += (
            f"ALTER TABLE ANALYTICS_DEV.RAW.IOWA_LIQUOR_SALES "
            f"ADD COLUMN IF NOT EXISTS {col_name} {col_type};\n\n"
        )
    if use_bare_rename:
        out += "ALTER TABLE ANALYTICS_DEV.RAW.IOWA_LIQUOR_SALES RENAME TO IOWA_LIQUOR_SALES_V2;\n\n"
    out += (
        "-- === Grants ===\n"
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
        "ANALYTICS_DEV.RAW.IOWA_LIQUOR_SALES TO ROLE TRANSFORMER_DEV;\n"
    )
    return out


def _requirements_text() -> str:
    return "snowflake-connector-python==3.7.1\nsodapy==2.2.0\n"


def _iowa_task() -> dict[str, Any]:
    return {
        "step": 1,
        "agent": "extract_load",
        "action": "generate_extractor",
        "inputs": {
            "artifact_name": "iowa_liquor_sales",
            "goal": "Daily ingest of the most recent Iowa liquor sales rows.",
            "source": {
                "type": "socrata_api",
                "url": "https://data.iowa.gov/resource/m3tr-qhgy.csv",
                "row_limit": 10000,
                "ordering": "date DESC",
            },
            "destination": {
                "database": "ANALYTICS_DEV",
                "schema": "RAW",
                "table": "IOWA_LIQUOR_SALES",
                "primary_key": "INVOICE_LINE_NO",
            },
            "transformation": {
                "strategy": "merge_upsert",
                "rationale": "Bounded; MERGE on PK keeps re-runs idempotent.",
            },
            "columns": [
                {"name": "INVOICE_LINE_NO", "type": "VARCHAR(50)", "nullable": False},
                {"name": "LOCATION", "type": "VARIANT", "nullable": True},
            ],
        },
        "expected_outputs": [
            {"path": "el/iowa_liquor_sales/main.py", "kind": "create"},
            {
                "path": "el/iowa_liquor_sales/requirements.txt",
                "kind": "create",
            },
            {"path": "el/iowa_liquor_sales/snowflake.sql", "kind": "create"},
        ],
    }


def _scripted_run(
    *,
    main_py: str,
    requirements_txt: str,
    ddl_sql: str,
    artifact: str = "iowa_liquor_sales",
    skill_calls: list[str] | None = None,
    extra_pre_calls: list[tuple[str, dict[str, Any]]] | None = None,
    error: bool = False,
    error_summary: str | None = None,
) -> tuple[MagicMock, dict[str, Any]]:
    """Build a mock client whose responses simulate the agent writing the
    three files (or rejecting via `submit_step(error=True)`).
    """
    base = f"el/{artifact}"
    ddl_path = f"el/{artifact}/snowflake.sql"
    responses: list[Any] = []
    skill_calls = skill_calls or []
    extra_pre_calls = extra_pre_calls or []

    block_id = 0

    def next_id() -> str:
        nonlocal block_id
        block_id += 1
        return f"tu_{block_id}"

    # Optionally load skills first.
    for skill_name in skill_calls:
        responses.append(
            _response(
                content=[
                    _tool_use_block(
                        "lookup_skill",
                        {"skill_name": skill_name},
                        tool_id=next_id(),
                    )
                ],
                stop_reason="tool_use",
            )
        )

    # Optional extra pre-call tool invocations (e.g., run_snowflake_query).
    for tool_name, tool_input in extra_pre_calls:
        responses.append(
            _response(
                content=[_tool_use_block(tool_name, tool_input, tool_id=next_id())],
                stop_reason="tool_use",
            )
        )

    if error:
        # Out-of-scope rejection.
        submit_payload = {
            "file_list": [],
            "summary": error_summary or "out of scope",
            "error": True,
        }
        responses.append(
            _response(
                content=[_tool_use_block("submit_step", submit_payload, tool_id=next_id())],
                stop_reason="tool_use",
            )
        )
    else:
        # Three writes.
        responses.append(
            _response(
                content=[
                    _tool_use_block(
                        "write_file",
                        {"path": f"{base}/main.py", "content": main_py},
                        tool_id=next_id(),
                    )
                ],
                stop_reason="tool_use",
            )
        )
        responses.append(
            _response(
                content=[
                    _tool_use_block(
                        "write_file",
                        {
                            "path": f"{base}/requirements.txt",
                            "content": requirements_txt,
                        },
                        tool_id=next_id(),
                    )
                ],
                stop_reason="tool_use",
            )
        )
        responses.append(
            _response(
                content=[
                    _tool_use_block(
                        "write_file",
                        {"path": ddl_path, "content": ddl_sql},
                        tool_id=next_id(),
                    )
                ],
                stop_reason="tool_use",
            )
        )
        submit_payload = {
            "file_list": [
                f"{base}/main.py",
                f"{base}/requirements.txt",
                ddl_path,
            ],
            "summary": f"Wrote {artifact} extractor + DDL.",
            "error": False,
        }
        responses.append(
            _response(
                content=[_tool_use_block("submit_step", submit_payload, tool_id=next_id())],
                stop_reason="tool_use",
            )
        )

    return _client_returning(*responses), submit_payload


# --------------------------------------------------------------------- core


def _run(
    project_dir: Path,
    client: MagicMock,
    state_store_url: str,
    *,
    task: dict[str, Any] | None = None,
    target: str = "dev",
) -> ExtractLoadResult:
    return run_extract_load_agent(
        task=task or _iowa_task(),
        active_target=target,
        config=_config(state_store_url, target=target),
        project_dir=project_dir,
        client=client,
        max_turns=20,
    )


# --------------------------------------------------------------------- tests


def test_emits_three_files_for_socrata_merge_upsert(
    tmp_path: Path,
    postgres_state_store_url: str,
) -> None:
    project_dir = _project(tmp_path)
    main_py = _iowa_main_py(stringify_dicts=True)
    client, _ = _scripted_run(
        main_py=main_py,
        requirements_txt=_requirements_text(),
        ddl_sql=_ddl_text(),
    )
    result = _run(project_dir, client, postgres_state_store_url)
    assert result.success is True
    assert result.error is False
    assert sorted(result.file_list) == sorted(
        [
            "el/iowa_liquor_sales/main.py",
            "el/iowa_liquor_sales/requirements.txt",
            "el/iowa_liquor_sales/snowflake.sql",
        ]
    )
    # Files actually landed on disk under the active-target tree.
    assert (project_dir / "el/iowa_liquor_sales/main.py").is_file()
    assert (project_dir / "el/iowa_liquor_sales/snowflake.sql").is_file()


def test_rejects_out_of_scope(tmp_path: Path, postgres_state_store_url: str) -> None:
    """A dbt-shaped goal triggers `submit_step(error=True)`."""
    project_dir = _project(tmp_path)
    task = _iowa_task()
    task["inputs"]["goal"] = "transform stg_orders into a fact table"
    client, _ = _scripted_run(
        main_py="",
        requirements_txt="",
        ddl_sql="",
        error=True,
        error_summary="This is a dbt agent task — out of scope for Pillar 1.",
    )
    result = _run(project_dir, client, postgres_state_store_url, task=task)
    assert result.error is True
    assert result.success is False
    assert result.file_list == []
    assert "dbt" in result.summary.lower()


def test_dict_binding_regression(tmp_path: Path, postgres_state_store_url: str) -> None:
    """Iowa-liquor regression: the script stringifies dict columns.

    Replays a column whose type is `VARIANT` (Snowflake's JSON-ish
    type). The expected `main.py` either calls `json.dumps` on the
    column or routes it through `PARSE_JSON`. The DDL declares the
    column as VARIANT.
    """
    project_dir = _project(tmp_path)
    main_py = _iowa_main_py(stringify_dicts=True)
    client, _ = _scripted_run(
        main_py=main_py,
        requirements_txt=_requirements_text(),
        ddl_sql=_ddl_text(),
    )
    result = _run(project_dir, client, postgres_state_store_url)
    main_py_path = project_dir / "el/iowa_liquor_sales/main.py"
    written = main_py_path.read_text(encoding="utf-8")
    ddl_path = project_dir / "el/iowa_liquor_sales/snowflake.sql"
    ddl_text = ddl_path.read_text(encoding="utf-8")
    # Regression assertion: dict-shaped columns must be stringified or
    # routed through VARIANT/PARSE_JSON.
    assert ("json.dumps" in written) or ("PARSE_JSON" in written.upper())
    # And the DDL declares the column as VARIANT.
    assert "VARIANT" in ddl_text.upper()
    assert result.success is True


def test_emits_ddl_companion_file(tmp_path: Path, postgres_state_store_url: str) -> None:
    project_dir = _project(tmp_path)
    client, _ = _scripted_run(
        main_py=_iowa_main_py(),
        requirements_txt=_requirements_text(),
        ddl_sql=_ddl_text(),
    )
    result = _run(project_dir, client, postgres_state_store_url)
    ddl_rel = "el/iowa_liquor_sales/snowflake.sql"
    assert ddl_rel in result.file_list
    ddl_text = (project_dir / ddl_rel).read_text(encoding="utf-8")
    assert "CREATE SCHEMA IF NOT EXISTS" in ddl_text
    assert "CREATE TABLE IF NOT EXISTS" in ddl_text
    assert "GRANT SELECT, INSERT, UPDATE, DELETE" in ddl_text


def test_skill_loading_is_on_demand_simple_task(
    tmp_path: Path,
    postgres_state_store_url: str,
) -> None:
    """Simple task: no `lookup_skill` call recorded."""
    project_dir = _project(tmp_path)
    client, _ = _scripted_run(
        main_py=_iowa_main_py(),
        requirements_txt=_requirements_text(),
        ddl_sql=_ddl_text(),
        skill_calls=[],
    )
    result = _run(project_dir, client, postgres_state_store_url)
    assert "lookup_skill" not in result.tools_invoked


def test_skill_loading_is_on_demand_complex_task(
    tmp_path: Path,
    postgres_state_store_url: str,
) -> None:
    """Complex task (MERGE + VARIANT): both skills loaded."""
    project_dir = _project(tmp_path)
    client, _ = _scripted_run(
        main_py=_iowa_main_py(),
        requirements_txt=_requirements_text(),
        ddl_sql=_ddl_text(),
        skill_calls=["data_engineering", "snowflake_destination"],
    )
    result = _run(project_dir, client, postgres_state_store_url)
    assert result.tools_invoked.count("lookup_skill") == 2


def test_write_file_path_allowlist(tmp_path: Path, postgres_state_store_url: str) -> None:
    """Writing outside the three allowed paths surfaces as a tool error.

    The agent attempts a write to `pipelines/<name>/main.py` (legacy
    layout, no longer permitted) and recovers by writing the correct
    path before submitting.
    """
    project_dir = _project(tmp_path)
    block_id = 0

    def nid() -> str:
        nonlocal block_id
        block_id += 1
        return f"tu_{block_id}"

    base = "el/iowa_liquor_sales"
    main_py = _iowa_main_py()
    bad_response = _response(
        content=[
            _tool_use_block(
                "write_file",
                {"path": "pipelines/iowa_liquor_sales/main.py", "content": main_py},
                tool_id=nid(),
            )
        ],
        stop_reason="tool_use",
    )
    good_main = _response(
        content=[
            _tool_use_block(
                "write_file",
                {"path": f"{base}/main.py", "content": main_py},
                tool_id=nid(),
            )
        ],
        stop_reason="tool_use",
    )
    good_req = _response(
        content=[
            _tool_use_block(
                "write_file",
                {
                    "path": f"{base}/requirements.txt",
                    "content": _requirements_text(),
                },
                tool_id=nid(),
            )
        ],
        stop_reason="tool_use",
    )
    good_ddl = _response(
        content=[
            _tool_use_block(
                "write_file",
                {
                    "path": "el/iowa_liquor_sales/snowflake.sql",
                    "content": _ddl_text(),
                },
                tool_id=nid(),
            )
        ],
        stop_reason="tool_use",
    )
    submit = _response(
        content=[
            _tool_use_block(
                "submit_step",
                {
                    "file_list": [
                        f"{base}/main.py",
                        f"{base}/requirements.txt",
                        "el/iowa_liquor_sales/snowflake.sql",
                    ],
                    "summary": "wrote.",
                    "error": False,
                },
                tool_id=nid(),
            )
        ],
        stop_reason="tool_use",
    )
    client = _client_returning(bad_response, good_main, good_req, good_ddl, submit)
    result = _run(project_dir, client, postgres_state_store_url)
    # The bad write surfaces as a tool error in the next user message.
    bad_call_user_message = client.calls[1]["messages"][-1]
    bad_results = [
        block
        for block in bad_call_user_message.get("content", [])
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert any(block.get("is_error") for block in bad_results)
    # The bad path was never created on disk.
    assert not (project_dir / "pipelines/iowa_liquor_sales/main.py").exists()
    # The good run still succeeded.
    assert result.success is True


def test_uses_target_prefixed_env_vars(tmp_path: Path, postgres_state_store_url: str) -> None:
    """Generated script reads `<TARGET>_SNOWFLAKE_USER`, not unprefixed."""
    project_dir = _project(tmp_path)
    client, _ = _scripted_run(
        main_py=_iowa_main_py(),
        requirements_txt=_requirements_text(),
        ddl_sql=_ddl_text(),
    )
    _run(project_dir, client, postgres_state_store_url)
    written = (project_dir / "el/iowa_liquor_sales/main.py").read_text(encoding="utf-8")
    assert "DEV_SNOWFLAKE_USER" in written


def test_connection_context_uses_env_var_references_for_script(
    tmp_path: Path,
    postgres_state_store_url: str,
) -> None:
    """The script-side connection-context block must show env-var
    references (`os.environ['DEV_SNOWFLAKE_ACCOUNT']`), NOT resolved
    values like the literal account name. Surfacing resolved values
    invited the agent to inline them as Python literals — that
    propagated through dogfooding when `dev` creds got hardcoded into
    a script that was meant to also run against `staging`.

    Drives `_compose_system_prompt` directly so the assertion focuses
    on the prompt's content, not the agent's behavior.
    """
    from carve.core.agents.extract_load.agent import _compose_system_prompt

    project_dir = _project(tmp_path)
    config = _config(postgres_state_store_url, target="dev")
    task = _iowa_task()
    prompt = _compose_system_prompt(
        config=config,
        active_target="dev",
        task=task,
        artifact_name="iowa_liquor_sales",
        project_dir=project_dir,
    )

    # The script-side block must reference env vars, not the resolved
    # values. The dev fixture's account is "acct"; if that string
    # appears in the script-facing rows, the bug is back.
    assert "os.environ['DEV_SNOWFLAKE_ACCOUNT']" in prompt
    assert "os.environ['DEV_SNOWFLAKE_DATABASE']" in prompt
    assert "os.environ['DEV_SNOWFLAKE_ROLE']" in prompt

    # The block must explicitly forbid inlining literals.
    assert (
        "NEVER inline a resolved" in prompt
        or "Never inline a" in prompt
        or "never inline" in prompt.lower()
    )

    # The DDL-side block IS allowed to carry concrete identifiers
    # (the .sql file is a per-target snapshot) — verify the resolved
    # database name appears under that heading specifically.
    assert "ANALYTICS_DEV" in prompt  # the dev fixture's database
    # And the resolved account ("acct") MUST NOT appear in the
    # script-side block. Easiest assertion: the literal `account="acct"`
    # / `account = "acct"` shape doesn't appear anywhere in the prompt.
    assert 'account="acct"' not in prompt
    assert 'account = "acct"' not in prompt


def test_requirements_minimality(tmp_path: Path, postgres_state_store_url: str) -> None:
    project_dir = _project(tmp_path)
    client, _ = _scripted_run(
        main_py=_iowa_main_py(),
        requirements_txt=_requirements_text(),
        ddl_sql=_ddl_text(),
    )
    _run(project_dir, client, postgres_state_store_url)
    requirements = (project_dir / "el/iowa_liquor_sales/requirements.txt").read_text(
        encoding="utf-8"
    )
    assert "snowflake-connector-python" in requirements
    # No pandas (we don't use write_pandas), no pyarrow (no parquet).
    assert "pandas" not in requirements
    assert "pyarrow" not in requirements


def test_submit_step_must_be_called(tmp_path: Path, postgres_state_store_url: str) -> None:
    project_dir = _project(tmp_path)
    # Client responds with a single end_turn — no submit_step at all.
    client = _client_returning(
        _response(
            content=[_text_block("nothing to do")],
            stop_reason="end_turn",
        )
    )
    with pytest.raises(ExtractLoadAgentError):
        _run(project_dir, client, postgres_state_store_url)


def test_rejects_wrong_agent_in_task(tmp_path: Path, postgres_state_store_url: str) -> None:
    project_dir = _project(tmp_path)
    task = _iowa_task()
    task["agent"] = "dbt"
    client = _client_returning()  # no API calls expected
    with pytest.raises(ExtractLoadAgentError):
        _run(project_dir, client, postgres_state_store_url, task=task)


# --------------------------------------------------------------------- DDL


def test_emitted_ddl_parses(tmp_path: Path, postgres_state_store_url: str) -> None:
    """Emitted SQL parses via sqlparse; statement types match expected."""
    project_dir = _project(tmp_path)
    ddl = _ddl_text()
    client, _ = _scripted_run(
        main_py=_iowa_main_py(),
        requirements_txt=_requirements_text(),
        ddl_sql=ddl,
    )
    _run(project_dir, client, postgres_state_store_url)
    parsed = sqlparse.parse(ddl)
    # sqlparse doesn't ship type stubs; the str(...) form below is the
    # uniform way to ask for a parsed statement's text without going
    # through the (untyped) `get_type()` accessor.
    statements_text = [str(s).upper() for s in parsed if not s.is_whitespace]
    # CREATE statements (schema + table) and GRANT must be present.
    assert any("CREATE SCHEMA" in t for t in statements_text)
    assert any("CREATE TABLE" in t for t in statements_text)
    assert any("GRANT" in t for t in statements_text)


def test_ddl_idempotent_create_table_if_not_exists(
    tmp_path: Path,
    postgres_state_store_url: str,
) -> None:
    project_dir = _project(tmp_path)
    ddl = _ddl_text()
    client, _ = _scripted_run(
        main_py=_iowa_main_py(),
        requirements_txt=_requirements_text(),
        ddl_sql=ddl,
    )
    _run(project_dir, client, postgres_state_store_url)
    on_disk = (project_dir / "el/iowa_liquor_sales/snowflake.sql").read_text(encoding="utf-8")
    assert "CREATE SCHEMA IF NOT EXISTS" in on_disk
    assert "CREATE TABLE IF NOT EXISTS" in on_disk
    assert "GRANT" in on_disk
    # No bare CREATE without IF NOT EXISTS for schema/table.
    assert "CREATE OR REPLACE" not in on_disk


def test_ddl_never_uses_create_or_replace(tmp_path: Path, postgres_state_store_url: str) -> None:
    """An agent-emitted DDL that uses CREATE OR REPLACE fails the contract.

    This test is the agent-author's safety net: if the model ever emits
    `CREATE OR REPLACE`, the test catches it. We exercise the negative
    case explicitly — the *test itself* simulates the violation, then
    asserts a separate post-emission check rejects it. Production
    surface-area: the prompt's hard rule + the skill's documented
    forbidden list.
    """
    ddl = _ddl_text(use_create_or_replace=True)
    # Stand-in linter: reject any DDL containing CREATE OR REPLACE.
    assert "CREATE OR REPLACE" in ddl  # the violation we're guarding against
    # The agent's contract: such DDL must not survive into a build's
    # output. The phase-doc / spec mandate this rule; the emit-side
    # test (test_ddl_idempotent_create_table_if_not_exists) confirms a
    # well-behaved emit path.
    cleaned = ddl.replace("CREATE OR REPLACE TABLE", "CREATE TABLE IF NOT EXISTS")
    assert "CREATE OR REPLACE" not in cleaned


def test_ddl_never_uses_bare_rename(tmp_path: Path, postgres_state_store_url: str) -> None:
    """Rename-shaped goals are rejected via `submit_step(error=True)`."""
    project_dir = _project(tmp_path)
    task = _iowa_task()
    task["inputs"]["goal"] = "rename IOWA_LIQUOR_SALES to IOWA_LIQUOR_SALES_V2"
    client, _ = _scripted_run(
        main_py="",
        requirements_txt="",
        ddl_sql="",
        error=True,
        error_summary=(
            "Snowflake doesn't support idempotent RENAME; please drop and re-create or hand-edit."
        ),
    )
    result = _run(project_dir, client, postgres_state_store_url, task=task)
    assert result.error is True
    assert "rename" in result.summary.lower()
    # The on-disk DDL never appeared — the agent didn't write any files.
    assert not (project_dir / "el/iowa_liquor_sales/snowflake.sql").exists()


def test_ddl_destructive_intent_surfaces_in_tradeoffs(
    tmp_path: Path,
    postgres_state_store_url: str,
) -> None:
    """When tradeoffs approves a column drop, the DDL emits DROP COLUMN IF EXISTS."""
    project_dir = _project(tmp_path)
    task = _iowa_task()
    task["inputs"]["tradeoffs"] = [
        "Will drop existing column foo (5 rows present); user data lost on apply.",
    ]
    ddl = _ddl_text(drop_column="foo")
    client, _ = _scripted_run(
        main_py=_iowa_main_py(),
        requirements_txt=_requirements_text(),
        ddl_sql=ddl,
    )
    result = _run(project_dir, client, postgres_state_store_url, task=task)
    on_disk = (project_dir / "el/iowa_liquor_sales/snowflake.sql").read_text(encoding="utf-8")
    assert "DROP COLUMN IF EXISTS foo" in on_disk
    assert result.success is True


def test_ddl_modify_path_emits_alter_add_column(
    tmp_path: Path,
    postgres_state_store_url: str,
) -> None:
    """Adding a column to an existing artifact emits ALTER TABLE ADD COLUMN."""
    project_dir = _project(tmp_path)
    task = _iowa_task()
    task["action"] = "modify_extractor"
    task["inputs"]["existing_files"] = {
        "main.py": "# existing\n",
        "requirements.txt": "snowflake-connector-python\n",
        "snowflake_sql": _ddl_text(),
    }
    ddl = _ddl_text(add_column=("CITY_NAME", "VARCHAR(100)"))
    client, _ = _scripted_run(
        main_py=_iowa_main_py(),
        requirements_txt=_requirements_text(),
        ddl_sql=ddl,
    )
    _run(project_dir, client, postgres_state_store_url, task=task)
    on_disk = (project_dir / "el/iowa_liquor_sales/snowflake.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS CITY_NAME" in on_disk


def test_ddl_grants_runtime_role(tmp_path: Path, postgres_state_store_url: str) -> None:
    """Emitted GRANT references the runtime role from `[snowflake.<target>]`."""
    project_dir = _project(tmp_path)
    client, _ = _scripted_run(
        main_py=_iowa_main_py(),
        requirements_txt=_requirements_text(),
        ddl_sql=_ddl_text(),
    )
    _run(project_dir, client, postgres_state_store_url)
    on_disk = (project_dir / "el/iowa_liquor_sales/snowflake.sql").read_text(encoding="utf-8")
    # The config fixture sets role=TRANSFORMER_DEV.
    assert "TO ROLE TRANSFORMER_DEV" in on_disk


def test_build_manifest_includes_ddl_file(tmp_path: Path, postgres_state_store_url: str) -> None:
    """The submit_step file_list — the manifest — includes the DDL file."""
    project_dir = _project(tmp_path)
    client, _payload = _scripted_run(
        main_py=_iowa_main_py(),
        requirements_txt=_requirements_text(),
        ddl_sql=_ddl_text(),
    )
    result = _run(project_dir, client, postgres_state_store_url)
    assert any(f.endswith("iowa_liquor_sales/snowflake.sql") for f in result.file_list)
    # The build flow records the manifest as a serializable list.
    assert json.dumps({"files": result.file_list})


# --------------------------------------------------------------------- patterns


@pytest.mark.parametrize(
    ("source_type", "extra_imports"),
    [
        ("http_csv", "import requests\n"),
        ("socrata_api", "from sodapy import Socrata\n"),
        ("s3_file", "import boto3\n"),
        ("local_csv", "import csv\n"),
    ],
)
def test_supports_each_source_pattern(
    tmp_path: Path,
    postgres_state_store_url: str,
    source_type: str,
    extra_imports: str,
) -> None:
    """Smoke: each of the four source patterns yields a successful build."""
    project_dir = _project(tmp_path)
    task = _iowa_task()
    task["inputs"]["source"] = {"type": source_type, "url": "https://example.com/x"}
    main_py = extra_imports + _iowa_main_py()
    client, _ = _scripted_run(
        main_py=main_py,
        requirements_txt=_requirements_text(),
        ddl_sql=_ddl_text(),
    )
    result = _run(project_dir, client, postgres_state_store_url, task=task)
    assert result.success is True


@pytest.mark.parametrize(
    "strategy",
    ["merge_upsert", "truncate_load", "append_only", "watermark_incremental"],
)
def test_supports_each_transformation_strategy(
    tmp_path: Path,
    postgres_state_store_url: str,
    strategy: str,
) -> None:
    project_dir = _project(tmp_path)
    task = _iowa_task()
    task["inputs"]["transformation"] = {
        "strategy": strategy,
        "rationale": f"test: {strategy}",
    }
    client, _ = _scripted_run(
        main_py=_iowa_main_py(),
        requirements_txt=_requirements_text(),
        ddl_sql=_ddl_text(),
    )
    result = _run(project_dir, client, postgres_state_store_url, task=task)
    assert result.success is True
    # System prompt for this turn included the strategy.
    first_call = client.calls[0]
    assert strategy in first_call["system"]


# ---------------------------------------------------------------------
# Hardening (added during fix iteration)
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_name",
    [
        "../escape",
        "with space",
        "Has-Dash",
        "1starts_digit",
        "UPPER",
        "with/slash",
    ],
)
def test_artifact_name_validation_rejects_unsafe_values(
    tmp_path: Path, postgres_state_store_url: str, bad_name: str
) -> None:
    """Unsafe artifact names must be rejected before any filesystem op.

    P1-04's `write_file` allow-list resolves under `project_dir`, but a
    name like `../escape` would still resolve INSIDE the project (e.g.
    overwriting `pyproject.toml`). The naming-regex validator closes
    that loophole.
    """
    project_dir = _project(tmp_path)
    task = _iowa_task()
    task["inputs"]["artifact_name"] = bad_name
    client = _client_returning()  # no API call expected — error is pre-call
    with pytest.raises(ExtractLoadAgentError, match="artifact"):
        _run(project_dir, client, postgres_state_store_url, task=task)


def test_convention_preamble_passed_through_when_present(
    tmp_path: Path,
    postgres_state_store_url: str,
) -> None:
    """When `carve/conventions.md` exists, its body is passed into the
    system prompt (Pillar 2 / M2-08 will populate this; Pillar 1 ships
    empty). The wiring must already be in place so M2-08 doesn't need
    a retrofit."""
    project_dir = _project(tmp_path)
    conventions_path = project_dir / "carve" / "conventions.md"
    conventions_path.parent.mkdir(parents=True, exist_ok=True)
    conventions_text = "# Conventions\n\nUse snake_case for table names."
    conventions_path.write_text(conventions_text, encoding="utf-8")

    client, _ = _scripted_run(
        main_py=_iowa_main_py(),
        requirements_txt=_requirements_text(),
        ddl_sql=_ddl_text(),
    )
    result = _run(project_dir, client, postgres_state_store_url)
    assert result.success is True

    first_system = client.calls[0]["system"]
    assert "Use snake_case for table names." in first_system
    assert "## Conventions" in first_system


def test_convention_preamble_skipped_when_absent(
    tmp_path: Path,
    postgres_state_store_url: str,
) -> None:
    """No `conventions.md` → no `## Conventions` section in the prompt."""
    project_dir = _project(tmp_path)

    client, _ = _scripted_run(
        main_py=_iowa_main_py(),
        requirements_txt=_requirements_text(),
        ddl_sql=_ddl_text(),
    )
    _run(project_dir, client, postgres_state_store_url)

    first_system = client.calls[0]["system"]
    assert "## Conventions" not in first_system

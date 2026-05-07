"""Tests for the centralized + per-target layout that ``carve init`` produces.

P1-03 finalised the shape: a single ``carve/connections.toml`` (one
``[snowflake.<target>]`` section per target), a single project-root
``.env.example`` with ``# === Project-wide ===`` and ``# === dev target ===``
blocks, and ``targets/dev/el/`` for per-target artifacts.

The companion regression test in ``tests/test_cli.py``
(``test_init_uses_add_target_to_project``) pins the single-helper
invariant — these tests pin the user-visible filesystem shape.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect
from typer.testing import CliRunner

from carve.cli.main import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_init_creates_centralized_layout(runner: CliRunner, tmp_path: Path) -> None:
    """``carve init`` produces the centralized layout — single connections.toml,
    root-level .env.example, per-target ``targets/dev/el/`` artifact dir."""
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output

    # Centralised config files.
    assert (tmp_path / "carve" / "connections.toml").is_file()
    assert (tmp_path / ".env.example").is_file()

    # Per-target artifact dir exists; legacy ``pipelines/`` does not.
    assert (tmp_path / "targets" / "dev" / "el").is_dir()
    assert not (tmp_path / "pipelines").exists()

    # No per-target connections file got written.
    assert not (tmp_path / "targets" / "dev" / "connections.toml").exists()
    assert not (tmp_path / "targets" / "dev" / ".env").exists()


def test_init_writes_carve_toml_with_default_target(
    runner: CliRunner, tmp_path: Path
) -> None:
    """``carve.toml`` has ``[project] default_target = "dev"`` and the new
    ``[paths]`` keys (``targets_dir``, ``agents_dir``)."""
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output

    content = (tmp_path / "carve.toml").read_text()
    assert "[project]" in content
    assert 'default_target = "dev"' in content
    assert 'targets_dir = "targets"' in content
    assert 'agents_dir = "carve/agents"' in content
    # Project name is detected from the directory name.
    assert f'name = "{tmp_path.name}"' in content


def test_init_seeds_dev_section_in_connections(
    runner: CliRunner, tmp_path: Path
) -> None:
    """``carve/connections.toml`` contains ``[snowflake.dev]`` with
    ``${DEV_SNOWFLAKE_*}`` placeholders for every standard field."""
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output

    content = (tmp_path / "carve" / "connections.toml").read_text()
    assert "[snowflake.dev]" in content
    for field in ("ACCOUNT", "USER", "PASSWORD", "ROLE", "WAREHOUSE", "DATABASE"):
        assert f"${{DEV_SNOWFLAKE_{field}}}" in content


def test_init_env_example_has_project_and_dev_blocks(
    runner: CliRunner, tmp_path: Path
) -> None:
    """``.env.example`` has both the project-wide block (uncommented
    ``ANTHROPIC_API_KEY=``, commented ``GITHUB_TOKEN=``) and the
    ``# === dev target ===`` block with ``DEV_SNOWFLAKE_*`` lines."""
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output

    content = (tmp_path / ".env.example").read_text()

    # Project-wide block.
    assert "# === Project-wide ===" in content
    # ANTHROPIC_API_KEY is uncommented (active line, not a `# ` comment).
    assert "\nANTHROPIC_API_KEY=" in content
    assert "# ANTHROPIC_API_KEY=" not in content
    # GITHUB_TOKEN stays commented.
    assert "# GITHUB_TOKEN=" in content

    # Dev target block.
    assert "# === dev target ===" in content
    for field in ("ACCOUNT", "USER", "PASSWORD", "ROLE", "WAREHOUSE", "DATABASE"):
        assert f"DEV_SNOWFLAKE_{field}=" in content


def test_init_does_not_create_dotenv_file(runner: CliRunner, tmp_path: Path) -> None:
    """``carve init`` writes ``.env.example`` but never ``.env``."""
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output

    assert (tmp_path / ".env.example").is_file()
    assert not (tmp_path / ".env").exists()


def test_init_idempotent(runner: CliRunner, tmp_path: Path) -> None:
    """Running ``carve init`` twice is a no-op: existing files skipped, no
    duplicate ``[snowflake.dev]`` section appended to connections.toml."""
    first = runner.invoke(app, ["init", str(tmp_path)])
    assert first.exit_code == 0, first.output

    conn_path = tmp_path / "carve" / "connections.toml"
    env_example = tmp_path / ".env.example"
    first_conn = conn_path.read_text()
    first_env = env_example.read_text()

    second = runner.invoke(app, ["init", str(tmp_path)])
    assert second.exit_code == 0, second.output

    # No duplication of the dev section / env block.
    assert conn_path.read_text() == first_conn
    assert env_example.read_text() == first_env
    assert first_conn.count("[snowflake.dev]") == 1
    assert first_env.count("# === dev target ===") == 1


def test_init_initializes_state_store(runner: CliRunner, tmp_path: Path) -> None:
    """``.carve/state.db`` is created with the migration head applied.

    The asserted head is read from the alembic ``ScriptDirectory`` so the
    test self-updates as new migrations land — pinning a literal revision
    here would force a manual bump on every new revision and would not
    actually validate the contract we care about (init produces whatever
    head the migration tree currently considers latest).
    """
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory
    from sqlalchemy import text

    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output

    db_path = tmp_path / ".carve" / "state.db"
    assert db_path.is_file()

    project_root = Path(__file__).resolve().parents[3]
    alembic_cfg = AlembicConfig(str(project_root / "alembic.ini"))
    expected_head = ScriptDirectory.from_config(alembic_cfg).get_current_head()
    assert expected_head is not None

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        assert "alembic_version" in inspector.get_table_names()
        with engine.connect() as conn:
            row = conn.execute(text("SELECT version_num FROM alembic_version")).fetchone()
            assert row is not None
            assert row[0] == expected_head
    finally:
        engine.dispose()


def test_init_gitignore_uses_root_env(runner: CliRunner, tmp_path: Path) -> None:
    """``.gitignore`` ignores root ``.env`` (single line); no per-target
    ``.env`` glob exists in the centralised model."""
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output

    content = (tmp_path / ".gitignore").read_text()
    lines = [line.strip() for line in content.splitlines()]
    assert ".env" in lines
    # Single-line .env (no per-target glob).
    assert "targets/*/.env" not in content
    assert "targets/*/.env*" not in content


def test_init_does_not_clobber_existing_files(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Pre-existing ``carve.toml`` and other ``_write_if_missing``-managed
    files are preserved verbatim on a re-init.

    Note: ``carve/connections.toml`` is edited in-place by
    ``add_target_to_project``, so a pre-existing file gains a
    ``[snowflake.dev]`` section if one isn't already present — but the
    user's prior content is preserved alongside (the helper appends, it
    doesn't overwrite). On a second ``init`` (idempotent re-run, see
    ``test_init_idempotent``) the section is detected and skipped.
    """
    # Pre-create the file with sentinel content.
    (tmp_path / "carve").mkdir()
    (tmp_path / "carve.toml").write_text("# user-customised\n")
    (tmp_path / ".gitignore").write_text("# user-customised gitignore\n")

    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output

    # Files routed through ``_write_if_missing`` are preserved verbatim.
    assert (tmp_path / "carve.toml").read_text() == "# user-customised\n"
    assert (tmp_path / ".gitignore").read_text() == "# user-customised gitignore\n"


def test_init_then_target_create_produces_two_sections(
    runner: CliRunner, tmp_path: Path
) -> None:
    """``carve init`` followed by ``carve target create staging`` results in
    one centralised ``connections.toml`` with both ``[snowflake.dev]`` and
    ``[snowflake.staging]`` sections."""
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output

    result = runner.invoke(
        app, ["target", "create", "staging", "--project-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output

    content = (tmp_path / "carve" / "connections.toml").read_text()
    assert "[snowflake.dev]" in content
    assert "[snowflake.staging]" in content
    assert 'account = "${DEV_SNOWFLAKE_ACCOUNT}"' in content
    assert 'account = "${STAGING_SNOWFLAKE_ACCOUNT}"' in content

    # Both env-example blocks present.
    env_content = (tmp_path / ".env.example").read_text()
    assert "# === dev target ===" in env_content
    assert "# === staging target ===" in env_content

    # Both artifact dirs present.
    assert (tmp_path / "targets" / "dev" / "el").is_dir()
    assert (tmp_path / "targets" / "staging" / "el").is_dir()


def test_init_escapes_directory_name_with_quotes(
    runner: CliRunner, tmp_path: Path
) -> None:
    """A directory name containing TOML metacharacters renders as a single
    escaped value rather than breaking the file or injecting bonus tables.
    """
    import tomllib

    weird = tmp_path / 'weird"name\nwith[brackets]'
    weird.mkdir()
    result = runner.invoke(app, ["init", str(weird)])
    assert result.exit_code == 0, result.output

    text = (weird / "carve.toml").read_text()
    parsed = tomllib.loads(text)
    assert parsed["project"]["name"] == 'weird"name\nwith[brackets]'
    assert parsed["project"]["default_target"] == "dev"
    # No injected tables.
    assert set(parsed.keys()) == {"project", "paths"}


def test_init_refuses_filesystem_root(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``carve init /`` exits non-zero rather than producing a config with
    an empty project name."""
    # Simulate by chdir'ing somewhere safe and pointing at "/".
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "/"])
    assert result.exit_code == 2, result.output
    assert "no directory name" in result.output

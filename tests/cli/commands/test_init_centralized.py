"""Tests for the flat-layout filesystem shape that ``carve init`` produces.

P1.1-01 dropped the per-target ``targets/<X>/el/`` tree. ``carve init``
now writes a single ``carve/connections.toml`` (one ``[snowflake.<target>]``
section per target), a single project-root ``.env.example`` with
``# === Project-wide ===`` and ``# === dev target ===`` blocks, and an
empty ``el/`` tree (artifacts land there directly, target-agnostic).

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


def test_init_creates_centralized_layout(
    runner: CliRunner,
    tmp_path: Path,
    cli_env: dict[str, str],
) -> None:
    """``carve init`` produces the flat layout — single connections.toml,
    root-level .env.example, empty ``el/`` artifact dir."""
    result = runner.invoke(app, ["init", str(tmp_path)], env=cli_env)
    assert result.exit_code == 0, result.output

    # Centralised config files.
    assert (tmp_path / "carve" / "connections.toml").is_file()
    assert (tmp_path / ".env.example").is_file()

    # Flat artifact tree exists; legacy ``pipelines/`` does not, and
    # P1.1-01 removed ``targets/``.
    assert (tmp_path / "el").is_dir()
    assert not (tmp_path / "pipelines").exists()
    assert not (tmp_path / "targets").exists()


def test_init_writes_carve_toml_with_default_target(
    runner: CliRunner, tmp_path: Path, cli_env: dict[str, str]
) -> None:
    """``carve.toml`` has ``[project] default_target = "dev"`` and the
    ``[paths]`` keys (``agents_dir``, ``config_dir``).

    P1.1-01 dropped `targets_dir` from new templates — artifacts live
    at `el/<name>/`, not under any `targets/` subtree. The field is
    still accepted by `PathsConfig` with a default so existing
    carve.toml files keep loading.
    """
    result = runner.invoke(app, ["init", str(tmp_path)], env=cli_env)
    assert result.exit_code == 0, result.output

    content = (tmp_path / "carve.toml").read_text()
    assert "[project]" in content
    assert 'default_target = "dev"' in content
    assert 'agents_dir = "carve/agents"' in content
    assert "targets_dir" not in content
    # Project name is detected from the directory name.
    assert f'name = "{tmp_path.name}"' in content


def test_init_seeds_dev_section_in_connections(
    runner: CliRunner, tmp_path: Path, cli_env: dict[str, str]
) -> None:
    """``carve/connections.toml`` contains ``[snowflake.dev]`` with
    ``${DEV_SNOWFLAKE_*}`` placeholders for every standard field."""
    result = runner.invoke(app, ["init", str(tmp_path)], env=cli_env)
    assert result.exit_code == 0, result.output

    content = (tmp_path / "carve" / "connections.toml").read_text()
    assert "[snowflake.dev]" in content
    for field in ("ACCOUNT", "USER", "PASSWORD", "ROLE", "WAREHOUSE", "DATABASE"):
        assert f"${{DEV_SNOWFLAKE_{field}}}" in content


def test_init_env_example_has_project_and_dev_blocks(
    runner: CliRunner, tmp_path: Path, cli_env: dict[str, str]
) -> None:
    """``.env.example`` has both the project-wide block (uncommented
    ``ANTHROPIC_API_KEY=``, commented ``GITHUB_TOKEN=``) and the
    ``# === dev target ===`` block with ``DEV_SNOWFLAKE_*`` lines."""
    result = runner.invoke(app, ["init", str(tmp_path)], env=cli_env)
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


def test_init_does_not_create_dotenv_file(
    runner: CliRunner,
    tmp_path: Path,
    cli_env: dict[str, str],
) -> None:
    """``carve init`` writes ``.env.example`` but never ``.env``."""
    result = runner.invoke(app, ["init", str(tmp_path)], env=cli_env)
    assert result.exit_code == 0, result.output

    assert (tmp_path / ".env.example").is_file()
    assert not (tmp_path / ".env").exists()


def test_init_idempotent(runner: CliRunner, tmp_path: Path, cli_env: dict[str, str]) -> None:
    """Running ``carve init`` twice is a no-op: existing files skipped, no
    duplicate ``[snowflake.dev]`` section appended to connections.toml."""
    first = runner.invoke(app, ["init", str(tmp_path)], env=cli_env)
    assert first.exit_code == 0, first.output

    conn_path = tmp_path / "carve" / "connections.toml"
    env_example = tmp_path / ".env.example"
    first_conn = conn_path.read_text()
    first_env = env_example.read_text()

    second = runner.invoke(app, ["init", str(tmp_path)], env=cli_env)
    assert second.exit_code == 0, second.output

    # No duplication of the dev section / env block.
    assert conn_path.read_text() == first_conn
    assert env_example.read_text() == first_env
    assert first_conn.count("[snowflake.dev]") == 1
    assert first_env.count("# === dev target ===") == 1


def test_init_initializes_state_store(
    runner: CliRunner,
    tmp_path: Path,
    cli_env: dict[str, str],
    postgres_state_store_url: str,
) -> None:
    """``carve init`` runs ``alembic upgrade head`` against the resolved
    Postgres state store. v0.1-01 retired SQLite; the state lives in
    Postgres now, and ``cli_env`` routes init at the per-test database.

    The asserted head is read from the alembic ``ScriptDirectory`` so the
    test self-updates as new migrations land — pinning a literal revision
    here would force a manual bump on every new revision and would not
    actually validate the contract we care about (init produces whatever
    head the migration tree currently considers latest).
    """
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory
    from sqlalchemy import text

    result = runner.invoke(app, ["init", str(tmp_path)], env=cli_env)
    assert result.exit_code == 0, result.output

    project_root = Path(__file__).resolve().parents[3]
    alembic_cfg = AlembicConfig(str(project_root / "alembic.ini"))
    expected_head = ScriptDirectory.from_config(alembic_cfg).get_current_head()
    assert expected_head is not None

    engine = create_engine(postgres_state_store_url)
    try:
        inspector = inspect(engine)
        assert "alembic_version" in inspector.get_table_names()
        with engine.connect() as conn:
            row = conn.execute(text("SELECT version_num FROM alembic_version")).fetchone()
            assert row is not None
            assert row[0] == expected_head
    finally:
        engine.dispose()


def test_init_gitignore_uses_root_env(
    runner: CliRunner,
    tmp_path: Path,
    cli_env: dict[str, str],
) -> None:
    """``.gitignore`` ignores root ``.env`` (single line); no per-target
    ``.env`` glob exists in the centralised model."""
    result = runner.invoke(app, ["init", str(tmp_path)], env=cli_env)
    assert result.exit_code == 0, result.output

    content = (tmp_path / ".gitignore").read_text()
    lines = [line.strip() for line in content.splitlines()]
    assert ".env" in lines
    # Single-line .env (no per-target glob).
    assert "targets/*/.env" not in content
    assert "targets/*/.env*" not in content


def test_init_does_not_clobber_existing_files(
    runner: CliRunner, tmp_path: Path, cli_env: dict[str, str]
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

    result = runner.invoke(app, ["init", str(tmp_path)], env=cli_env)
    assert result.exit_code == 0, result.output

    # Files routed through ``_write_if_missing`` are preserved verbatim.
    assert (tmp_path / "carve.toml").read_text() == "# user-customised\n"
    assert (tmp_path / ".gitignore").read_text() == "# user-customised gitignore\n"


def test_init_then_target_create_produces_two_sections(
    runner: CliRunner, tmp_path: Path, cli_env: dict[str, str]
) -> None:
    """``carve init`` followed by ``carve target create staging`` results in
    one centralised ``connections.toml`` with both ``[snowflake.dev]`` and
    ``[snowflake.staging]`` sections."""
    result = runner.invoke(app, ["init", str(tmp_path)], env=cli_env)
    assert result.exit_code == 0, result.output

    result = runner.invoke(
        app, ["target", "create", "staging", "--project-dir", str(tmp_path)], env=cli_env
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

    # P1.1-01: target create no longer creates a `targets/<name>/`
    # filesystem tree. The flat `el/` tree from init suffices.
    assert (tmp_path / "el").is_dir()
    assert not (tmp_path / "targets").exists()


def test_init_escapes_directory_name_with_quotes(
    runner: CliRunner, tmp_path: Path, cli_env: dict[str, str]
) -> None:
    """A directory name containing TOML metacharacters renders as a single
    escaped value rather than breaking the file or injecting bonus tables.
    """
    import tomllib

    weird = tmp_path / 'weird"name\nwith[brackets]'
    weird.mkdir()
    result = runner.invoke(app, ["init", str(weird)], env=cli_env)
    assert result.exit_code == 0, result.output

    text = (weird / "carve.toml").read_text()
    parsed = tomllib.loads(text)
    assert parsed["project"]["name"] == 'weird"name\nwith[brackets]'
    assert parsed["project"]["default_target"] == "dev"
    # No injected tables.
    assert set(parsed.keys()) == {"project", "paths"}


def test_init_refuses_filesystem_root(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_env: dict[str, str]
) -> None:
    """``carve init /`` exits non-zero rather than producing a config with
    an empty project name."""
    # Simulate by chdir'ing somewhere safe and pointing at "/".
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "/"], env=cli_env)
    assert result.exit_code == 2, result.output
    assert "no directory name" in result.output


def test_init_no_longer_creates_targets_subtree(
    runner: CliRunner, tmp_path: Path, cli_env: dict[str, str]
) -> None:
    """P1.1-01: ``carve init`` creates ``el/`` (empty) and does NOT create
    ``targets/``. The target abstraction survives via connections.toml,
    but the per-target filesystem tree is gone."""
    result = runner.invoke(app, ["init", str(tmp_path)], env=cli_env)
    assert result.exit_code == 0, result.output

    # `el/` is created (empty); builds populate it.
    assert (tmp_path / "el").is_dir()
    assert not any((tmp_path / "el").iterdir())

    # `targets/` is NOT created.
    assert not (tmp_path / "targets").exists()


def test_existing_targets_dir_left_alone(
    runner: CliRunner, tmp_path: Path, cli_env: dict[str, str]
) -> None:
    """When the project already has a ``targets/`` directory (pre-P1.1
    layout), ``carve init`` does not touch it — neither delete it nor
    mkdir under it."""
    # Pre-create a stale targets/ tree with sentinel content.
    legacy_dir = tmp_path / "targets" / "dev" / "el"
    legacy_dir.mkdir(parents=True)
    sentinel = legacy_dir / "_sentinel.txt"
    sentinel.write_text("pre-P1.1 content\n", encoding="utf-8")

    result = runner.invoke(app, ["init", str(tmp_path)], env=cli_env)
    assert result.exit_code == 0, result.output

    # Stale tree preserved verbatim.
    assert sentinel.is_file()
    assert sentinel.read_text() == "pre-P1.1 content\n"
    # No mkdir under it either.
    dev_dir = tmp_path / "targets" / "dev"
    assert sorted(child.name for child in dev_dir.iterdir()) == ["el"]


def test_target_create_does_not_create_targets_dir(
    runner: CliRunner, tmp_path: Path, cli_env: dict[str, str]
) -> None:
    """``carve target create staging`` adds the connections.toml section
    and .env.example block; does NOT create ``targets/staging/``."""
    init_result = runner.invoke(app, ["init", str(tmp_path)], env=cli_env)
    assert init_result.exit_code == 0, init_result.output

    result = runner.invoke(
        app, ["target", "create", "staging", "--project-dir", str(tmp_path)], env=cli_env
    )
    assert result.exit_code == 0, result.output

    # Config got the new section / env-example block.
    conn = (tmp_path / "carve" / "connections.toml").read_text()
    assert "[snowflake.staging]" in conn
    env = (tmp_path / ".env.example").read_text()
    assert "# === staging target ===" in env

    # Filesystem: no targets/ tree.
    assert not (tmp_path / "targets").exists()

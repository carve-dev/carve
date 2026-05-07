"""CLI tests for M1-01.

Verify:
- `carve --help` lists all eight commands
- Each command stub exits with code 0
- `carve version` prints the version from `pyproject.toml` (via importlib metadata)
- `carve init` creates the expected file layout in a fresh tmpdir
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from carve.cli.main import app
from carve.core.config import load_config
from carve.version import __version__

EXPECTED_COMMANDS = [
    "init",
    "plan",
    "build",
    "deploy",
    "el",
    "runs",
    "logs",
    "pipelines",
    "serve",
    "target",
    "version",
]


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_help_lists_all_eight_commands(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    for cmd in EXPECTED_COMMANDS:
        assert cmd in result.output, f"missing {cmd!r} in --help output:\n{result.output}"


def test_help_hides_deprecated_run_alias(runner: CliRunner) -> None:
    """`carve run` is hidden from the top-level --help under P1-07.

    It still works (with a deprecation banner) when invoked explicitly
    — `test_carve_run_deprecated_alias_warns_and_forwards` covers that
    behavior. Here we just pin that the listing isn't polluted.
    """
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    # "run" appears as part of the "el run" line in `el --help` (not
    # this view). Top-level should show `el` but not a bare `run`
    # entry. Typer renders hidden commands by suppressing them from the
    # help table.
    lines = [line.strip() for line in result.output.splitlines()]
    bare_run = [
        line for line in lines if line.startswith("run ") or line == "run"
    ]
    assert bare_run == [], f"unexpected `run` in top-level help:\n{result.output}"


def test_version_command_prints_package_version(runner: CliRunner) -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0, result.output
    assert __version__ in result.output


def test_version_matches_pyproject(runner: CliRunner) -> None:
    """`carve version` output must match the `[project].version` in pyproject.toml."""
    import tomllib

    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject_path.open("rb") as f:
        data = tomllib.load(f)
    pyproject_version = data["project"]["version"]

    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert pyproject_version in result.output


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("serve", []),
        ("version", []),
    ],
)
def test_command_stub_exits_zero(runner: CliRunner, command: str, args: list[str]) -> None:
    """Stubs that haven't grown a real implementation yet still exit 0."""
    result = runner.invoke(app, [command, *args])
    assert result.exit_code == 0, result.output


def test_deploy_prints_m2_placeholder(runner: CliRunner) -> None:
    """`carve deploy` is a reserved-verb stub that prints a redirect to `carve run`."""
    result = runner.invoke(app, ["deploy", "my_pipeline"])
    assert result.exit_code == 0, result.output
    assert "M2" in result.output
    assert "carve el run my_pipeline" in result.output


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("plan", ["a goal"]),
        ("build", ["plan-id-123"]),
        ("el", ["run", "my_pipeline"]),
        ("runs", []),
        ("logs", ["run-id-123"]),
        ("pipelines", []),
    ],
)
def test_real_command_exits_2_without_carve_toml(
    runner: CliRunner, tmp_path: Path, command: str, args: list[str]
) -> None:
    """Plan/build/run/runs/logs/pipelines fail with exit 2 when no config.

    Each command loads the merged `Config` and exits 2 on `ConfigError`,
    so invoking them in an empty tmpdir is the simplest way to exercise
    the CLI surface without standing up an Anthropic mock or a state store.
    """
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(app, [command, *args])
    assert result.exit_code == 2, result.output


def test_init_creates_expected_layout(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output

    assert (tmp_path / "carve.toml").is_file()
    assert (tmp_path / "carve" / "connections.toml").is_file()
    assert (tmp_path / "carve" / "runner.toml").is_file()
    assert (tmp_path / "carve" / "models.toml").is_file()
    assert (tmp_path / "carve" / "agents").is_dir()
    assert (tmp_path / "targets" / "dev" / "el").is_dir()
    assert not (tmp_path / "pipelines").exists()
    assert (tmp_path / ".env.example").is_file()
    assert (tmp_path / ".gitignore").is_file()


def test_init_writes_models_toml_placeholder(runner: CliRunner, tmp_path: Path) -> None:
    """`carve init` must drop a placeholder `models.toml` so the user has a clear
    edit target. The file body itself is commented out — the user must uncomment
    and supply real values before `carve plan` will work."""
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output

    content = (tmp_path / "carve" / "models.toml").read_text()
    # Anchor strings — these are the field names a user needs to recognise.
    assert "# anthropic_api_key = " in content
    assert '# default_model = "claude-sonnet-4-5-20250929"' in content
    # `models.toml`'s body *is* the [models] section — the loader merges it
    # under that key, so the file must not declare its own header.
    assert "[models]" not in content
    assert "[anthropic]" not in content
    # The keys must be commented; if they aren't, the loader would parse
    # the placeholder as a real, broken config.
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # No active config lines expected.
        raise AssertionError(f"unexpected active line in models.toml placeholder: {line!r}")


def test_init_writes_connections_toml_template(runner: CliRunner, tmp_path: Path) -> None:
    """`connections.toml` ships a real `[snowflake.dev]` section.

    P1-01 changed init to call ``add_target_to_project("dev", root)``,
    so the section is uncommented and uses ``${DEV_SNOWFLAKE_*}``-prefixed
    placeholders.
    """
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output

    content = (tmp_path / "carve" / "connections.toml").read_text()
    assert "[snowflake.dev]" in content
    assert 'account = "${DEV_SNOWFLAKE_ACCOUNT}"' in content
    assert 'user = "${DEV_SNOWFLAKE_USER}"' in content
    assert 'password = "${DEV_SNOWFLAKE_PASSWORD}"' in content


def test_init_writes_runner_toml_template(runner: CliRunner, tmp_path: Path) -> None:
    """`runner.toml` is the [runner] section — no header inside the file."""
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output

    content = (tmp_path / "carve" / "runner.toml").read_text()
    assert '# type = "local_venv"' in content
    assert "# default_timeout_seconds = 1800" in content
    # Sub-section file: must not declare its own [runner] header.
    assert "[runner]" not in content


def test_init_writes_env_example_template(runner: CliRunner, tmp_path: Path) -> None:
    """`.env.example` lists project-wide vars + a ``# === dev target ===`` block.

    P1-01 introduced target-prefixed env-var names; the dev block lives
    alongside the project-wide section.
    """
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output

    content = (tmp_path / ".env.example").read_text()
    assert "ANTHROPIC_API_KEY=" in content
    # Project-wide ANTHROPIC_API_KEY is uncommented in the new layout.
    assert "# ANTHROPIC_API_KEY=" not in content
    assert "# GITHUB_TOKEN=" in content
    assert "# === Project-wide ===" in content
    assert "# === dev target ===" in content
    assert "DEV_SNOWFLAKE_ACCOUNT=" in content
    assert "DEV_SNOWFLAKE_USER=" in content
    assert "DEV_SNOWFLAKE_PASSWORD=" in content


def test_init_produces_loadable_config(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A freshly-initialised project must round-trip through `load_config()`.

    P1-01 made init write a real ``[snowflake.dev]`` section with
    ``${DEV_SNOWFLAKE_*}`` placeholders, so loading needs those env vars
    populated. We set placeholder values to satisfy the loader.
    """
    monkeypatch.setenv("DEV_SNOWFLAKE_ACCOUNT", "acc")
    monkeypatch.setenv("DEV_SNOWFLAKE_USER", "u")
    monkeypatch.setenv("DEV_SNOWFLAKE_PASSWORD", "p")
    monkeypatch.setenv("DEV_SNOWFLAKE_ROLE", "r")
    monkeypatch.setenv("DEV_SNOWFLAKE_WAREHOUSE", "w")
    monkeypatch.setenv("DEV_SNOWFLAKE_DATABASE", "d")

    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output

    config = load_config(tmp_path)
    assert config.project.name == tmp_path.name
    assert config.models.anthropic_api_key is None
    assert config.models.default_model == "claude-sonnet-4-5-20250929"
    assert config.runner.type == "local_venv"
    assert "dev" in config.connections.snowflake
    assert config.connections.snowflake["dev"].account == "acc"


def test_init_carve_toml_content(runner: CliRunner, tmp_path: Path) -> None:
    """The exact content of `carve.toml` is consumed by the M1-02 loader."""
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output

    content = (tmp_path / "carve.toml").read_text()
    # The project name is detected from the directory name at init time.
    assert f'name = "{tmp_path.name}"' in content
    assert 'default_target = "dev"' in content
    assert 'config_dir = "carve"' in content
    assert 'agents_dir = "carve/agents"' in content
    assert 'targets_dir = "targets"' in content


def test_init_is_idempotent_on_existing_files(runner: CliRunner, tmp_path: Path) -> None:
    """Running `init` twice must not error; existing files are left alone."""
    first = runner.invoke(app, ["init", str(tmp_path)])
    assert first.exit_code == 0

    # Mutate one file; re-running init must not overwrite it.
    sentinel = "# user customization\n"
    (tmp_path / "carve.toml").write_text(sentinel)

    second = runner.invoke(app, ["init", str(tmp_path)])
    assert second.exit_code == 0
    assert (tmp_path / "carve.toml").read_text() == sentinel


# ---------------------------------------------------------------------------
# P1-01: target system regressions
# ---------------------------------------------------------------------------


def test_init_uses_add_target_to_project(runner: CliRunner, tmp_path: Path) -> None:
    """``carve init`` produces the same artifacts that ``carve target create dev``
    would, on a fresh project.

    Both code paths route through ``add_target_to_project("dev", root)``;
    this regression test pins the contract by initialising one project with
    ``init`` and another by stitching together ``init`` + a fresh
    ``target create``, then comparing the dev section + dev env-example
    block + ``targets/dev/el/`` byte-for-byte.
    """
    init_dir = tmp_path / "init"
    create_dir = tmp_path / "create"
    init_dir.mkdir()
    create_dir.mkdir()

    # `init` flow: produces dev directly.
    result = runner.invoke(app, ["init", str(init_dir)])
    assert result.exit_code == 0, result.output

    # `target create` flow: init another project, then delete dev (force +
    # no-default-warning), then re-create dev via `target create`.
    result = runner.invoke(app, ["init", str(create_dir)])
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        app,
        [
            "target",
            "delete",
            "dev",
            "--yes",
            "--force",
            "--no-default-warning",
            "--project-dir",
            str(create_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        app,
        ["target", "create", "dev", "--project-dir", str(create_dir)],
    )
    assert result.exit_code == 0, result.output

    # Pull out the [snowflake.dev] body from each connections.toml.
    init_conn = (init_dir / "carve" / "connections.toml").read_text()
    create_conn = (create_dir / "carve" / "connections.toml").read_text()
    assert _extract_section(init_conn, "snowflake.dev") == _extract_section(
        create_conn, "snowflake.dev"
    )

    # Pull out the # === dev target === block from each .env.example.
    init_env = (init_dir / ".env.example").read_text()
    create_env = (create_dir / ".env.example").read_text()
    assert _extract_env_block(init_env, "dev") == _extract_env_block(create_env, "dev")

    # targets/dev/el/ exists in both.
    assert (init_dir / "targets" / "dev" / "el").is_dir()
    assert (create_dir / "targets" / "dev" / "el").is_dir()


def _extract_section(content: str, header: str) -> list[str]:
    """Return the lines of a TOML section, including the header.

    Stops at the next ``[`` header or EOF.
    """
    lines = content.splitlines()
    out: list[str] = []
    in_section = False
    target = f"[{header}]"
    for line in lines:
        if line.strip() == target:
            in_section = True
            out.append(line)
            continue
        if in_section and line.strip().startswith("[") and line.strip() != target:
            break
        if in_section:
            out.append(line)
    return out


def _extract_env_block(content: str, name: str) -> list[str]:
    """Return the lines of an env-example block for ``name``."""
    lines = content.splitlines()
    out: list[str] = []
    in_block = False
    header = f"# === {name} target ==="
    for line in lines:
        if line.strip() == header:
            in_block = True
            out.append(line)
            continue
        if in_block and line.lstrip().startswith("# ===") and line.strip() != header:
            break
        if in_block:
            out.append(line)
    return out


def test_top_level_target_flag_wired(runner: CliRunner, tmp_path: Path) -> None:
    """Running ``carve --target staging <subcommand>`` stows ``staging`` for
    downstream resolution.

    We exercise this via ``carve target show staging`` after creating the
    target — the show command itself doesn't care about the top-level flag,
    but a successful invocation exercises the typer plumbing for the flag.
    Crucially, the run must not error out due to the new top-level option.
    """
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        app, ["target", "create", "staging", "--project-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output

    # Pass the top-level --target flag and verify it's captured. We use
    # `target show` (passing --project-dir at the subcommand level so it
    # can find the project) to drive an actual command invocation; the
    # primary assertion is that the top-level flag was stowed.
    result = runner.invoke(
        app,
        [
            "--target",
            "staging",
            "target",
            "show",
            "staging",
            "--project-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output

    # Verify it was captured into the module-level slot.
    from carve.cli import main as carve_main

    assert carve_main.ACTIVE_TARGET_FLAG == "staging"


# ---------------------------------------------------------------------------
# M1.1-03: top-level `.env` auto-load callback
# ---------------------------------------------------------------------------


def _arm_probe_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register ``CARVE_DOTENV_PROBE`` with monkeypatch for auto-cleanup.

    ``load_dotenv`` mutates ``os.environ`` directly; pytest's monkeypatch
    only restores keys it was told about. Setting then deleting the key
    through monkeypatch records the original (unset) state so the teardown
    will pop the key regardless of which code path set it.
    """
    monkeypatch.setenv("CARVE_DOTENV_PROBE", "")
    monkeypatch.delenv("CARVE_DOTENV_PROBE", raising=False)


def test_callback_loads_env_before_command(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `.env` in --project-dir is loaded before the subcommand body runs."""
    # tests/conftest.py disables auto-load globally; opt back in for this test.
    monkeypatch.delenv("CARVE_NO_DOTENV", raising=False)
    _arm_probe_cleanup(monkeypatch)
    (tmp_path / ".env").write_text("CARVE_DOTENV_PROBE=from-dotenv\n")

    result = runner.invoke(app, ["--project-dir", str(tmp_path), "version"])

    assert result.exit_code == 0, result.output
    assert os.environ.get("CARVE_DOTENV_PROBE") == "from-dotenv"


def test_callback_respects_carve_no_dotenv(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`CARVE_NO_DOTENV=1` skips the auto-load entirely."""
    monkeypatch.setenv("CARVE_NO_DOTENV", "1")
    _arm_probe_cleanup(monkeypatch)
    (tmp_path / ".env").write_text("CARVE_DOTENV_PROBE=should-not-be-set\n")

    result = runner.invoke(app, ["--project-dir", str(tmp_path), "version"])

    assert result.exit_code == 0, result.output
    assert "CARVE_DOTENV_PROBE" not in os.environ


def test_env_file_option_overrides_default(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--env-file` wins over <project-dir>/.env."""
    monkeypatch.delenv("CARVE_NO_DOTENV", raising=False)
    _arm_probe_cleanup(monkeypatch)
    (tmp_path / ".env").write_text("CARVE_DOTENV_PROBE=default-env\n")
    custom = tmp_path / "custom.env"
    custom.write_text("CARVE_DOTENV_PROBE=custom-env\n")

    result = runner.invoke(
        app,
        [
            "--project-dir",
            str(tmp_path),
            "--env-file",
            str(custom),
            "version",
        ],
    )

    assert result.exit_code == 0, result.output
    assert os.environ.get("CARVE_DOTENV_PROBE") == "custom-env"

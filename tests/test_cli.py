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
    "apply",
    "run",
    "runs",
    "logs",
    "pipelines",
    "serve",
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


def test_apply_prints_m2_placeholder(runner: CliRunner) -> None:
    """`carve apply` is a reserved-verb stub that prints a redirect to `carve run`."""
    result = runner.invoke(app, ["apply", "my_pipeline"])
    assert result.exit_code == 0, result.output
    assert "M2" in result.output
    assert "carve run my_pipeline" in result.output


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("plan", ["a goal"]),
        ("build", ["plan-id-123"]),
        ("run", ["my_pipeline"]),
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
    assert (tmp_path / "pipelines").is_dir()
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
    """`connections.toml` should ship a fully-commented Snowflake template."""
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output

    content = (tmp_path / "carve" / "connections.toml").read_text()
    # Connections is a dict-of-targets shape, so the [snowflake.<target>]
    # header *does* belong in this file (commented out).
    assert "# [snowflake.dev]" in content
    assert '# account = "${SNOWFLAKE_ACCOUNT}"' in content
    # Both alternative auth methods are documented.
    assert 'authenticator = "externalbrowser"' in content
    assert "private_key_path" in content


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
    """`.env.example` should list every env var referenced by the templates."""
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output

    content = (tmp_path / ".env.example").read_text()
    assert "# ANTHROPIC_API_KEY=" in content
    assert "# SNOWFLAKE_ACCOUNT=" in content
    assert "# SNOWFLAKE_USER=" in content
    assert "# SNOWFLAKE_PASSWORD=" in content


def test_init_produces_loadable_config(runner: CliRunner, tmp_path: Path) -> None:
    """A freshly-initialised project must round-trip through `load_config()`.

    The templates are all commented out, so the merged config falls back to
    schema defaults. Crucially, `models.anthropic_api_key` is `None` (the
    field is now optional at load-time); commands that need it raise their
    own ConfigError at use-time.
    """
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output

    config = load_config(tmp_path)
    assert config.project.name == "my-carve-project"
    assert config.models.anthropic_api_key is None
    assert config.models.default_model == "claude-sonnet-4-5-20250929"
    assert config.runner.type == "local_venv"
    assert config.connections.snowflake == {}


def test_init_carve_toml_content(runner: CliRunner, tmp_path: Path) -> None:
    """The exact content of `carve.toml` is consumed by the M1-02 loader."""
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output

    content = (tmp_path / "carve.toml").read_text()
    assert 'name = "my-carve-project"' in content
    assert 'default_target = "dev"' in content
    assert 'config_dir = "carve"' in content


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

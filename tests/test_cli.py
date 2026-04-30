"""CLI tests for M1-01.

Verify:
- `carve --help` lists all eight commands
- Each command stub exits with code 0
- `carve version` prints the version from `pyproject.toml` (via importlib metadata)
- `carve init` creates the expected file layout in a fresh tmpdir
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from carve.cli.main import app
from carve.version import __version__

EXPECTED_COMMANDS = [
    "init",
    "plan",
    "apply",
    "run",
    "runs",
    "logs",
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
        ("run", ["my_pipeline"]),
        ("serve", []),
        ("version", []),
    ],
)
def test_command_stub_exits_zero(runner: CliRunner, command: str, args: list[str]) -> None:
    """Stubs that haven't grown a real implementation yet still exit 0."""
    result = runner.invoke(app, [command, *args])
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("plan", ["a goal"]),
        ("apply", ["plan-id-123"]),
        ("runs", []),
        ("logs", ["run-id-123"]),
    ],
)
def test_real_command_exits_2_without_carve_toml(
    runner: CliRunner, tmp_path: Path, command: str, args: list[str]
) -> None:
    """Plan/apply/runs/logs fail with exit code 2 when run outside a project.

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
    # Body is a comment block (commented out); active config lines don't appear.
    assert "anthropic_api_key" in content
    assert "default_model" in content
    # The keys must be commented; if they aren't, the loader would parse
    # the placeholder as a real, broken config.
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # No active config lines expected.
        raise AssertionError(f"unexpected active line in models.toml placeholder: {line!r}")


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

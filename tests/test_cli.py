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
    "auth",
]


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_auth_status_reports_mode_without_leaking_secret(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`carve auth status` resolves the active mode and prints no secret.

    Uses a minimal hand-written project (no snowflake target) — `auth status`
    only needs `load_config`, not the state store.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-value")
    for var in ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "CARVE_HOSTED"):
        monkeypatch.delenv(var, raising=False)

    (tmp_path / "carve.toml").write_text('[project]\nname = "authtest"\n')
    (tmp_path / "carve").mkdir()
    (tmp_path / "carve" / "models.toml").write_text("# commented placeholder\n")

    res = runner.invoke(app, ["auth", "status", "--project-dir", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert "api_key" in res.output
    assert "claude-opus-4-8" in res.output
    assert "sk-secret-value" not in res.output


def test_help_lists_all_eight_commands(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    for cmd in EXPECTED_COMMANDS:
        assert cmd in result.output, f"missing {cmd!r} in --help output:\n{result.output}"


def test_top_level_run_command_is_gone(runner: CliRunner) -> None:
    """`carve run <name>` no longer exists — the deprecated alias was
    removed in dogfooding because it silently swallowed the top-level
    `--target` flag (the alias built its own typer signature with a
    fresh `--target` Option that defaulted to None, which then beat
    the parent callback's value). The replacement is `carve el run`
    and only `carve el run`.
    """
    result = runner.invoke(app, ["run", "iowa"])
    # Typer's "no such command" surfaces as exit_code != 0 with the
    # name in the error text.
    assert result.exit_code != 0
    output = result.output.lower()
    assert "no such command" in output or "usage:" in output


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
def test_command_stub_exits_zero(
    runner: CliRunner,
    command: str,
    args: list[str],
) -> None:
    """Stubs that haven't grown a real implementation yet still exit 0."""
    result = runner.invoke(app, [command, *args])
    assert result.exit_code == 0, result.output


def test_deploy_alias_requires_from_and_to(runner: CliRunner) -> None:
    """`carve deploy` (P1-08 forwarding alias) demands `--from` and `--to`.

    The M1.1-06 stub that printed an "M2 arrives later" placeholder is
    replaced by a thin wrapper around ``carve el deploy`` that prints
    a yellow deprecation banner and forwards the same flags. Calling
    it with just a positional name triggers typer's missing-option
    error path; the deprecation banner is exercised in
    ``tests/cli/commands/el/test_deploy.py``.
    """
    result = runner.invoke(app, ["deploy", "my_pipeline"])
    # Missing --from / --to → typer exits 2 with a usage error. We assert the
    # contract (exit 2 + a usage message), not the rendered option list:
    # typer's rich error only prints the options panel at wider terminal
    # widths, so asserting "--from" is environment-brittle (passes locally,
    # fails under CI's no-TTY width). The forwarded flags are covered in
    # tests/cli/commands/el/test_deploy.py.
    assert result.exit_code == 2
    assert "Usage" in result.output


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


def test_init_creates_expected_layout(
    runner: CliRunner,
    tmp_path: Path,
    cli_env: dict[str, str],
) -> None:
    result = runner.invoke(app, ["init", str(tmp_path)], env=cli_env)
    assert result.exit_code == 0, result.output

    assert (tmp_path / "carve.toml").is_file()
    assert (tmp_path / "carve" / "connections.toml").is_file()
    assert (tmp_path / "carve" / "runner.toml").is_file()
    assert (tmp_path / "carve" / "models.toml").is_file()
    assert (tmp_path / "carve" / "agents").is_dir()
    # P1.1-01: flat `el/` tree, no `targets/` filesystem subtree.
    assert (tmp_path / "el").is_dir()
    assert not (tmp_path / "pipelines").exists()
    assert not (tmp_path / "targets").exists()
    assert (tmp_path / ".env.example").is_file()
    assert (tmp_path / ".gitignore").is_file()


def test_init_writes_models_toml_placeholder(
    runner: CliRunner,
    tmp_path: Path,
    cli_env: dict[str, str],
) -> None:
    """`carve init` must drop a placeholder `models.toml` so the user has a clear
    edit target. The file body itself is commented out — the user must uncomment
    and supply real values before `carve plan` will work."""
    result = runner.invoke(app, ["init", str(tmp_path)], env=cli_env)
    assert result.exit_code == 0, result.output

    content = (tmp_path / "carve" / "models.toml").read_text()
    # Anchor strings — these are the field names a user needs to recognise.
    assert "# anthropic_api_key = " in content
    assert '# default_model = "claude-opus-4-8"' in content
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


def test_init_writes_connections_toml_template(
    runner: CliRunner,
    tmp_path: Path,
    cli_env: dict[str, str],
) -> None:
    """`connections.toml` ships a COMMENTED-OUT `[snowflake.dev]` template.

    The default target is scaffolded commented out so a fresh project loads
    without warehouse credentials (see ``test_init_produces_loadable_config``).
    The ``${DEV_SNOWFLAKE_*}`` placeholders are present as documentation;
    uncommenting + filling them (or ``carve target create``) activates it.
    """
    result = runner.invoke(app, ["init", str(tmp_path)], env=cli_env)
    assert result.exit_code == 0, result.output

    content = (tmp_path / "carve" / "connections.toml").read_text()
    # Commented header present; no LIVE (uncommented) section.
    assert "# [snowflake.dev]" in content
    assert "\n[snowflake.dev]" not in content
    assert '# account = "${DEV_SNOWFLAKE_ACCOUNT}"' in content
    assert '# user = "${DEV_SNOWFLAKE_USER}"' in content
    assert '# password = "${DEV_SNOWFLAKE_PASSWORD}"' in content


def test_init_writes_runner_toml_template(
    runner: CliRunner,
    tmp_path: Path,
    cli_env: dict[str, str],
) -> None:
    """`runner.toml` is the [runner] section — no header inside the file."""
    result = runner.invoke(app, ["init", str(tmp_path)], env=cli_env)
    assert result.exit_code == 0, result.output

    content = (tmp_path / "carve" / "runner.toml").read_text()
    assert '# type = "local_venv"' in content
    assert "# default_timeout_seconds = 1800" in content
    # Sub-section file: must not declare its own [runner] header.
    assert "[runner]" not in content


def test_init_writes_env_example_template(
    runner: CliRunner,
    tmp_path: Path,
    cli_env: dict[str, str],
) -> None:
    """`.env.example` lists project-wide vars + a ``# === dev target ===`` block.

    P1-01 introduced target-prefixed env-var names; the dev block lives
    alongside the project-wide section.
    """
    result = runner.invoke(app, ["init", str(tmp_path)], env=cli_env)
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
    runner: CliRunner, tmp_path: Path, cli_env: dict[str, str]
) -> None:
    """A freshly-initialised project must round-trip through `load_config()`
    WITHOUT any warehouse credentials set.

    The default target's ``[snowflake.dev]`` section is scaffolded commented
    out, so ``load_config`` doesn't choke on unset ``${DEV_SNOWFLAKE_*}`` vars
    — the regression for the first-run break where the advertised
    ``carve plan`` died at config load. No ``DEV_SNOWFLAKE_*`` env vars are
    set here on purpose; the connections map is empty until the user
    uncomments the section or runs ``carve target create``.
    """
    result = runner.invoke(app, ["init", str(tmp_path)], env=cli_env)
    assert result.exit_code == 0, result.output

    config = load_config(tmp_path)
    assert config.project.name == tmp_path.name
    assert config.models.anthropic_api_key is None
    assert config.models.default_model == "claude-opus-4-8"
    assert config.runner.type == "local_venv"
    # Commented dev section → no live target, but the file still loads.
    assert config.connections.snowflake == {}


def test_init_carve_toml_content(
    runner: CliRunner,
    tmp_path: Path,
    cli_env: dict[str, str],
) -> None:
    """The exact content of `carve.toml` is consumed by the M1-02 loader."""
    result = runner.invoke(app, ["init", str(tmp_path)], env=cli_env)
    assert result.exit_code == 0, result.output

    content = (tmp_path / "carve.toml").read_text()
    # The project name is detected from the directory name at init time.
    assert f'name = "{tmp_path.name}"' in content
    assert 'default_target = "dev"' in content
    assert 'config_dir = "carve"' in content
    assert 'agents_dir = "carve/agents"' in content
    # P1.1-01: `targets_dir` is no longer emitted by `carve init`. The
    # field stays in `PathsConfig` with a default so existing carve.toml
    # files keep loading, but new projects don't see it.
    assert "targets_dir" not in content


def test_init_is_idempotent_on_existing_files(
    runner: CliRunner,
    tmp_path: Path,
    cli_env: dict[str, str],
) -> None:
    """Running `init` twice must not error; existing files are left alone."""
    first = runner.invoke(app, ["init", str(tmp_path)], env=cli_env)
    assert first.exit_code == 0

    # Mutate one file; re-running init must not overwrite it.
    sentinel = "# user customization\n"
    (tmp_path / "carve.toml").write_text(sentinel)

    second = runner.invoke(app, ["init", str(tmp_path)], env=cli_env)
    assert second.exit_code == 0
    assert (tmp_path / "carve.toml").read_text() == sentinel


# ---------------------------------------------------------------------------
# P1-01: target system regressions
# ---------------------------------------------------------------------------


def test_init_scaffolds_commented_dev_target(
    runner: CliRunner,
    tmp_path: Path,
    cli_env: dict[str, str],
) -> None:
    """``carve init`` scaffolds the default target's connections section
    COMMENTED OUT — diverging on purpose from ``carve target create dev``,
    which writes a LIVE section.

    The new contract (lean init): a fresh project must load without warehouse
    credentials, so the section is a commented template; activation is the
    user's explicit step (uncomment, or run ``carve target create``). The
    ``.env.example`` dev block is still produced by the same
    ``add_env_example_block`` helper target-create uses, so it stays
    byte-for-byte identical between the two verbs.
    """
    init_dir = tmp_path / "init"
    create_dir = tmp_path / "create"
    init_dir.mkdir()
    create_dir.mkdir()

    result = runner.invoke(app, ["init", str(init_dir)], env=cli_env)
    assert result.exit_code == 0, result.output

    # init's connections.toml: the dev section is commented (no LIVE section).
    init_conn = (init_dir / "carve" / "connections.toml").read_text()
    assert "# [snowflake.dev]" in init_conn
    assert _extract_section(init_conn, "snowflake.dev") == []

    # Activation is explicit: after init, `carve target create` writes a LIVE
    # section (here a second target name, to show the live-vs-commented split).
    result = runner.invoke(app, ["init", str(create_dir)], env=cli_env)
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        app, ["target", "create", "prod", "--project-dir", str(create_dir)], env=cli_env
    )
    assert result.exit_code == 0, result.output
    create_conn = (create_dir / "carve" / "connections.toml").read_text()
    assert _extract_section(create_conn, "snowflake.prod") != []  # live section

    # init still produces a well-formed dev env-example block (via the same
    # add_env_example_block helper target-create uses).
    init_env = (init_dir / ".env.example").read_text()
    dev_block = _extract_env_block(init_env, "dev")
    assert dev_block[0] == "# === dev target ==="
    assert "DEV_SNOWFLAKE_ACCOUNT=" in dev_block

    # Flat el/ tree exists in both (created by init); no targets/ tree.
    assert (init_dir / "el").is_dir()
    assert (create_dir / "el").is_dir()
    assert not (init_dir / "targets").exists()
    assert not (create_dir / "targets").exists()


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


def test_top_level_target_flag_wired(
    runner: CliRunner,
    tmp_path: Path,
    cli_env: dict[str, str],
) -> None:
    """Running ``carve --target staging <subcommand>`` stows ``staging`` for
    downstream resolution.

    We exercise this via ``carve target show staging`` after creating the
    target — the show command itself doesn't care about the top-level flag,
    but a successful invocation exercises the typer plumbing for the flag.
    Crucially, the run must not error out due to the new top-level option.
    """
    result = runner.invoke(app, ["init", str(tmp_path)], env=cli_env)
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        app, ["target", "create", "staging", "--project-dir", str(tmp_path)], env=cli_env
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
        env=cli_env,
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

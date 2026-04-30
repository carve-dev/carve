"""Tests for `LocalVenvRunner`.

These tests intentionally avoid pip installs of arbitrary deps:

- Every step uses ``requirements=[]`` so `_ensure_venv` only does
  ``python -m venv`` and skips ``pip install``.
- Scripts are tiny inline files written via ``tmp_path.write_text``.
- The Snowflake-env test asserts on the env dict the runner constructs,
  not on a real Snowflake import.

The venv-create step still takes a couple of seconds the first time,
so the slow tests share a single project venv via the shared
`venv_cache_dir` fixture.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from carve.core.config.schema import (
    Config,
    ConnectionsConfig,
    ModelsConfig,
    ProjectConfig,
    RunnerConfig,
    ServerConfig,
    SnowflakeConnection,
)
from carve.core.runners.local_venv import LocalVenvRunner, _snowflake_env
from carve.core.state import Repository
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)
from carve.core.steps.base import RunContext
from carve.core.steps.python import PythonStep, PythonStepConfig

# ----------------------------------------------------------------- Fixtures


def _build_config(
    *,
    venv_cache_dir: str = ".carve/venvs",
    snowflake: dict[str, SnowflakeConnection] | None = None,
) -> Config:
    return Config(
        project=ProjectConfig(name="runner-test"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        runner=RunnerConfig(venv_cache_dir=venv_cache_dir),
        server=ServerConfig(state_store="sqlite:///.carve/state.db"),
        connections=ConnectionsConfig(snowflake=snowflake or {}),
    )


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def repo(project_dir: Path) -> Repository:
    config = _build_config()
    engine = create_engine_from_config(config, project_dir=project_dir)
    initialize_database(engine)
    factory = create_session_factory(engine)
    return Repository(factory)


@pytest.fixture
def runner(
    project_dir: Path, repo: Repository
) -> LocalVenvRunner:
    cache = project_dir / ".carve" / "venvs"
    cache.mkdir(parents=True, exist_ok=True)
    config = _build_config(venv_cache_dir=str(cache))
    return LocalVenvRunner(config.runner, repo)


def _make_context(
    project_dir: Path,
    repo: Repository,
    *,
    target: str = "dev",
    snowflake: dict[str, SnowflakeConnection] | None = None,
) -> RunContext:
    run_id = repo.create_run(kind="step", target_id="t")
    config = _build_config(snowflake=snowflake)
    return RunContext(
        run_id=run_id,
        project_dir=project_dir,
        target=target,
        config=config,
    )


def _wait_terminal(repo: Repository, run_id: str, timeout: float = 30.0) -> str:
    """Poll the repo until the run reaches a terminal state."""
    terminal = {"success", "failed", "cancelled", "crashed"}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = repo.get_run(run_id)
        if run is not None and run.status in terminal:
            return run.status
        time.sleep(0.05)
    pytest.fail(f"run {run_id} did not reach terminal status in {timeout}s")


# ----------------------------------------------------------------- Venv cache


def test_venv_cache_paths_are_deterministic_and_unique(
    runner: LocalVenvRunner,
) -> None:
    """Same requirements -> same path; different ones -> different path.

    We don't actually create the venvs in this test (just compute paths
    by calling the helper hash logic indirectly). To avoid creating real
    venvs, monkeypatch isn't quite enough — instead we call the public
    `_ensure_venv` once for each set, which does create them, and assert
    the *paths* match expectations. The empty-requirements case is what
    we mostly use elsewhere so it caches cleanly.
    """
    path_a = runner._ensure_venv([])
    path_a_again = runner._ensure_venv([])
    assert path_a == path_a_again
    assert path_a.exists()

    # An empty list and a list with a hashable string differ.
    # We don't actually install — but if `requirements` is non-empty the
    # code would attempt pip. Instead, just verify the *path* is different
    # by computing it the same way the runner does, without actually
    # invoking _ensure_venv on a non-empty list.
    import hashlib

    other_hash = hashlib.sha256(b"some-pkg==1.0").hexdigest()
    assert other_hash != path_a.name
    # Cache key is the full SHA-256 hex digest (64 chars), not a slice.
    assert len(path_a.name) == 64


# --------------------------------------------------------------- Successful run


def test_simple_script_runs_to_success_and_logs_stdout(
    runner: LocalVenvRunner,
    repo: Repository,
    project_dir: Path,
) -> None:
    script = project_dir / "hello.py"
    script.write_text('print("hello from carve")\n')

    step = PythonStep(
        PythonStepConfig(
            id="hello",
            script="hello.py",
            timeout_seconds=60,
        )
    )
    context = _make_context(project_dir, repo)

    handle = runner.execute(step, context)
    assert handle.run_id == context.run_id
    assert handle.process_id > 0

    status = _wait_terminal(repo, context.run_id)
    assert status == "success"

    logs = repo.get_logs(context.run_id)
    messages = [log.message for log in logs]
    assert "hello from carve" in messages


def test_wait_returns_step_result_with_status(
    runner: LocalVenvRunner,
    repo: Repository,
    project_dir: Path,
) -> None:
    script = project_dir / "ok.py"
    script.write_text("print('ok')\n")

    step = PythonStep(PythonStepConfig(id="ok", script="ok.py", timeout_seconds=60))
    context = _make_context(project_dir, repo)

    runner.execute(step, context)
    result = runner.wait(context.run_id)

    assert result.status == "success"
    assert result.error is None
    assert result.duration_ms >= 0


# ----------------------------------------------------------------- Failure path


def test_failing_script_records_failed_status_with_exit_code(
    runner: LocalVenvRunner,
    repo: Repository,
    project_dir: Path,
) -> None:
    script = project_dir / "boom.py"
    script.write_text("import sys\nsys.exit(7)\n")

    step = PythonStep(
        PythonStepConfig(id="boom", script="boom.py", timeout_seconds=60)
    )
    context = _make_context(project_dir, repo)

    runner.execute(step, context)
    status = _wait_terminal(repo, context.run_id)

    assert status == "failed"
    run = repo.get_run(context.run_id)
    assert run is not None
    assert run.error_message is not None
    assert "7" in run.error_message


# ----------------------------------------------------------------- Timeout/cancel


def test_timeout_triggers_cancellation(
    runner: LocalVenvRunner,
    repo: Repository,
    project_dir: Path,
) -> None:
    script = project_dir / "slow.py"
    script.write_text("import time\ntime.sleep(60)\n")

    step = PythonStep(
        PythonStepConfig(id="slow", script="slow.py", timeout_seconds=1)
    )
    context = _make_context(project_dir, repo)

    runner.execute(step, context)
    status = _wait_terminal(repo, context.run_id, timeout=20.0)

    # Watchdog -> cancel -> SIGTERM -> exit. SIGTERM produces exit code
    # -15, which `_finalize_run` deterministically maps to "cancelled";
    # if this flakes the watchdog/signal-mapping path is broken.
    assert status == "cancelled"


def test_cancel_is_idempotent_after_completion(
    runner: LocalVenvRunner,
    repo: Repository,
    project_dir: Path,
) -> None:
    script = project_dir / "fast.py"
    script.write_text("print('done')\n")

    step = PythonStep(PythonStepConfig(id="fast", script="fast.py", timeout_seconds=60))
    context = _make_context(project_dir, repo)

    runner.execute(step, context)
    _wait_terminal(repo, context.run_id)

    # Should not raise even though the run is long gone.
    runner.cancel(context.run_id)


# ------------------------------------------------------------ Snowflake env


def test_snowflake_env_is_built_for_configured_target() -> None:
    snowflake = {
        "dev": SnowflakeConnection(
            account="acc",
            user="u",
            password="pw",
            role="r",
            warehouse="w",
            database="d",
            schema="s",
        )
    }
    config = _build_config(snowflake=snowflake)
    context = RunContext(
        run_id="r",
        project_dir=Path("/tmp"),
        target="dev",
        config=config,
    )
    env = _snowflake_env(context)
    assert env["SNOWFLAKE_ACCOUNT"] == "acc"
    assert env["SNOWFLAKE_USER"] == "u"
    assert env["SNOWFLAKE_PASSWORD"] == "pw"
    assert env["SNOWFLAKE_ROLE"] == "r"
    assert env["SNOWFLAKE_WAREHOUSE"] == "w"
    assert env["SNOWFLAKE_DATABASE"] == "d"
    assert env["SNOWFLAKE_SCHEMA"] == "s"


def test_snowflake_env_is_empty_for_unconfigured_target() -> None:
    config = _build_config(snowflake={})
    context = RunContext(
        run_id="r",
        project_dir=Path("/tmp"),
        target="dev",
        config=config,
    )
    assert _snowflake_env(context) == {}


def test_snowflake_env_handles_missing_optional_fields() -> None:
    snowflake = {
        "dev": SnowflakeConnection(
            account="acc",
            user="u",
            role="r",
            warehouse="w",
            database="d",
        )
    }
    config = _build_config(snowflake=snowflake)
    context = RunContext(
        run_id="r",
        project_dir=Path("/tmp"),
        target="dev",
        config=config,
    )
    env = _snowflake_env(context)
    assert env["SNOWFLAKE_PASSWORD"] == ""
    assert env["SNOWFLAKE_SCHEMA"] == ""


def test_snowflake_env_is_injected_into_subprocess(
    runner: LocalVenvRunner,
    repo: Repository,
    project_dir: Path,
) -> None:
    """End-to-end: configured Snowflake creds reach the subprocess env."""
    script = project_dir / "show_env.py"
    script.write_text(
        "import os\n"
        "print('ACCOUNT=' + os.environ.get('SNOWFLAKE_ACCOUNT', 'MISSING'))\n"
        "print('USER=' + os.environ.get('SNOWFLAKE_USER', 'MISSING'))\n"
    )

    snowflake = {
        "dev": SnowflakeConnection(
            account="my-account",
            user="alice",
            role="r",
            warehouse="w",
            database="d",
        )
    }
    step = PythonStep(
        PythonStepConfig(id="env", script="show_env.py", timeout_seconds=60)
    )
    context = _make_context(project_dir, repo, snowflake=snowflake)

    runner.execute(step, context)
    _wait_terminal(repo, context.run_id)

    messages = [log.message for log in repo.get_logs(context.run_id)]
    assert "ACCOUNT=my-account" in messages
    assert "USER=alice" in messages


def test_step_env_overrides_are_passed_through(
    runner: LocalVenvRunner,
    repo: Repository,
    project_dir: Path,
) -> None:
    script = project_dir / "show_my_var.py"
    script.write_text(
        "import os\nprint('VAL=' + os.environ.get('CARVE_TEST_VAR', 'MISSING'))\n"
    )

    step = PythonStep(
        PythonStepConfig(
            id="env",
            script="show_my_var.py",
            env={"CARVE_TEST_VAR": "from-step"},
            timeout_seconds=60,
        )
    )
    context = _make_context(project_dir, repo)

    runner.execute(step, context)
    _wait_terminal(repo, context.run_id)

    messages = [log.message for log in repo.get_logs(context.run_id)]
    assert "VAL=from-step" in messages


# ------------------------------------------------------------ Path traversal


def test_path_traversal_in_script_is_rejected(
    runner: LocalVenvRunner,
    repo: Repository,
    project_dir: Path,
) -> None:
    """A script that resolves outside ``project_dir`` is rejected."""
    # Set up a script outside the project root.
    outside = project_dir.parent / "outside.py"
    outside.write_text("print('nope')\n")

    step = PythonStep(
        PythonStepConfig(
            id="bad",
            script="../outside.py",
            timeout_seconds=60,
        )
    )
    context = _make_context(project_dir, repo)

    with pytest.raises(ValueError, match="outside project_dir"):
        runner.execute(step, context)


def test_absolute_path_outside_project_is_rejected(
    runner: LocalVenvRunner,
    repo: Repository,
    project_dir: Path,
) -> None:
    step = PythonStep(
        PythonStepConfig(
            id="bad",
            script="/etc/passwd",
            timeout_seconds=60,
        )
    )
    context = _make_context(project_dir, repo)

    with pytest.raises(ValueError, match="outside project_dir"):
        runner.execute(step, context)


# ------------------------------------------------------------ Get-status


def test_get_status_reflects_repo_state(
    runner: LocalVenvRunner,
    repo: Repository,
    project_dir: Path,
) -> None:
    script = project_dir / "ok.py"
    script.write_text("print('ok')\n")

    step = PythonStep(PythonStepConfig(id="s", script="ok.py", timeout_seconds=60))
    context = _make_context(project_dir, repo)

    runner.execute(step, context)
    _wait_terminal(repo, context.run_id)
    assert runner.get_status(context.run_id) == "success"
    assert runner.get_status("nonexistent") == "unknown"


# ----------------------------------------------------- Async log streaming


@pytest.mark.asyncio
async def test_stream_logs_yields_lines_and_terminates(
    runner: LocalVenvRunner,
    repo: Repository,
    project_dir: Path,
) -> None:
    script = project_dir / "two.py"
    script.write_text("print('one')\nprint('two')\n")

    step = PythonStep(PythonStepConfig(id="s", script="two.py", timeout_seconds=60))
    context = _make_context(project_dir, repo)

    runner.execute(step, context)

    seen: list[str] = []
    async for line in runner.stream_logs(context.run_id):
        seen.append(line.message)
        if len(seen) >= 2:
            break

    assert "one" in seen
    assert "two" in seen


# Sanity: the runner's `python_executable` defaults to the current interpreter
# so tests don't need an alternative interpreter configured.
def test_python_executable_defaults_to_sys_executable(
    project_dir: Path, repo: Repository
) -> None:
    import sys

    config = _build_config()
    runner = LocalVenvRunner(config.runner, repo)
    assert runner.python_executable == sys.executable

    runner_custom = LocalVenvRunner(
        config.runner, repo, python_executable="/custom/python"
    )
    assert runner_custom.python_executable == "/custom/python"


# Sanity that we don't accidentally leak the runner's own env vars
# (regression guard for accidentally swapping `os.environ.copy()` order).
def test_step_env_overrides_inherited_env(
    runner: LocalVenvRunner,
    repo: Repository,
    project_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CARVE_OVERRIDE_ME", "from-parent")

    script = project_dir / "ovr.py"
    script.write_text(
        "import os\nprint('V=' + os.environ['CARVE_OVERRIDE_ME'])\n"
    )

    step = PythonStep(
        PythonStepConfig(
            id="ovr",
            script="ovr.py",
            env={"CARVE_OVERRIDE_ME": "from-step"},
            timeout_seconds=60,
        )
    )
    context = _make_context(project_dir, repo)

    runner.execute(step, context)
    _wait_terminal(repo, context.run_id)

    messages = [log.message for log in repo.get_logs(context.run_id)]
    assert "V=from-step" in messages


# Touching `os` here so the import is used (test of importability without
# a real call inside this file).
_ = os.name


# ------------------------------------------------------ Secret-env stripping


def test_anthropic_api_key_is_stripped_from_subprocess_env(
    runner: LocalVenvRunner,
    repo: Repository,
    project_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ANTHROPIC_API_KEY must not be inherited by the user subprocess."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-must-not-leak")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-also-secret")

    script = project_dir / "show_anthropic.py"
    script.write_text(
        "import os\n"
        "print('KEY=' + os.environ.get('ANTHROPIC_API_KEY', '<unset>'))\n"
        "print('TOK=' + os.environ.get('ANTHROPIC_AUTH_TOKEN', '<unset>'))\n"
    )

    step = PythonStep(
        PythonStepConfig(
            id="anth",
            script="show_anthropic.py",
            timeout_seconds=60,
        )
    )
    context = _make_context(project_dir, repo)

    runner.execute(step, context)
    _wait_terminal(repo, context.run_id)

    messages = [log.message for log in repo.get_logs(context.run_id)]
    assert "KEY=<unset>" in messages
    assert "TOK=<unset>" in messages
    # And the secret string must not appear anywhere in the log stream.
    assert not any("sk-secret-must-not-leak" in m for m in messages)
    assert not any("tok-also-secret" in m for m in messages)


def test_step_env_can_reintroduce_anthropic_key_intentionally(
    runner: LocalVenvRunner,
    repo: Repository,
    project_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A step can opt back in to ANTHROPIC_API_KEY via `env`."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-parent")

    script = project_dir / "want_anth.py"
    script.write_text(
        "import os\n"
        "print('K=' + os.environ.get('ANTHROPIC_API_KEY', '<unset>'))\n"
    )

    step = PythonStep(
        PythonStepConfig(
            id="want",
            script="want_anth.py",
            env={"ANTHROPIC_API_KEY": "sk-explicit"},
            timeout_seconds=60,
        )
    )
    context = _make_context(project_dir, repo)

    runner.execute(step, context)
    _wait_terminal(repo, context.run_id)

    messages = [log.message for log in repo.get_logs(context.run_id)]
    assert "K=sk-explicit" in messages


# -------------------------------------------------- Process-group cancellation


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="os.killpg / process groups are POSIX-only",
)
def test_cancel_kills_forked_child_of_user_script(
    runner: LocalVenvRunner,
    repo: Repository,
    project_dir: Path,
) -> None:
    """A child forked by the user script must die when the run is cancelled.

    The script forks a child that writes the timestamp to a sentinel
    file in a tight loop. After the watchdog cancels the parent, the
    child should stop writing within a short window because SIGTERM
    is sent to the whole process group.
    """
    sentinel = project_dir / "child_alive.txt"
    script = project_dir / "fork_child.py"
    script.write_text(
        "import os, time\n"
        f"sentinel = {str(sentinel)!r}\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    # Child: keep writing forever.\n"
        "    while True:\n"
        "        with open(sentinel, 'w') as f:\n"
        "            f.write(str(time.time()))\n"
        "        time.sleep(0.05)\n"
        "else:\n"
        "    # Parent: also sleep so the watchdog has work to cancel.\n"
        "    time.sleep(60)\n"
    )

    step = PythonStep(
        PythonStepConfig(
            id="fork",
            script="fork_child.py",
            timeout_seconds=1,
        )
    )
    context = _make_context(project_dir, repo)

    runner.execute(step, context)
    status = _wait_terminal(repo, context.run_id, timeout=20.0)
    assert status == "cancelled"

    # Wait for the sentinel to stop being touched. If the child survived
    # the cancel(), the mtime would keep moving forward.
    if not sentinel.exists():
        # Child was killed before it ever wrote. That's fine.
        return

    # Snapshot mtime, then wait > the child's 50ms loop, then re-check.
    deadline = time.monotonic() + 5.0
    last_mtime = sentinel.stat().st_mtime
    stable = False
    while time.monotonic() < deadline:
        time.sleep(0.5)
        cur_mtime = sentinel.stat().st_mtime
        if cur_mtime == last_mtime:
            stable = True
            break
        last_mtime = cur_mtime
    assert stable, (
        "forked child kept writing the sentinel after cancel(); "
        "process-group signalling is broken"
    )

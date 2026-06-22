"""Tests for `PythonStep` and its config.

The step itself is a passive config holder, so these tests focus on
config validation: defaults, required fields, extra-field rejection,
and `validate()` round-tripping through a TOML-shaped dict.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from carve.core.steps.python import PythonStep, PythonStepConfig


def test_python_step_config_requires_id_and_script() -> None:
    with pytest.raises(ValidationError):
        PythonStepConfig.model_validate({"script": "x.py"})  # missing id

    with pytest.raises(ValidationError):
        PythonStepConfig.model_validate({"id": "s1"})  # missing script


def test_python_step_config_defaults_are_sensible() -> None:
    cfg = PythonStepConfig(id="s1", script="x.py")
    assert cfg.requirements == []
    assert cfg.env == {}
    assert cfg.timeout_seconds == 1800
    assert cfg.retries == 0


def test_python_step_config_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        PythonStepConfig.model_validate({"id": "s1", "script": "x.py", "unknown": True})


def test_python_step_holds_its_config_and_step_type_string() -> None:
    cfg = PythonStepConfig(id="s1", script="x.py")
    step = PythonStep(cfg)
    assert step.config is cfg
    assert step.step_type == "python"
    assert PythonStep.step_type == "python"


def test_python_step_config_rejects_pip_flag_in_requirements() -> None:
    """Entries that look like pip flags must fail validation.

    Without this guard, a requirement like ``--index-url=...`` would be
    handed to ``pip install`` and could redirect installs to an
    attacker-controlled index. The runner also passes ``--`` to pip as
    defence in depth, but config-time rejection makes the misuse
    obvious before any subprocess runs.
    """
    with pytest.raises(ValidationError, match="must be package specs"):
        PythonStepConfig(
            id="s1",
            script="x.py",
            requirements=["--index-url=http://evil/simple"],
        )

    with pytest.raises(ValidationError, match="must be package specs"):
        PythonStepConfig(
            id="s1",
            script="x.py",
            requirements=["-r", "/tmp/x.txt"],
        )

    with pytest.raises(ValidationError, match="must be package specs"):
        PythonStepConfig(
            id="s1",
            script="x.py",
            requirements=["valid-pkg==1.0", "--bad"],
        )


def test_python_step_validate_parses_dict_into_config() -> None:
    step = PythonStep(PythonStepConfig(id="s1", script="placeholder.py"))
    parsed = step.validate(
        {
            "id": "s2",
            "script": "ingest.py",
            "requirements": ["snowflake-connector-python==3.7.0"],
            "env": {"SOME_FLAG": "1"},
            "timeout_seconds": 60,
        }
    )
    assert isinstance(parsed, PythonStepConfig)
    assert parsed.id == "s2"
    assert parsed.script == "ingest.py"
    assert parsed.requirements == ["snowflake-connector-python==3.7.0"]
    assert parsed.env == {"SOME_FLAG": "1"}
    assert parsed.timeout_seconds == 60

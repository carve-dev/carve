"""`[runtime.archive]` config — schema defaults/validation + loader wiring.

Two layers:

* schema-only (no filesystem): :func:`parse_duration`, :class:`ArchiveConfig`
  defaults + window/interval validation, and ``extra="forbid"``;
* loader (writes a ``runtime.toml`` into a temp project): ``[runtime.archive]``
  parses into ``Config.runtime.archive``, an absent ``runtime.toml`` falls back
  to defaults, and an invalid window / unknown key surfaces as a ``ConfigError``.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from carve.core.config import ConfigError, load_config
from carve.core.config.schema import ArchiveConfig, Config, RuntimeConfig, parse_duration

# --------------------------------------------------------------------------- parse_duration


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("7d", timedelta(days=7)),
        ("30d", timedelta(days=30)),
        ("3600s", timedelta(seconds=3600)),
        ("12h", timedelta(hours=12)),
        ("15m", timedelta(minutes=15)),
        (" 7d ", timedelta(days=7)),  # surrounding whitespace tolerated
    ],
)
def test_parse_duration_parses_supported_units(text: str, expected: timedelta) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["0d", "0s", "soon", "7", "7w", "-1d", "", "d", "1.5d"])
def test_parse_duration_rejects_bad_or_nonpositive(text: str) -> None:
    with pytest.raises(ValueError, match="duration"):
        parse_duration(text)


# --------------------------------------------------------------------------- ArchiveConfig


def test_archive_config_defaults() -> None:
    cfg = ArchiveConfig()
    assert cfg.interval_s == 3600
    assert cfg.jobs_window == "7d"
    assert cfg.runs_window == "30d"
    assert cfg.logs_window == "30d"
    assert cfg.steps_window == "30d"


def test_archive_config_accepts_custom_windows() -> None:
    cfg = ArchiveConfig(interval_s=120, jobs_window="14d", logs_window="48h")
    assert cfg.interval_s == 120
    assert cfg.jobs_window == "14d"
    assert cfg.logs_window == "48h"


def test_archive_config_rejects_unparseable_window() -> None:
    with pytest.raises(ValidationError):
        ArchiveConfig(jobs_window="whenever")


def test_archive_config_rejects_nonpositive_window() -> None:
    with pytest.raises(ValidationError):
        ArchiveConfig(runs_window="0d")


def test_archive_config_rejects_nonpositive_interval() -> None:
    with pytest.raises(ValidationError):
        ArchiveConfig(interval_s=0)


def test_archive_config_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ArchiveConfig(weeks_window="2w")  # type: ignore[call-arg]


def test_runtime_config_defaults_and_forbids_extra() -> None:
    cfg = RuntimeConfig()
    assert cfg.archive.jobs_window == "7d"
    with pytest.raises(ValidationError):
        RuntimeConfig(stale_threshold_s=10)  # type: ignore[call-arg]


def test_config_runtime_defaults_present() -> None:
    cfg = Config.model_validate({"project": {"name": "x"}, "models": {"anthropic_api_key": "k"}})
    assert cfg.runtime.archive.interval_s == 3600


# --------------------------------------------------------------------------- loader


def _write_project(root: Path, runtime_toml: str | None = None) -> None:
    (root / "carve.toml").write_text('[project]\nname = "rt"\n\n[paths]\nconfig_dir = "carve"\n')
    (root / "carve").mkdir()
    (root / "carve" / "models.toml").write_text('anthropic_api_key = "k"\n')
    if runtime_toml is not None:
        (root / "carve" / "runtime.toml").write_text(runtime_toml)


def test_loader_parses_runtime_archive_section(tmp_path: Path) -> None:
    _write_project(tmp_path, '[archive]\njobs_window = "14d"\ninterval_s = 120\n')
    cfg = load_config(tmp_path)
    assert cfg.runtime.archive.jobs_window == "14d"
    assert cfg.runtime.archive.interval_s == 120
    # Unspecified windows keep their defaults.
    assert cfg.runtime.archive.runs_window == "30d"


def test_loader_defaults_when_runtime_toml_absent(tmp_path: Path) -> None:
    _write_project(tmp_path)  # no runtime.toml
    cfg = load_config(tmp_path)
    assert cfg.runtime.archive.interval_s == 3600
    assert cfg.runtime.archive.jobs_window == "7d"


def test_loader_rejects_invalid_window(tmp_path: Path) -> None:
    _write_project(tmp_path, '[archive]\njobs_window = "soon"\n')
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_loader_rejects_unknown_archive_key(tmp_path: Path) -> None:
    _write_project(tmp_path, '[archive]\nweeks_window = "2w"\n')
    with pytest.raises(ConfigError):
        load_config(tmp_path)

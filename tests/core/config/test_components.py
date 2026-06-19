"""Tests for the `[components.<name>]` config surface.

Covers schema-level parsing (no filesystem) and end-to-end loading from a
`carve.toml` (the loader wiring). Verifies all block shapes, the
cross-field validation rules, and that errors route to `carve.toml`.

*(layout spec Tests: unit bullet 1)*
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from carve.core.config import ConfigError, load_config
from carve.core.config.schema import (
    ComponentConfig,
    ComponentMode,
    ComponentType,
    Config,
)

# ---------------------------------------------------------------------------
# Schema-only: shapes parse
# ---------------------------------------------------------------------------


class TestComponentConfigShapes:
    def test_same_repo_dlt(self) -> None:
        c = ComponentConfig(type="dlt", mode="same-repo")
        assert c.type is ComponentType.DLT
        assert c.mode is ComponentMode.SAME_REPO
        assert c.url is None and c.path is None and c.ref is None

    def test_same_repo_dbt(self) -> None:
        c = ComponentConfig(type="dbt", mode="same-repo")
        assert c.type is ComponentType.DBT
        assert c.mode is ComponentMode.SAME_REPO

    def test_separate_local_with_path(self) -> None:
        c = ComponentConfig(type="dlt", mode="separate-local", path="/abs/ingest")
        assert c.mode is ComponentMode.SEPARATE_LOCAL
        assert c.path == "/abs/ingest"

    def test_separate_remote_with_url_and_branch(self) -> None:
        c = ComponentConfig(
            type="dbt",
            mode="separate-remote",
            url="git@github.com:org/repo.git",
            branch="main",
        )
        assert c.mode is ComponentMode.SEPARATE_REMOTE
        assert c.url == "git@github.com:org/repo.git"
        assert c.branch == "main"
        assert c.ref is None

    def test_separate_remote_with_url_and_ref(self) -> None:
        c = ComponentConfig(
            type="dbt",
            mode="separate-remote",
            url="git@github.com:org/repo.git",
            ref="9f3a1c7",
        )
        assert c.ref == "9f3a1c7"

    def test_sync_mode_defaults_hard_and_before_run_true(self) -> None:
        c = ComponentConfig(type="dlt", mode="same-repo")
        assert c.sync_mode == "hard"
        assert c.sync_before_run is True

    def test_soft_sync_mode_accepted(self) -> None:
        c = ComponentConfig(
            type="dbt",
            mode="separate-remote",
            url="https://github.com/org/repo.git",
            sync_mode="soft",
        )
        assert c.sync_mode == "soft"


# ---------------------------------------------------------------------------
# Schema-only: invalid shapes raise
# ---------------------------------------------------------------------------


class TestComponentConfigValidation:
    def test_separate_local_without_path_raises(self) -> None:
        with pytest.raises(ValidationError, match=r"path.*required"):
            ComponentConfig(type="dlt", mode="separate-local")

    def test_separate_remote_without_url_raises(self) -> None:
        with pytest.raises(ValidationError, match=r"url.*required"):
            ComponentConfig(type="dbt", mode="separate-remote")

    def test_path_on_same_repo_raises(self) -> None:
        with pytest.raises(ValidationError, match=r"path.*only valid"):
            ComponentConfig(type="dlt", mode="same-repo", path="/abs")

    def test_url_on_separate_local_raises(self) -> None:
        with pytest.raises(ValidationError, match="only valid"):
            ComponentConfig(
                type="dlt",
                mode="separate-local",
                path="/abs",
                url="https://github.com/org/repo.git",
            )

    def test_unknown_key_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ComponentConfig.model_validate(
                {"type": "dlt", "mode": "same-repo", "bogus": 1}
            )

    def test_unknown_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ComponentConfig(type="spark", mode="same-repo")  # type: ignore[arg-type]

    def test_unknown_mode_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ComponentConfig(type="dlt", mode="vendored")  # type: ignore[arg-type]

    def test_bad_sync_mode_rejected(self) -> None:
        with pytest.raises(ValidationError, match="sync_mode"):
            ComponentConfig(
                type="dbt",
                mode="separate-remote",
                url="https://github.com/org/repo.git",
                sync_mode="rebase",
            )


class TestComponentUrlValidation:
    """The `url` transport allow-list (defense-in-depth for `git clone`).

    *(layout slice security review: git argument-injection hardening)*
    """

    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/org/repo.git",
            "ssh://git@github.com/org/repo.git",
            "git://github.com/org/repo.git",
            "file:///srv/git/repo.git",
            "git@github.com:org/repo.git",  # scp-style
        ],
    )
    def test_allowed_transports_accepted(self, url: str) -> None:
        c = ComponentConfig(type="dbt", mode="separate-remote", url=url)
        assert c.url == url

    @pytest.mark.parametrize(
        "url",
        [
            "ext::sh -c 'touch /tmp/x'",  # ext-transport RCE
            "--upload-pack=touch /tmp/x",  # option-shaped
            "-oProxyCommand=evil",  # option-shaped
            "http://github.com/org/repo.git",  # http not in the allow-list
            "ftp://example.com/repo.git",  # disallowed scheme
            "x",  # not a URL at all
            "",  # empty
        ],
    )
    def test_disallowed_transports_rejected(self, url: str) -> None:
        with pytest.raises(ValidationError):
            ComponentConfig(type="dbt", mode="separate-remote", url=url)

    @pytest.mark.parametrize("field", ["ref", "branch"])
    @pytest.mark.parametrize("value", ["--orphan=x", "-x", "--upload-pack=evil"])
    def test_option_shaped_ref_or_branch_rejected(
        self, field: str, value: str
    ) -> None:
        with pytest.raises(ValidationError):
            ComponentConfig(
                type="dbt",
                mode="separate-remote",
                url="https://github.com/org/repo.git",
                **{field: value},
            )


# ---------------------------------------------------------------------------
# Config-level: components default empty (simple mode) and parse when present
# ---------------------------------------------------------------------------


class TestConfigComponents:
    def test_components_default_empty_is_simple_mode(self) -> None:
        cfg = Config.model_validate(
            {"project": {"name": "x"}, "models": {"anthropic_api_key": "k"}}
        )
        assert cfg.components == {}

    def test_components_keyed_by_name(self) -> None:
        cfg = Config.model_validate(
            {
                "project": {"name": "x"},
                "models": {"anthropic_api_key": "k"},
                "components": {
                    "analytics": {
                        "type": "dbt",
                        "mode": "separate-remote",
                        "url": "git@github.com:org/analytics.git",
                        "ref": "9f3a1c7",
                    },
                    "stripe_charges": {
                        "type": "dlt",
                        "mode": "separate-local",
                        "path": "/path/to/ingest-stripe",
                    },
                },
            }
        )
        assert set(cfg.components) == {"analytics", "stripe_charges"}
        assert cfg.components["analytics"].ref == "9f3a1c7"
        assert cfg.components["stripe_charges"].path == "/path/to/ingest-stripe"


# ---------------------------------------------------------------------------
# Loader: end-to-end from carve.toml
# ---------------------------------------------------------------------------


def _write_project(
    root: Path, *, carve_toml_extra: str = "", anthropic_key: str = "sk-test"
) -> None:
    (root / "carve.toml").write_text(
        '[project]\nname = "tmp"\n\n[paths]\nconfig_dir = "carve"\n' + carve_toml_extra
    )
    (root / "carve").mkdir(exist_ok=True)
    (root / "carve" / "models.toml").write_text(
        f'anthropic_api_key = "{anthropic_key}"\n'
    )


def test_omitted_components_block_is_simple_mode(tmp_path: Path) -> None:
    _write_project(tmp_path)
    cfg = load_config(tmp_path)
    assert cfg.components == {}


def test_components_block_loads_from_carve_toml(tmp_path: Path) -> None:
    extra = (
        "\n[components.analytics]\n"
        'type = "dbt"\n'
        'mode = "separate-remote"\n'
        'url = "git@github.com:org/analytics.git"\n'
        'branch = "main"\n'
        "\n[components.stripe_charges]\n"
        'type = "dlt"\n'
        'mode = "separate-local"\n'
        'path = "/path/to/ingest-stripe"\n'
    )
    _write_project(tmp_path, carve_toml_extra=extra)
    cfg = load_config(tmp_path)
    assert set(cfg.components) == {"analytics", "stripe_charges"}
    assert cfg.components["analytics"].branch == "main"
    assert cfg.components["analytics"].type is ComponentType.DBT
    assert cfg.components["stripe_charges"].mode is ComponentMode.SEPARATE_LOCAL


def test_env_var_interpolation_inside_component_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANALYTICS_URL", "git@github.com:org/analytics.git")
    extra = (
        "\n[components.analytics]\n"
        'type = "dbt"\n'
        'mode = "separate-remote"\n'
        'url = "${ANALYTICS_URL}"\n'
        'branch = "main"\n'
    )
    _write_project(tmp_path, carve_toml_extra=extra)
    cfg = load_config(tmp_path)
    assert cfg.components["analytics"].url == "git@github.com:org/analytics.git"


def test_invalid_component_block_raises_configerror_pointing_at_carve_toml(
    tmp_path: Path,
) -> None:
    # separate-local without path -> structured ConfigError naming carve.toml.
    extra = (
        "\n[components.broken]\n"
        'type = "dlt"\n'
        'mode = "separate-local"\n'  # missing path
    )
    _write_project(tmp_path, carve_toml_extra=extra)
    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path)
    err = excinfo.value
    assert err.field is not None and err.field.startswith("components.broken")
    assert err.file is not None and err.file.name == "carve.toml"


def test_unknown_key_in_component_block_raises(tmp_path: Path) -> None:
    extra = (
        "\n[components.broken]\n"
        'type = "dlt"\n'
        'mode = "same-repo"\n'
        'bogus = "nope"\n'
    )
    _write_project(tmp_path, carve_toml_extra=extra)
    with pytest.raises(ConfigError):
        load_config(tmp_path)

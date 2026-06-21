"""`ModelsConfig` — auth_mode validation, model-id default, tier resolution.

Part of model-auth (DELIVERY Increment 1b).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from carve.core.agents.pricing import lookup_pricing
from carve.core.config.schema import ModelsConfig


def test_default_model_is_current_and_priced() -> None:
    m = ModelsConfig()
    assert m.default_model == "claude-opus-4-8"
    # The install default must be priced, or cost reporting silently zeroes.
    assert lookup_pricing(m.default_model) is not None


@pytest.mark.parametrize(
    "model_id",
    ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5", "claude-fable-5"],
)
def test_current_models_are_priced(model_id: str) -> None:
    assert lookup_pricing(model_id) is not None


def test_auth_mode_accepts_known_values() -> None:
    assert ModelsConfig(auth_mode="api_key").auth_mode == "api_key"
    assert ModelsConfig(auth_mode="oauth").auth_mode == "oauth"
    assert ModelsConfig().auth_mode is None  # unset -> auto-resolve


def test_auth_mode_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        ModelsConfig(auth_mode="claude_code_oauth")


def test_resolve_model_none_falls_back_to_default() -> None:
    assert ModelsConfig().resolve_model(None) == "claude-opus-4-8"


def test_resolve_model_tier_label() -> None:
    m = ModelsConfig(tiers={"fast": "claude-haiku-4-5"})
    assert m.resolve_model("fast") == "claude-haiku-4-5"


def test_resolve_model_literal_id_passes_through() -> None:
    assert ModelsConfig().resolve_model("claude-sonnet-4-6") == "claude-sonnet-4-6"

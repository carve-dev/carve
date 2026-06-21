"""Per-model pricing table for cost estimation.

Prices are USD per 1M tokens. Update when Anthropic changes prices.
The lookup is intentionally tolerant: a model id like
`claude-sonnet-4-5-20250929` falls back to the dated-stripped key
`claude-sonnet-4-5` if no exact match exists.

If the model is unknown, `cost_usd` returns 0.0 rather than raising —
unknown models still complete; the run row simply records no cost.
The caller is expected to log a warning in that case.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPricing:
    """Per-1M-token prices for a single model."""

    input_per_mtok: float
    output_per_mtok: float
    cache_creation_per_mtok: float
    cache_read_per_mtok: float


# Source: Anthropic public pricing page. Stored in USD per 1M tokens.
# Cache-write is ~1.25x input (5-minute TTL); cache-read is ~0.1x input.
PRICING: dict[str, ModelPricing] = {
    # Current generation (the install-default lives here — see
    # `ModelsConfig.default_model`).
    "claude-opus-4-8": ModelPricing(
        input_per_mtok=5.0,
        output_per_mtok=25.0,
        cache_creation_per_mtok=6.25,
        cache_read_per_mtok=0.50,
    ),
    "claude-sonnet-4-6": ModelPricing(
        input_per_mtok=3.0,
        output_per_mtok=15.0,
        cache_creation_per_mtok=3.75,
        cache_read_per_mtok=0.30,
    ),
    "claude-haiku-4-5": ModelPricing(
        input_per_mtok=1.0,
        output_per_mtok=5.0,
        cache_creation_per_mtok=1.25,
        cache_read_per_mtok=0.10,
    ),
    "claude-fable-5": ModelPricing(
        input_per_mtok=10.0,
        output_per_mtok=50.0,
        cache_creation_per_mtok=12.5,
        cache_read_per_mtok=1.0,
    ),
    # Prior generation, still selectable.
    "claude-sonnet-4-5": ModelPricing(
        input_per_mtok=3.0,
        output_per_mtok=15.0,
        cache_creation_per_mtok=3.75,
        cache_read_per_mtok=0.30,
    ),
    "claude-opus-4-5": ModelPricing(
        input_per_mtok=15.0,
        output_per_mtok=75.0,
        cache_creation_per_mtok=18.75,
        cache_read_per_mtok=1.50,
    ),
}


def lookup_pricing(model: str) -> ModelPricing | None:
    """Resolve a model id to its `ModelPricing`, or `None` if unknown.

    Tries the literal id first, then strips a trailing `-YYYYMMDD` date
    suffix (Anthropic's snapshot convention) and tries again.
    """
    if model in PRICING:
        return PRICING[model]
    parts = model.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 8:
        return PRICING.get(parts[0])
    return None


def compute_cost_usd(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """Return the cost in USD for a token bundle on `model`.

    Returns 0.0 for unknown models (callers should log a warning).
    """
    pricing = lookup_pricing(model)
    if pricing is None:
        return 0.0
    return (
        input_tokens * pricing.input_per_mtok
        + output_tokens * pricing.output_per_mtok
        + cache_creation_tokens * pricing.cache_creation_per_mtok
        + cache_read_tokens * pricing.cache_read_per_mtok
    ) / 1_000_000

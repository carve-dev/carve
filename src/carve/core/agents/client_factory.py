"""Single resolver for the Anthropic client + credential precedence.

Every place that talks to the model provider builds its client here, so
credential precedence lives in exactly one spot:

    explicit ``auth_mode`` in ``models.toml``
      -> ``ANTHROPIC_API_KEY``
      -> a Claude-subscription OAuth bearer
      -> a clear, actionable error

Two credential paths (see the model-auth capability spec):

* **API key** — ``anthropic.Anthropic(api_key=...)`` from
  ``ANTHROPIC_API_KEY`` (carried into ``ModelsConfig.anthropic_api_key``
  via the ``${ANTHROPIC_API_KEY}`` indirection in ``models.toml``).
* **Subscription OAuth bearer** — ``anthropic.Anthropic(auth_token=...)``
  plus the ``anthropic-beta: oauth-2025-04-20`` header the Messages API
  requires for an OAuth bearer, from ``ANTHROPIC_AUTH_TOKEN`` or
  ``CLAUDE_CODE_OAUTH_TOKEN``. Carve owns no browser flow or token store —
  the token is minted by ``claude setup-token`` / ``ant auth login``.

The resolver emits **exactly one** credential. The SDK sends *both* the
``x-api-key`` and ``Authorization`` headers when an API key and an auth
token are both resolvable from the environment, and the API rejects that —
so when both are present we refuse with a clear error rather than letting
the SDK produce a cryptic 400.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from carve.core.config import Config, ConfigError

# The beta header the Messages API requires when authenticating with an
# OAuth bearer (vs. an API key).
OAUTH_BETA_HEADER = "oauth-2025-04-20"

# OAuth-token env vars, in lookup order. ``ANTHROPIC_AUTH_TOKEN`` is what the
# SDK itself reads; ``CLAUDE_CODE_OAUTH_TOKEN`` is what ``claude setup-token``
# emits — accept both.
_OAUTH_ENV_VARS = ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")


def _is_hosted() -> bool:
    """True in the hosted product, where subscription OAuth is not offered."""
    return os.environ.get("CARVE_HOSTED", "").strip().lower() in ("1", "true", "yes")


def _oauth_token() -> tuple[str, str] | None:
    """Return ``(token, source_env_var)`` for the first OAuth env var set."""
    for name in _OAUTH_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value, name
    return None


@dataclass(frozen=True)
class _Decision:
    """The resolved credential decision (no secret values).

    ``suppress_other`` is set when an explicit ``auth_mode`` wins but the
    *other* credential is also present in the environment: the client is
    built with the SDK's header-omit sentinel so the SDK cannot add the
    second credential (which would make the API 400).
    """

    mode: str  # "api_key" | "oauth" | "unconfigured"
    source: str  # human label for where the credential comes from
    hosted: bool
    suppress_other: bool
    error: ConfigError | None


@dataclass(frozen=True)
class AuthStatus:
    """What ``carve auth status`` reports — never any secret value."""

    mode: str  # "api_key" | "oauth" | "unconfigured"
    source: str
    credential_present: bool
    hosted: bool
    default_model: str
    note: str | None


def _resolve(config: Config) -> _Decision:
    """Decide which credential path to use, without touching secrets.

    An explicit ``auth_mode`` *wins* — including over a stray opposite
    credential in the environment, which is then suppressed at the wire (see
    ``make_client``). Only the auto path (``auth_mode`` unset) refuses when
    both are present, because there it has no signal to disambiguate.
    """
    models = config.models
    requested = models.auth_mode
    hosted = _is_hosted()

    api_key_present = bool(models.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY"))
    oauth = _oauth_token()
    oauth_present = oauth is not None

    if requested == "api_key":
        if not api_key_present:
            return _Decision(
                "api_key",
                "ANTHROPIC_API_KEY",
                hosted,
                False,
                ConfigError(
                    'Anthropic API key required (auth_mode = "api_key") but unset.',
                    file="carve/models.toml",
                    field="models.anthropic_api_key",
                    hint=(
                        "Set ANTHROPIC_API_KEY in your environment (or .env), or "
                        'switch auth_mode to "oauth".'
                    ),
                ),
            )
        # Explicit mode wins; suppress any stray OAuth token at the wire.
        return _Decision("api_key", "ANTHROPIC_API_KEY", hosted, oauth_present, None)

    if requested == "oauth":
        if hosted:
            return _Decision(
                "oauth",
                "subscription OAuth",
                hosted,
                False,
                ConfigError(
                    "Claude-subscription OAuth is not available in the hosted product.",
                    hint="Use ANTHROPIC_API_KEY (or the platform credential).",
                ),
            )
        if not oauth_present:
            return _Decision(
                "oauth",
                "ANTHROPIC_AUTH_TOKEN",
                hosted,
                False,
                ConfigError(
                    'OAuth token required (auth_mode = "oauth") but unset.',
                    hint=(
                        "Run `carve auth login` (wraps `claude setup-token`) or set "
                        "ANTHROPIC_AUTH_TOKEN."
                    ),
                ),
            )
        assert oauth is not None  # narrowed by oauth_present
        # Explicit mode wins; suppress any stray API key at the wire.
        return _Decision("oauth", oauth[1], hosted, api_key_present, None)

    # auto-resolve (auth_mode unset): no explicit signal to disambiguate, so a
    # both-present environment is refused rather than guessed.
    if api_key_present and oauth_present:
        return _Decision(
            "unconfigured",
            "conflict",
            hosted,
            False,
            ConfigError(
                "Both an API key and a Claude-subscription OAuth token are set; "
                "Anthropic rejects requests that carry both.",
                hint=(
                    "Pin `auth_mode` in carve/models.toml to choose one, or unset the "
                    "other (e.g. `unset ANTHROPIC_API_KEY` to use OAuth)."
                ),
            ),
        )
    if api_key_present:
        return _Decision("api_key", "ANTHROPIC_API_KEY", hosted, False, None)
    if oauth_present and not hosted:
        assert oauth is not None  # narrowed by oauth_present
        return _Decision("oauth", oauth[1], hosted, False, None)
    if oauth_present and hosted:
        return _Decision(
            "unconfigured",
            "subscription OAuth",
            hosted,
            False,
            ConfigError(
                "Claude-subscription OAuth is not available in the hosted product, "
                "and no ANTHROPIC_API_KEY is set.",
                hint="Set ANTHROPIC_API_KEY (or the platform credential).",
            ),
        )
    return _Decision(
        "unconfigured",
        "none",
        hosted,
        False,
        ConfigError(
            "No Anthropic credential found.",
            file="carve/models.toml",
            hint=(
                "Set ANTHROPIC_API_KEY, or run `carve auth login` to use a Claude "
                "subscription."
            ),
        ),
    )


def make_client(config: Config, client: Any | None = None) -> Any:
    """Return the Anthropic client for ``config``'s credential settings.

    ``client`` is the dependency-injection seam: when a caller (e.g. a test)
    passes a client, it is returned untouched and no credential is resolved.
    Otherwise the precedence above is applied, raising :class:`ConfigError`
    with an actionable hint when no single credential can be resolved.
    """
    if client is not None:
        return client

    import anthropic

    decision = _resolve(config)
    if decision.error is not None:
        raise decision.error

    if decision.mode == "api_key":
        key = config.models.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")
        kwargs: dict[str, Any] = {"api_key": key}
        if decision.suppress_other:
            # Explicit api_key mode + a stray OAuth token in the env: omit the
            # bearer the SDK would otherwise env-infer, so exactly one
            # credential reaches the wire.
            kwargs["default_headers"] = {"Authorization": anthropic.Omit()}
        return anthropic.Anthropic(**kwargs)

    token = _oauth_token()
    if token is None:  # defensive: _resolve guaranteed presence
        raise ConfigError(
            "OAuth token required but unset.",
            hint="Run `carve auth login` or set ANTHROPIC_AUTH_TOKEN.",
        )
    headers: dict[str, Any] = {"anthropic-beta": OAUTH_BETA_HEADER}
    if decision.suppress_other:
        # Explicit oauth mode + a stray API key in the env: omit x-api-key.
        headers["X-Api-Key"] = anthropic.Omit()
    return anthropic.Anthropic(auth_token=token[0], default_headers=headers)


def auth_status(config: Config) -> AuthStatus:
    """Resolve the active auth mode for ``carve auth status`` (no secrets)."""
    decision = _resolve(config)
    note: str | None = None
    if decision.error is not None:
        note = decision.error.message
    elif decision.suppress_other:
        other = "OAuth token" if decision.mode == "api_key" else "API key"
        note = f"ignoring a conflicting {other} in the environment"
    return AuthStatus(
        mode=decision.mode,
        source=decision.source,
        credential_present=decision.error is None and decision.mode != "unconfigured",
        hosted=decision.hosted,
        default_model=config.models.default_model,
        note=note,
    )

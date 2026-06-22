"""Credential precedence + client construction (model-auth, Increment 1b).

Exercises the single resolver `carve.core.agents.client_factory.make_client`
without any network: `anthropic.Anthropic` is monkeypatched to a recorder so
the tests assert *which* kwargs (api_key vs. auth_token + oauth beta header)
the resolver would pass.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

from carve.core.agents import client_factory
from carve.core.config import ConfigError
from carve.core.config.schema import ModelsConfig

_CRED_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CARVE_HOSTED",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dev/CI environment may carry a real key — start from none."""
    for var in _CRED_ENV:
        monkeypatch.delenv(var, raising=False)


class _Recorder:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] | None = None

    def __call__(self, **kwargs: Any) -> str:
        self.kwargs = kwargs
        return "stub-client"


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    import anthropic

    rec = _Recorder()
    monkeypatch.setattr(anthropic, "Anthropic", rec)
    return rec


def _cfg(**models_kwargs: Any) -> Any:
    return types.SimpleNamespace(models=ModelsConfig(**models_kwargs))


# --- dependency-injection passthrough -------------------------------------


def test_injected_client_is_returned_untouched(recorder: _Recorder) -> None:
    sentinel = object()
    assert client_factory.make_client(_cfg(anthropic_api_key="sk"), sentinel) is sentinel
    assert recorder.kwargs is None  # no credential resolved


# --- API key path ----------------------------------------------------------


def test_api_key_mode_explicit(recorder: _Recorder) -> None:
    client_factory.make_client(_cfg(auth_mode="api_key", anthropic_api_key="sk-x"))
    assert recorder.kwargs == {"api_key": "sk-x"}


def test_api_key_auto_from_env(monkeypatch: pytest.MonkeyPatch, recorder: _Recorder) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    client_factory.make_client(_cfg())  # auth_mode unset -> auto
    assert recorder.kwargs == {"api_key": "sk-env"}


def test_api_key_required_but_missing(recorder: _Recorder) -> None:
    with pytest.raises(ConfigError):
        client_factory.make_client(_cfg(auth_mode="api_key"))
    assert recorder.kwargs is None


# --- OAuth bearer path -----------------------------------------------------


def test_oauth_uses_bearer_and_beta_header(
    monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
) -> None:
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "oauth-tok")
    client_factory.make_client(_cfg(auth_mode="oauth"))
    assert recorder.kwargs == {
        "auth_token": "oauth-tok",
        "default_headers": {"anthropic-beta": "oauth-2025-04-20"},
    }
    assert recorder.kwargs is not None and "api_key" not in recorder.kwargs


def test_oauth_auto_from_claude_code_token(
    monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "cc-tok")
    client_factory.make_client(_cfg())  # auto; no api key
    assert recorder.kwargs is not None
    assert recorder.kwargs["auth_token"] == "cc-tok"
    assert recorder.kwargs["default_headers"] == {"anthropic-beta": "oauth-2025-04-20"}


def test_oauth_required_but_missing(recorder: _Recorder) -> None:
    with pytest.raises(ConfigError):
        client_factory.make_client(_cfg(auth_mode="oauth"))


# --- precedence + conflicts ------------------------------------------------


def test_auto_prefers_api_key(recorder: _Recorder) -> None:
    client_factory.make_client(_cfg(anthropic_api_key="sk-cfg"))
    assert recorder.kwargs == {"api_key": "sk-cfg"}


def test_auto_mode_refuses_when_both_credentials_present(
    monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
) -> None:
    # Auto mode has no signal to disambiguate -> refuse (the SDK would send
    # both headers and the API 400s).
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "oauth-tok")
    with pytest.raises(ConfigError) as exc:
        client_factory.make_client(_cfg())
    assert "both" in str(exc.value).lower()
    assert recorder.kwargs is None


def test_explicit_api_key_wins_and_suppresses_stray_oauth(
    monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
) -> None:
    # Claude Code can export CLAUDE_CODE_OAUTH_TOKEN globally; a pinned
    # api_key mode must still win, suppressing the stray bearer at the wire.
    import anthropic

    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "stray-bearer")
    client_factory.make_client(_cfg(auth_mode="api_key", anthropic_api_key="sk-x"))
    assert recorder.kwargs is not None
    assert recorder.kwargs["api_key"] == "sk-x"
    assert isinstance(recorder.kwargs["default_headers"]["Authorization"], anthropic.Omit)


def test_explicit_oauth_wins_and_suppresses_stray_api_key(
    monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
) -> None:
    import anthropic

    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "oauth-tok")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-stray")
    client_factory.make_client(_cfg(auth_mode="oauth"))
    assert recorder.kwargs is not None
    assert recorder.kwargs["auth_token"] == "oauth-tok"
    headers = recorder.kwargs["default_headers"]
    assert headers["anthropic-beta"] == "oauth-2025-04-20"
    assert isinstance(headers["X-Api-Key"], anthropic.Omit)


def test_explicit_api_key_emits_single_auth_header_on_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wire-level guard: the Omit sentinel must actually drop the header.

    Uses the real SDK (no recorder) and inspects the built request headers,
    so an SDK change to Omit semantics is caught here rather than at runtime.
    """
    import anthropic
    from anthropic._base_client import FinalRequestOptions

    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "stray-bearer")
    client = client_factory.make_client(_cfg(auth_mode="api_key", anthropic_api_key="sk-x"))
    assert isinstance(client, anthropic.Anthropic)
    built = dict(client._build_headers(FinalRequestOptions(method="post", url="/v1/messages")))
    lower = {k.lower(): v for k, v in built.items()}
    assert "x-api-key" in lower
    assert "authorization" not in lower


def test_explicit_oauth_emits_single_auth_header_on_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Symmetric wire-level guard for the oauth path (suppress stray x-api-key)."""
    import anthropic
    from anthropic._base_client import FinalRequestOptions

    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "oauth-tok")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-stray")
    client = client_factory.make_client(_cfg(auth_mode="oauth"))
    assert isinstance(client, anthropic.Anthropic)
    built = dict(client._build_headers(FinalRequestOptions(method="post", url="/v1/messages")))
    lower = {k.lower(): v for k, v in built.items()}
    assert "authorization" in lower
    assert "x-api-key" not in lower
    assert lower.get("anthropic-beta") == "oauth-2025-04-20"


def test_no_credential_errors_actionably(recorder: _Recorder) -> None:
    with pytest.raises(ConfigError) as exc:
        client_factory.make_client(_cfg())
    msg = str(exc.value)
    assert "carve auth login" in msg or "ANTHROPIC_API_KEY" in msg


# --- hosted split ----------------------------------------------------------


def test_hosted_refuses_explicit_oauth(
    monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
) -> None:
    monkeypatch.setenv("CARVE_HOSTED", "1")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "oauth-tok")
    with pytest.raises(ConfigError) as exc:
        client_factory.make_client(_cfg(auth_mode="oauth"))
    assert "hosted" in str(exc.value).lower()


def test_hosted_auto_with_only_oauth_errors(
    monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
) -> None:
    monkeypatch.setenv("CARVE_HOSTED", "true")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "oauth-tok")
    with pytest.raises(ConfigError):
        client_factory.make_client(_cfg())


def test_hosted_still_allows_api_key(
    monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
) -> None:
    monkeypatch.setenv("CARVE_HOSTED", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    client_factory.make_client(_cfg())
    assert recorder.kwargs == {"api_key": "sk-env"}


# --- auth_status (no secrets) ----------------------------------------------


def test_auth_status_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    st = client_factory.auth_status(_cfg())
    assert st.mode == "api_key"
    assert st.credential_present is True
    assert st.default_model == "claude-opus-4-8"
    assert st.note is None


def test_auth_status_unconfigured_carries_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    st = client_factory.auth_status(_cfg())
    assert st.mode == "unconfigured"
    assert st.credential_present is False
    assert st.note  # the actionable error message


def test_auth_status_never_exposes_the_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "super-secret-token")
    st = client_factory.auth_status(_cfg())
    assert "super-secret-token" not in repr(st)
    assert st.mode == "oauth"
    assert st.credential_present is True


def test_auth_status_explicit_mode_reports_present_despite_stray_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "stray-bearer")
    st = client_factory.auth_status(_cfg(auth_mode="api_key", anthropic_api_key="sk-x"))
    assert st.mode == "api_key"
    assert st.credential_present is True
    assert st.note is not None and "ignoring" in st.note.lower()
    assert "stray-bearer" not in repr(st)

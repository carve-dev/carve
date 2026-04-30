# M1.1-02 — Claude Code OAuth auth path

**Milestone:** 1.1 — M1 follow-ups
**Estimated effort:** 1–1.5 days (most of it is investigation + adapter work)
**Dependencies:** M1-02 (config), M1-04 (agent loop), M1.1-01 (init templates — to advertise the new mode)

## Purpose

Let users authenticate Carve's agent loop against their Claude Code / Claude Max subscription via OAuth, instead of requiring a separate API key from console.anthropic.com. The Claude Agent SDK already does this — Carve should plug into it as an alternative client.

Today the loop hard-codes the API-key path:

```python
# src/carve/cli/orchestrator/planner.py
client = anthropic.Anthropic(api_key=config.models.anthropic_api_key)
```

After this spec, `models.toml` selects between two auth modes:

```toml
[anthropic]
auth_mode = "api_key"            # default — same as today
anthropic_api_key = "${ANTHROPIC_API_KEY}"
default_model = "claude-sonnet-4-5"

# OR:
[anthropic]
auth_mode = "claude_code_oauth"
default_model = "claude-sonnet-4-5"
```

The `anthropic_api_key` field becomes optional and is only required when `auth_mode == "api_key"`.

## Scope

### In scope

- `auth_mode: Literal["api_key", "claude_code_oauth"]` field on `ModelsConfig`, defaulting to `"api_key"`.
- A `client_factory` module under `src/carve/core/agents/` that takes `ModelsConfig` and returns a client object the loop can use.
- An adapter (if needed) that gives the Claude Agent SDK client the same `messages.create(...)` shape `AgentLoop` already calls.
- `claude-agent-sdk` as an **optional** dependency: `pip install carve[oauth]`. Without it, attempting to use OAuth raises a clear ConfigError pointing at the install command.
- A model-validator on `ModelsConfig` that enforces the `api_key`-requires-`anthropic_api_key` invariant.
- Updated `carve init` template (M1.1-01 lands first) so `models.toml` shows both modes commented.
- Tests for both auth modes with the SDKs mocked, plus a gated integration test that hits the real OAuth path.

### Out of scope

- Switching the default to OAuth. Keep `api_key` default until OAuth has shipped for a few weeks and proven reliable.
- Multi-tenant / SaaS auth. M1/M2 is single-user.
- Workbench-issued OAuth tokens (a different OAuth flow). Only the Claude Code session path is in scope here.
- Token-cost reporting differences between the two SDKs. If usage shapes diverge, surface what's available; don't refactor the cost pipeline.

## Investigation phase (do first)

Before writing code, the engineer should answer these questions in a short note attached to the implementation PR:

1. **Login flow.** Does `claude-agent-sdk` pick up an existing Claude Code session automatically (e.g. by reading the same on-disk credentials), or does it need an explicit login step? If explicit, what command — and should `carve plan` invoke it implicitly on first use, or surface an error telling the user to run it?
2. **Message API shape.** What is the SDK's equivalent of `client.messages.create(model=..., system=..., max_tokens=..., tools=..., messages=...)`? Does it accept the same tool-use protocol the M1 `AgentLoop` already implements, or do we need an adapter?
3. **Token usage.** Does the SDK expose `response.usage.input_tokens` / `output_tokens` / cache counters in the same shape? If different, what do we record on `Plan.estimates_json`?
4. **Failure modes.** What happens if the user has only Claude Pro (no API access) — does the SDK raise something specific, or is it discovered at request time? Map whatever it raises to a clean ConfigError or AgentError.
5. **Model selection.** Does the OAuth path support arbitrary model strings, or is it restricted to whatever's in the user's subscription? If restricted, document that constraint near `default_model`.

The investigation answers shape the adapter design. **Don't skip them** — the implementation can degrade quickly if the two SDKs diverge in ways we didn't anticipate.

## Implementation

### Schema change

```python
# src/carve/core/config/schema.py

class ModelsConfig(BaseModel):
    auth_mode: Literal["api_key", "claude_code_oauth"] = "api_key"
    anthropic_api_key: str | None = None      # required when auth_mode == "api_key"
    default_model: str = "claude-sonnet-4-5-20250929"

    @model_validator(mode="after")
    def _check_auth_consistency(self) -> "ModelsConfig":
        if self.auth_mode == "api_key" and not self.anthropic_api_key:
            raise ValueError(
                "auth_mode='api_key' requires anthropic_api_key. "
                "Set it in carve/models.toml or switch to auth_mode='claude_code_oauth'."
            )
        return self
```

The model-validator exists so `ConfigError` carries the field path and a helpful hint, just like other M1-02 validation failures.

### `client_factory`

```python
# src/carve/core/agents/client_factory.py

def build_client(models: ModelsConfig) -> "MessagesClient":
    """Return a client with a `.messages.create(...)` interface that AgentLoop can call."""
    if models.auth_mode == "api_key":
        import anthropic
        return anthropic.Anthropic(api_key=models.anthropic_api_key)
    elif models.auth_mode == "claude_code_oauth":
        try:
            from claude_agent_sdk import ClaudeAgentClient  # adjust to actual API
        except ImportError as exc:
            raise ConfigError(
                "auth_mode='claude_code_oauth' requires the optional 'oauth' extra. "
                "Install with: pip install 'carve[oauth]'.",
                file="carve/models.toml",
                field="models.auth_mode",
            ) from exc
        client = ClaudeAgentClient()              # or whatever the SDK exposes
        return _ClaudeCodeAdapter(client)         # only if shapes diverge
    else:
        raise AssertionError(f"unhandled auth_mode: {models.auth_mode}")
```

If the SDKs' `messages.create` shapes are compatible (investigation answer #2), the adapter is unnecessary and we return the SDK client directly. If they diverge, `_ClaudeCodeAdapter` translates the call site `AgentLoop` uses into whatever the SDK expects, including unwrapping the response back into an object with `.content`, `.stop_reason`, `.usage.input_tokens`, etc.

### Planner wiring

```python
# src/carve/cli/orchestrator/planner.py
from carve.core.agents.client_factory import build_client

def generate_plan(...):
    ...
    client = build_client(config.models)
    loop = AgentLoop(client=client, tools=..., system_prompt=..., model=config.models.default_model)
    ...
```

No changes to `AgentLoop` itself. The whole point is that the loop only sees a client interface.

### Optional dependency

`pyproject.toml`:

```toml
[project.optional-dependencies]
oauth = ["claude-agent-sdk>=...whatever the latest is..."]
```

The `dev` extras already exist; `oauth` is new and additive. CI doesn't need to install `oauth` for the unit tests (they mock the SDK), but the gated integration test does.

### `carve init` template (depends on M1.1-01)

After M1.1-01 lands, update the `models.toml` template generated by `carve init` to document both modes:

```toml
# Anthropic / model configuration.

# Option A — API key (default, billed via console.anthropic.com):
# [anthropic]
# auth_mode = "api_key"
# anthropic_api_key = "${ANTHROPIC_API_KEY}"
# default_model = "claude-sonnet-4-5"

# Option B — Claude Code OAuth (uses your Claude Max plan credits):
# Requires the optional dep: pip install 'carve[oauth]'
# [anthropic]
# auth_mode = "claude_code_oauth"
# default_model = "claude-sonnet-4-5"
```

## Tests

### Unit tests (always run)

- `tests/core/agents/test_client_factory.py`:
  - `auth_mode="api_key"` with a key returns an `anthropic.Anthropic` instance.
  - `auth_mode="api_key"` without a key — schema validation raises ConfigError with the hint.
  - `auth_mode="claude_code_oauth"` with the SDK importable returns the OAuth client.
  - `auth_mode="claude_code_oauth"` without the SDK — raises ConfigError pointing at `pip install 'carve[oauth]'`.
- `tests/core/agents/test_loop.py` extension: a parametrized variant of an existing tool-use test that runs against a mocked OAuth client (proves the adapter wires correctly, if there is one).

### Integration test (gated)

- `tests/cli/orchestrator/test_planner_oauth_integration.py`, gated on `CARVE_OAUTH_INTEGRATION_TEST=1`. Runs a one-turn plan against the real OAuth path. Skipped unless the env var is set and a Claude Code session is logged in.

## Acceptance criteria

- A user with a Claude Max plan can run `carve plan` end-to-end without ever creating a Console API key.
- `auth_mode="api_key"` continues to work unchanged for users who haven't migrated.
- Helpful errors when the optional dep is missing or when the schema invariants are violated.
- All gates (`ruff`, `mypy --strict`, full `pytest`) stay green; new tests cover both branches.
- README setup section documents both modes side by side, including the cost-billing distinction (Console pay-as-you-go vs Claude Max plan credits).

## Files this spec produces

New:

- `src/carve/core/agents/client_factory.py`
- (maybe) `src/carve/core/agents/_claude_code_adapter.py` — only if the SDK shapes diverge
- `tests/core/agents/test_client_factory.py`
- `tests/cli/orchestrator/test_planner_oauth_integration.py`

Modified:

- `src/carve/core/config/schema.py` (new `auth_mode`, validator)
- `src/carve/cli/orchestrator/planner.py` (use `build_client`)
- `pyproject.toml` (new `oauth` optional extra)
- `README.md` (auth section)
- `CHANGELOG.md`
- `src/carve/cli/commands/init.py` (template update — depends on M1.1-01)

## What this enables

- Carve becomes the path of least resistance for engineers who already have Claude Code: no second account, no second credit card.
- The `client_factory` indirection is reusable when M3 adds non-Anthropic providers (Bedrock, Vertex). Add new branches to the factory; the loop stays untouched.
- The `auth_mode` field is the right place to bolt on future modes (Workbench OAuth, service accounts) without breaking existing configs.

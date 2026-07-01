"""CLI↔REST parity — every CLI command has a REST counterpart or a reviewed exemption.

The *mechanism* the rest-api spec requires (PRD §6 intro): enumerate the real
Typer command surface from ``carve.cli.main.app`` so a newly-added CLI command
automatically trips this test, and assert each command maps to a REST router tag
**or** appears in an explicit, commented exemption allow-list. Runs offline (the
app is built over a ``MagicMock`` state store — no Postgres needed).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from carve.api.main import create_app
from carve.cli.main import app as cli_app
from carve.core.config.schema import Config, ModelsConfig, ProjectConfig

# --- The reviewed mapping: CLI command → REST router tag ---------------------
#
# Keys are the exact invocation names ("group sub" for subcommands). Values are
# the router tag that provides the REST counterpart.
COMMAND_TO_TAG: dict[str, str] = {
    "plan": "plans",
    "build": "builds",
    "plan-and-build": "builds",
    "deploy": "deploys",
    "runs": "runs",
    "logs": "runs",  # logs are a run sub-resource: GET /runs/{id}/logs
    "component": "components",
    "auth rotate": "tokens",  # mints an API bearer token → /api/v1/tokens
    # target group → /api/v1/targets
    "target create": "targets",
    "target list": "targets",
    "target show": "targets",
    "target rename": "targets",
    "target delete": "targets",
    # el group → run history (/runs) + the deploy surface (/deploys)
    "el run": "runs",
    "el list": "deploys",
    "el deploy": "deploys",
    "el verify": "deploys",
    # pipelines group → /api/v1/pipelines
    "pipelines validate": "pipelines",
    "pipelines list": "pipelines",
    "pipelines show": "pipelines",
    "pipelines diff": "pipelines",
    # components group → /api/v1/components
    "components show": "components",
    # schedule group → /api/v1/schedules
    "schedule list": "schedules",
    "schedule show": "schedules",
    "schedule pause": "schedules",
    "schedule resume": "schedules",
    "schedule set-cron": "schedules",
    "schedule reseed": "schedules",
    # metrics group → /api/v1/metrics
    "metrics costs": "metrics",
    "metrics runs": "metrics",
    "metrics agents": "metrics",
    # memory group → /api/v1/memory
    "memory show": "memory",
    "memory edit": "memory",
    "memory append-decision": "memory",
    "memory refresh": "memory",
    # agents group → /api/v1/agents
    "agents list": "agents",
    "agents show": "agents",
    "agents create": "agents",
    # skills group → /api/v1/skills
    "skills list": "skills",
    "skills show": "skills",
    "skills test": "skills",
    # mcp-servers group → /api/v1/mcp-servers
    "mcp-servers add": "mcp-servers",
    "mcp-servers list": "mcp-servers",
    "mcp-servers remove": "mcp-servers",
}

# --- The reviewed exemption allow-list (plumbing-only / interactive) ----------
#
# Each entry is a command with NO control-plane REST counterpart, with a
# one-line justification. Silent gaps are NOT allowed — an unmapped, unexempted
# command fails the test.
EXEMPTIONS: dict[str, str] = {
    "init": "local project scaffolding; runs before the server/DB exist",
    "serve": "the process that launches this API server itself",
    "mcp-serve": "MCP server process; an auto-generated adapter *over* this REST API",
    "worker": "process launcher; live worker state is exposed via /api/v1/workers",
    "version": "build metadata; exposed via the OpenAPI info.version field",
    "connect": "local execution-engine install + connection validation (interactive setup)",
    "auth status": "local model-provider credential inspection (env/keychain, not server state)",
    "auth login": "interactive browser OAuth via `claude setup-token`",
}

# --- Write-parity: a *mutating* CLI command needs a *mutating* REST operation --
#
# The tag-level mapping above let a read-only lifecycle router (plans/builds/runs
# = GET only) pass parity while the REST surface couldn't drive plan→build→run.
# These sets close that hole: a mutating command must have ≥1 POST/PUT/PATCH/
# DELETE on its mapped tag, unless it is an explicit, reviewed write-exemption.
MUTATING_COMMANDS: set[str] = {
    "plan",
    "build",
    "plan-and-build",
    "deploy",
    "el run",
    "el deploy",
    "el verify",
    "memory append-decision",
    "memory edit",
    "schedule pause",
    "schedule resume",
    "schedule set-cron",
    "auth rotate",
    "target create",
    "target rename",
    "target delete",
    "agents create",
    "mcp-servers add",
    "mcp-servers remove",
}

# Every key here is a provably-real CLI command (asserted by
# test_write_parity_exempt_entries_are_grounded) — so the exempt set can't drift
# into phantom entries or silently hide a real mutating command.
WRITE_PARITY_EXEMPT: dict[str, str] = {
    # Deploy write-surface deferred to Increment 6 (deploys table + non-interactive
    # PR handoff); `carve deploy` is a six-phase interactive flow, not a simple POST.
    "deploy": "deploy write-surface deferred to Increment 6 (deploys table + PR handoff)",
    "el deploy": "deploy write-surface deferred to Increment 6 (deploys table + PR handoff)",
    "el verify": "deploy write-surface deferred to Increment 6 (deploys table + PR handoff)",
    # These CLI writes map to read-only REST routers today; their REST write
    # surfaces are out of scope for the plan/build/run/memory gap-fill (tracked).
    "target create": "targets REST write-surface not yet exposed (read-only router); tracked",
    "target rename": "targets REST write-surface not yet exposed (read-only router); tracked",
    "target delete": "targets REST write-surface not yet exposed (read-only router); tracked",
    "agents create": "agents REST write-surface not yet exposed (read-only router); tracked",
    "mcp-servers add": "mcp-servers REST write-surface not exposed (read-only router); tracked",
    "mcp-servers remove": "mcp-servers REST write-surface not exposed (read-only router); tracked",
}

# Deferrals that are FLAGS, not distinct CLI commands — they don't independently
# trip command parity, so they don't belong in the command-keyed WRITE_PARITY_EXEMPT.
# Recorded here (keyed by the flag invocation) so the deferral is explicit, and
# asserted to NOT shadow a real command by test_flag_level_deferrals_are_not_commands.
FLAG_LEVEL_DEFERRALS: dict[str, str] = {
    "plan --refine": "refine chains backlogged by the lean-plan-build ADR (a flag, not a command)",
}

_MUTATING_METHODS = {"post", "put", "patch", "delete"}


def _enumerate_commands(typer_app: object, prefix: str = "") -> list[str]:
    """Walk a Typer app into ``["cmd", "group sub", ...]`` invocation names."""
    names: list[str] = []
    for cmd in typer_app.registered_commands:  # type: ignore[attr-defined]
        name = cmd.name or cmd.callback.__name__.replace("_", "-")
        names.append(f"{prefix}{name}".strip())
    for group in typer_app.registered_groups:  # type: ignore[attr-defined]
        names.extend(_enumerate_commands(group.typer_instance, prefix=f"{group.name} "))
    return names


def _tag_methods() -> dict[str, set[str]]:
    """Map each served tag → the set of HTTP methods across its operations."""
    config = Config(
        project=ProjectConfig(name="parity"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
    )
    schema = create_app(MagicMock(), config).openapi()
    tag_methods: dict[str, set[str]] = {}
    for operations in schema["paths"].values():
        for method, operation in operations.items():
            if not isinstance(operation, dict):
                continue
            for tag in operation.get("tags", []):
                tag_methods.setdefault(tag, set()).add(method.lower())
    return tag_methods


def _app_tags() -> set[str]:
    """Every tag actually served, read from the generated OpenAPI schema."""
    return set(_tag_methods())


def test_every_cli_command_is_mapped_or_exempt() -> None:
    """A new CLI command with no mapping and no exemption fails CI here."""
    commands = _enumerate_commands(cli_app)
    unaccounted = [
        c for c in commands if c not in COMMAND_TO_TAG and c not in EXEMPTIONS
    ]
    assert not unaccounted, (
        "CLI commands lacking a REST counterpart or an explicit exemption: "
        f"{sorted(unaccounted)}. Add a router mapping to COMMAND_TO_TAG or a "
        "justified entry to EXEMPTIONS."
    )


def test_mapped_tags_are_served_by_the_app() -> None:
    """Every router tag referenced by the mapping is actually mounted."""
    served = _app_tags()
    missing = {tag for tag in COMMAND_TO_TAG.values() if tag not in served}
    assert not missing, f"mapped router tags with no routes in the app: {sorted(missing)}"


def test_exemptions_do_not_overlap_mappings() -> None:
    """An exemption and a mapping for the same command would be a review error."""
    overlap = set(COMMAND_TO_TAG) & set(EXEMPTIONS)
    assert not overlap, f"commands both mapped and exempted: {sorted(overlap)}"


def test_mutating_commands_have_a_mutating_rest_operation() -> None:
    """A mutating CLI command must expose ≥1 write op on its tag (or be write-exempt).

    This is the anti-regression: after the write-surface gap-fill, a read-only
    lifecycle router for a mutating command fails CI here.
    """
    tag_methods = _tag_methods()
    gaps: list[str] = []
    for command in sorted(MUTATING_COMMANDS):
        if command in WRITE_PARITY_EXEMPT:
            continue
        tag = COMMAND_TO_TAG.get(command)
        assert tag is not None, f"mutating command {command!r} is not in COMMAND_TO_TAG"
        if not (tag_methods.get(tag, set()) & _MUTATING_METHODS):
            gaps.append(f"{command} → tag {tag!r} (methods {sorted(tag_methods.get(tag, set()))})")
    assert not gaps, (
        "mutating CLI commands whose REST tag exposes no write operation: "
        f"{gaps}. Add the POST/PUT/PATCH/DELETE, or a justified WRITE_PARITY_EXEMPT entry."
    )


def test_mutating_command_set_is_grounded() -> None:
    """Every MUTATING_COMMANDS entry is a real CLI command with a tag mapping."""
    real = set(_enumerate_commands(cli_app))
    assert MUTATING_COMMANDS <= real, (
        f"MUTATING_COMMANDS references non-existent CLI commands: "
        f"{sorted(MUTATING_COMMANDS - real)}"
    )
    unmapped = {c for c in MUTATING_COMMANDS if c not in COMMAND_TO_TAG}
    assert not unmapped, f"mutating commands missing a COMMAND_TO_TAG entry: {sorted(unmapped)}"


def test_write_parity_exempt_entries_are_grounded() -> None:
    """Every WRITE_PARITY_EXEMPT key is a real CLI command with a tag mapping."""
    real = set(_enumerate_commands(cli_app))
    unknown = {c for c in WRITE_PARITY_EXEMPT if c not in real}
    assert not unknown, (
        f"WRITE_PARITY_EXEMPT references non-existent CLI commands: {sorted(unknown)} "
        "(flag-level deferrals belong in FLAG_LEVEL_DEFERRALS, not here)."
    )
    unmapped = {c for c in WRITE_PARITY_EXEMPT if c not in COMMAND_TO_TAG}
    assert not unmapped, f"exempt commands missing a COMMAND_TO_TAG entry: {sorted(unmapped)}"


def test_flag_level_deferrals_are_not_commands() -> None:
    """A flag-level deferral must not shadow a real command (else it hides a gap)."""
    real = set(_enumerate_commands(cli_app))
    shadowing = {f for f in FLAG_LEVEL_DEFERRALS if f in real}
    assert not shadowing, (
        f"FLAG_LEVEL_DEFERRALS entries that are actually real commands: {sorted(shadowing)}"
    )


def test_plan_build_run_have_post_after_gap_fill() -> None:
    """Explicit guard: the gap-fill's three lifecycle writes exist; deploy is exempt."""
    tag_methods = _tag_methods()
    assert "post" in tag_methods.get("plans", set())
    assert "post" in tag_methods.get("builds", set())
    assert "post" in tag_methods.get("runs", set())
    assert "deploy" in WRITE_PARITY_EXEMPT

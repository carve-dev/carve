# Connect: AI-driven onboarding and on-demand provisioning

> The **first magical moment** — and the thing that makes setup *not* a wall of install-time questions. `connect` is the capability the [orchestrator](./harness.md) wields (not a standing subagent) to **set things up on first need**: connect a warehouse or a source, configure a component's [execution backend](./dbt-execution.md), and **provision + pin** the bundled engine the moment a user first reaches for it. It is the lazy, agent-driven complement to [`init`](./init.md): **`init` scaffolds + *detects* once; `connect` *provisions* on demand.** The user never front-loads choices they can't answer yet.

## Status

- **Status:** Drafting
- **Depends on:** [harness](./harness.md) (the orchestrator wields it; tools + permission gate), [init](./init.md) (the scaffold + detection it builds on), [layout](./layout.md) (writes/pins component config), [sql](./sql.md) (warehouse connection + introspection to validate a connect), [dbt-execution](./dbt-execution.md) (the bundled-engine provisioning + pin it performs).
- **Used by:** the orchestrator on first dbt/dlt/warehouse use; [dbt-execution](./dbt-execution.md) (lazy engine provisioning); [dlt-engineer](./dlt-engineer.md) (source onboarding).
- **Lineage:** net-new. Resolves the [`_strategy/2026-06-ai-harness.md`](../_strategy/2026-06-ai-harness.md) "connect/onboarding is a capability the orchestrator wields, not a standing agent" note — and the spec-structure audit's open "is `connect` a capability or folded into `init`?" question (answer: a capability, distinct from `init`).

## Goal

Make onboarding **on-demand and intelligent** instead of front-loaded and manual: when a user first does something that needs a connection, a backend, or an engine, the orchestrator **figures out what's needed, sets it up, validates it, and records it** — so the experience is "ask for the thing, get the thing," with the resulting config left **declarative and reproducible**.

## Out of scope

- **The one-time project scaffold** — creating the project, templating `carve.toml`/`carve/`, detecting what already exists is [`init`](./init.md). `connect` runs *after* init, on first use.
- **Authoring** — writing dlt/dbt code is the [dlt-engineer](./dlt-engineer.md) / [dbt-engineer](./dbt-engineer.md). `connect` wires *access + execution*, not code.

## Behavior

### Division of labor with `init`

| | [`init`](./init.md) | `connect` |
|---|---|---|
| When | once, at project creation | on first need, repeatedly |
| Mode | scaffold + **detect** | **provision** + connect + validate |
| dbt | records *which backend exists* (Cloud/native/external/none) | installs + **pins** the bundled engine when first used; wires Cloud/native creds |
| Warehouse/source | templates `connections.toml` | connects a real target/source, validates it via [`sql`](./sql.md), records it |
| Asks the user | only what it can't detect | only what it can't infer at the moment of need |

`init` deliberately does **not** install a dbt engine or force backend/version choices (a user often can't answer yet, and Cloud/native/external install nothing). `connect` handles all of that lazily.

### On-demand provisioning + pin (the core loop)

> **Updated during implementation (2026-06-26):** the **lean first slice** of this capability shipped — the provision→validate→pin loop is **real for the bundled `dbt-core` engine path**, in `src/carve/core/connect/` (`installer.py` install + path-containment, `dbt_provision.py` the loop, `result.py` the typed outcomes/errors) with `carve connect [component]` (`cli/commands/connect.py`) as the explicit command and the importable `provision_dbt_engine` as the thin orchestrator-trigger seam. It builds the *deferred install half* of [dbt-execution](./dbt-execution.md)'s `engine.py` (which resolves + pins but explicitly does not install): the install now populates the injected engine binary path that the `LocalDbtBackend` consumes. Two invariants are load-bearing and tested: **fail-closed is an ordering invariant** (`pin_engine` is structurally unreachable until validate succeeds — a failed validate leaves `carve.toml` byte-identical) and **idempotence is two checks** (a no-op requires *pinned* **and** *engine-present-on-disk*; a pin-without-install re-installs). **In this slice:** the bundled `dbt-core` resolve→install→validate→pin path (a Carve-managed `pip`-install venv per engine/version), and the managed/external short-circuits (install nothing). **Deferred to later slices:** warehouse/source `connect` (`carve connect <warehouse|source>`), credential capture (`carve env set|list|unset`), the **Fusion (Apache-2.0 dbt Core v2.0 Rust binary) fetch** — resolution *pins* fusion correctly but install raises `EngineInstallNotSupported` so the deferral can never silently false-succeed — the **full implicit mid-task orchestrator wiring** (only the thin importable seam ships), worker-placement override of the install root (the seam exists, [runtime](./runtime.md) drives it later), and **eager elect-and-pin at init**. The described loop below is the eventual target; status stays **Drafting** while the remaining slices land.

When the orchestrator hits a step that needs setup it doesn't have, it invokes `connect`, which:

1. **Detects the situation** — existing dbt project? dbt Cloud creds present? snowflake-native? a warehouse already connected? — reusing [`init`](./init.md)'s detection + live [`sql`](./sql.md) introspection.
2. **Resolves the right thing** — e.g., the [dbt-execution](./dbt-execution.md) backend + (for bundled) the engine by warehouse (Fusion/dbt Core v2.0 where supported, dbt-core fallback) and version.
3. **Provisions** — installs the bundled engine into the worker environment, or wires the Cloud/native/remote trigger, or opens the warehouse/source connection.
4. **Validates** — a smoke check (a trivial `sql` introspection; a `dbt parse`/`dbt debug`; a source reachability probe) before declaring success.
5. **Pins it back into config** — the resolved backend/engine/version/connection is written to `carve.toml`/`connections.toml`, so it's **declarative and reproducible** from then on (a lockfile, not a black box).

The next run reads the pinned config and does no provisioning — `connect` fires only when something is missing.

### Entry points

- **Explicit:** `carve connect` (and `carve connect <warehouse|source>`) — the first-magical-moment command a user can run directly ("connect my Snowflake / my Stripe").
- **Implicit:** the orchestrator triggers `connect` mid-task when a step needs a connection/backend/engine that isn't set up — the user never has to know it ran, except that the thing now works and the config now records it.
- **`carve env set | list | unset`** — the credential-entry surface `connect` drives (and a user can run directly): `set` takes a value via **masked stdin** and writes it to `.env`; `list` shows names only (**never values**); `unset` removes one. The MCP-equivalent exists for chat-driven flows. This is how secrets are entered *without* pasting them into chat — [init](./init.md) only scaffolds `.env.example`; `carve env`/`connect` write the real `.env`.

**Version detection.** When `connect` provisions or resolves an engine, it also **detects the installed dbt/dlt version and warns if it's outside Carve's tested range**, recording the resolved version in config (the pin). Adapting *generated code* to that version is the engineers' job ([dlt-engineer](./dlt-engineer.md) / [dbt-engineer](./dbt-engineer.md)); detect-and-warn-and-pin is `connect`'s.

> **Updated during implementation (2026-06-26):** because `dbt_version` flows into **both** a `pip` requirement (`dbt-core==<version>`) **and** a managed-engine directory name (`<install_root>/<engine>-<version>`) that `connect` then `mkdir`s and execs (`<dir>/bin/dbt`), an unconstrained version is a path-traversal → code-execution sink. The pin is hardened at two layers: `ComponentConfig._safe_dbt_version` (in `core/config/schema.py`) constrains `dbt_version` at the config-load boundary to a version-token charset (no path separators, `..`, NUL, whitespace, or leading `-`), and `installer.engine_install_dir` raises `UnsafeEnginePath` if the keyed dir ever resolves outside `install_root` — so a directly-constructed pin can't escape either. (This was a HIGH path-traversal found, fixed, and re-confirmed PASS by the security review.)

### Power-user escape hatch

A user who wants control can **elect + pin eagerly** at init (`carve init --dbt-engine … --dbt-version …`, pre-supplied connections) — `connect` then finds everything already set and does nothing. Lazy by default, eager by choice.

## Tests

> **Updated during implementation (2026-06-26):** the bullets realized by the lean first slice live under `tests/core/connect/` + `tests/cli/commands/test_connect_command.py` (all offline via injected install/validate seams + a fake runner, plus one `importorskip`-gated real-dbt-DuckDB install). The **lazy dbt provision** bullet is realized for the **dbt-core (DuckDB)** path (`test_lazy_provision_resolves_installs_validates_and_pins`); the **Fusion (Snowflake)** variant is the deferred binary fetch (asserted to raise `EngineInstallNotSupported` rather than install — `test_install_fusion_raises_not_supported`). Idempotence is split into the two-check pair (`test_second_run_is_noop_when_pinned_and_present` + `test_pinned_but_missing_reinstalls`); fail-closed is `test_failed_validate_leaves_config_byte_identical`; managed/external no-install and the init-division bullets all have shipped tests. The **warehouse connect** bullet awaits the deferred warehouse/source slice. A security regression test (`test_engine_install_dir_rejects_traversal_version`) guards the `dbt_version` path-traversal fix.

- **Integration (lazy dbt provision):** a first `dbt` step with `dbt_env="bundled"` and no engine pinned triggers `connect`, which resolves Fusion (Snowflake) / dbt-core (DuckDB), installs it, validates `dbt parse`, and writes `dbt_engine`/`dbt_version` into `carve.toml`; a second run provisions nothing.
- **Integration (warehouse connect):** `carve connect snowflake` opens + validates the connection via `sql` introspection and records it; a bad credential fails the validate step cleanly (no half-written config).
- **Integration (managed backend, no install):** connecting a snowflake-native or dbt Cloud component wires creds/refs and installs **no** engine.
- **Unit (idempotent):** `connect` is a no-op when the needed backend/connection/engine is already pinned.
- **Unit (init division):** `init` records a detected dbt-Cloud component's *presence* but performs no provisioning; `connect` performs it on first use.

## Acceptance

- A user reaches for a thing (dbt, a warehouse, a source); `connect` sets it up on demand, **validates** it, and leaves **declarative, reproducible** config behind — with no install-time interrogation.
- Bundled-engine provisioning resolves the right engine by warehouse and **pins** it; managed/external backends install nothing.
- `init` (scaffold + detect) and `connect` (provision on demand) are cleanly separated; `connect` is idempotent and fails closed (no partial config on a failed validate).
- `carve connect` works as an explicit first-moment command; the orchestrator also triggers it implicitly.

## Design notes

- **Why a capability, not part of `init`?** Onboarding isn't a one-time event — it recurs every time a new component/connection enters the picture, and it's *intelligent* (detect → resolve → validate), which is exactly what the agent layer is for. Folding it into `init` would force every choice up front and lose the magical, just-in-time quality. `init` is a scaffolder; `connect` is an agent capability.
- **Why provision-then-pin?** Magical first touch + deterministic forever after — the package-manager pattern (resolve once, lock). It's the resolution to "lazy feels uncontrolled": the config always tells you exactly what's set up.
- **Why orchestrator-wielded, not a standing agent?** Onboarding is cross-cutting glue the orchestrator applies mid-task, not a domain specialist with its own deep loop — consistent with the [ai-harness](../_strategy/2026-06-ai-harness.md) taxonomy.

## Open questions

- **Where the bundled engine is installed.**
  > **Resolved for the dbt-core path (2026-06-26):** the bundled engine installs into a **Carve-managed `install_root`** (default `.carve/engines/<engine>-<version>/`, the gitignored scratch dir) as a `pip`-install **venv** (dbt-core, Python), keyed by engine/version so the install is idempotent and "present-on-disk" is a deterministic path check. `install_root` is **injectable at the loop boundary**, so [runtime](./runtime.md) worker-placement can override it later (a co-located worker on the user's box provisions there).

  Still open: the **Fusion (Rust binary) install mechanism** — a *fetched binary*, not a pip venv — is deferred (resolution pins it; install raises `EngineInstallNotSupported`); confirm the fetch/verify mechanism with `runtime`/`dbt-execution` when it lands.
- **Source-connect breadth.** How far `carve connect <source>` goes for dlt sources (credential capture, a reachability probe, handing off to the [dlt-engineer](./dlt-engineer.md)) vs. staying a thin connection step.
- **Phasing.** Which slice of `connect` lands when is a [DELIVERY](../DELIVERY.md) decision; the lazy-provision-and-pin loop pairs naturally with whichever increment first ships the bundled dbt backend.

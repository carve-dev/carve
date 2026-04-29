# M3-13 — `carve doctor` command

**Milestone:** 3 — Polish for adoption
**Estimated effort:** 1 day
**Dependencies:** M1-02 (config loader), M1-06 (Snowflake connector), M2-05 (dbt integration), M3-04 (MCP client)

## Purpose

A diagnostic command that answers "is this Carve install correctly configured and able to do its job?" Catches the common environment problems (missing env vars, expired credentials, wrong dbt version, broken MCP servers) before they show up as confusing pipeline failures.

## Why it matters

Most "Carve doesn't work" support requests are environmental: wrong Python version, missing env var, Snowflake key expired, dbt manifest stale, network blocking the API. A single command that checks all of these surfaces and gives clear "fix it like this" output is the difference between a 2-minute self-resolution and a Slack thread.

## Structure

```bash
$ carve doctor
Carve doctor — diagnosing your installation

  ✓ Carve version          0.1.0 (latest)
  ✓ Python                 3.11.7 (>= 3.11 required)
  ✓ Working directory      /Users/jane/work/jaffle-shop
  ✓ Config file            carve/carve.toml found and valid

  Connections
  ✓ snowflake (default)    Connected as ANALYTICS_USER · 1.2s
  ✓ github                 Authenticated as @jane-doe

  Agents
  ✓ Anthropic API          OK · claude-sonnet-4 reachable · 380ms
  ✓ orchestration          loaded · 1 skill, 0 errors
  ✓ dbt-engineer           loaded · 4 skills, 0 errors
  ✓ snowflake-engineer     loaded · 3 skills, 0 errors
  ✓ quality                loaded · 2 skills, 0 errors

  dbt
  ✓ dbt-core               1.8.4 (>= 1.7 required)
  ✓ Profile                jaffle_shop · target=dev · resolves
  ✓ Manifest               fresh (1 minute old)

  MCP servers
  ✓ pagerduty              Connected · 8 tools available
  ✗ datadog                Cannot connect: timeout after 5s
                           Try: check DATADOG_API_KEY env var or
                                set carve mcp datadog --disabled

  Server
  ✓ Carve API server       Not running locally (no port 8765 listener)
                           Start with: carve serve

  Storage
  ✓ State database         carve/.carve/state.db · 1.2 MB · readable
  ✓ Plan store             3 plans on disk, oldest 4 days

Summary: 1 problem found
  ✗ MCP server datadog cannot connect

Run `carve doctor --verbose` for full details.
```

Each line: status glyph, label, summary, optional next-step hint on failure.

## Check categories

Each check returns a `CheckResult`:

```python
class CheckStatus(str, Enum):
    PASS = "pass"      # ✓
    WARN = "warn"      # ⚠
    FAIL = "fail"      # ✗
    SKIP = "skip"      # ○ (e.g. "Snowflake check skipped — no connection configured")

@dataclass
class CheckResult:
    label: str
    status: CheckStatus
    summary: str                    # Single-line human text
    detail: str | None = None       # Multi-line, shown with --verbose or on FAIL
    fix_hint: str | None = None     # "Try: ..."
    duration_ms: int | None = None
```

Each category is a list of checks, run in order. Categories run in parallel where safe (Snowflake and GitHub can run concurrently; dbt manifest check waits on profile resolution).

## Categories and checks

### Carve self-check
- Carve version, comparing against PyPI latest (with timeout — don't block on network)
- Python version
- Current working directory and whether it looks like a Carve project
- `carve.toml` parses and validates against schema

### Connections
- For each connection in `carve/connections.toml`:
  - Resolve env vars without exposing values
  - For Snowflake: `SELECT CURRENT_USER(), CURRENT_ROLE(), CURRENT_WAREHOUSE()` round-trip
  - For GitHub: `GET /user` to verify token
  - For Postgres etc.: connection round-trip
- Failure modes that get specific hints:
  - Missing env var → "Set $SNOWFLAKE_ACCOUNT in your shell"
  - Expired key → "Snowflake says key not associated with user; rotate via..."
  - Wrong role → "Role REPORTER cannot use warehouse COMPUTE_XL"

### Agents
- Anthropic API key set and reachable (model list endpoint)
- Each agent in `carve/agents/` loads without error
- Each skill referenced by an agent exists
- MCP-namespaced skills require their MCP servers be reachable (deferred to MCP section)

### dbt
- `dbt --version` works
- Version meets `>=1.7` requirement
- `profiles.yml` resolves the profile referenced in `dbt_project.yml`
- Manifest staleness (warn at 24h+, fail at 7d+)
- If staleness fails: hint is "Run `dbt parse` or `dbt compile` to refresh"

### MCP servers
- For each server in `carve/mcp.toml`:
  - Spawn (stdio) or connect (http) with 5s timeout
  - List tools (verifies it speaks MCP protocol)
  - Disconnect cleanly
- Failure → fix hint includes the env var or config that's likely wrong, plus how to disable

### Server / runtime
- Is `carve serve` running on the configured port? (informational, not a fail)
- Is the state DB present, readable, and not corrupted? (`PRAGMA integrity_check`)
- Is the plan store directory writable?
- Disk space on the working volume (warn under 1GB free)

## Flags

- `--verbose` / `-v` — show detail blocks, not just summaries
- `--json` — machine-readable output (for CI integration)
- `--category <name>` — run only one category (e.g. `--category connections`)
- `--fix` — for a small set of auto-fixable issues, attempt the fix (e.g. refresh manifest, write missing default config). Conservative; never modifies credentials.
- `--no-network` — skip checks that require network (faster local check)

## JSON output

```json
{
  "carve_version": "0.1.0",
  "python_version": "3.11.7",
  "categories": [
    {
      "name": "connections",
      "checks": [
        {
          "label": "snowflake (default)",
          "status": "pass",
          "summary": "Connected as ANALYTICS_USER",
          "duration_ms": 1213
        }
      ]
    }
  ],
  "summary": {
    "passed": 18,
    "warned": 0,
    "failed": 1,
    "skipped": 0
  }
}
```

## Exit codes

- `0` — all checks pass (warnings included)
- `1` — at least one check failed
- `5` — internal error in doctor itself (consistent with `carve` global exit code policy)

## CI usage

Doctor is well-suited for CI gate:

```yaml
- name: Verify Carve config
  run: carve doctor --json --no-network > doctor.json
- name: Fail on errors
  run: jq -e '.summary.failed == 0' doctor.json
```

Each example project (M3-11) runs `carve doctor` in CI as a smoke test.

## Implementation

```python
# src/carve/doctor/__init__.py
class Doctor:
    def __init__(self, config: CarveConfig):
        self.config = config
        self.checks: list[CheckCategory] = [
            SelfCheck(),
            ConnectionsCheck(config),
            AgentsCheck(config),
            DbtCheck(config),
            McpCheck(config),
            RuntimeCheck(config),
        ]

    async def run(
        self, category: str | None = None, no_network: bool = False
    ) -> DoctorReport:
        results = []
        for cat in self.checks:
            if category and cat.name != category:
                continue
            cat_result = await cat.run(no_network=no_network)
            results.append(cat_result)
        return DoctorReport(results=results)
```

Each `CheckCategory` is a class with an async `run` method that returns `list[CheckResult]`. Categories are independent — adding a new one means dropping a class into `doctor/checks/` and registering it.

## Tests

- Unit: each check class tested with mocked dependencies (Snowflake driver, dbt subprocess, MCP server)
- Integration: a "broken project" fixture deliberately fails several checks; assert correct categorization and exit code
- JSON output schema validates against `doctor_report.schema.json`
- `--fix` for auto-fixable issues is tested end-to-end (e.g. manifest refresh)

## Acceptance criteria

- [ ] `carve doctor` runs in <5 seconds against a healthy local install
- [ ] All check categories implemented with at least 3 checks each
- [ ] Failed checks always include a `fix_hint`
- [ ] `--json` output validates against schema
- [ ] `--category connections` runs only that category
- [ ] Doctor is invoked in example-project CI workflows
- [ ] Doctor docs published at `docs.carve.dev/reference/cli#doctor`

## Files this spec produces

```
src/carve/doctor/
├── __init__.py             Doctor entry point
├── report.py               DoctorReport, CheckResult, formatting
├── checks/
│   ├── __init__.py
│   ├── base.py             CheckCategory base class
│   ├── self_check.py
│   ├── connections.py
│   ├── agents.py
│   ├── dbt.py
│   ├── mcp.py
│   └── runtime.py
├── render.py               Pretty terminal output
└── schema.py               JSON output schema

src/carve/cli/commands/doctor.py    CLI entry, flag parsing

tests/doctor/
├── test_doctor_runner.py
├── test_check_connections.py
├── test_check_dbt.py
├── test_render.py
└── fixtures/
    └── broken_project/        Fixture with deliberate misconfiguration
```

## What this enables

- Self-service problem resolution
- A consistent CI gate for Carve projects
- An onboarding confidence-builder ("doctor says I'm ready")
- A clear extension point for future checks (e.g. cost-budget check, policy check)

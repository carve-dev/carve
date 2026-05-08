# Calling `carve el deploy` from CI/CD

`carve el deploy` is one deterministic CLI command that promotes an EL
artifact from one target to another. It copies files, applies DDL via
the deploy role, smoke-verifies via the runtime role, and records a
deploy `Run` row — all in one shot. Wrap it in whatever CI/CD tooling
you already operate.

```bash
# In your CI pipeline (GitHub Actions, GitLab CI, Airflow PythonOperator, etc.):
pip install carve
carve el deploy iowa_liquor_sales --from dev --to prod --yes
```

Set the `<TARGET>_SNOWFLAKE_*` env vars (including the matching
`<TARGET>_SNOWFLAKE_DEPLOY_*` variants) as CI secrets. Carve resolves
``[snowflake.<target>_deploy]`` for the DDL-application step and
``[snowflake.<target>]`` for the runtime smoke-check; both are
populated from environment variables interpolated into your
`carve/connections.toml` at run time.

Run on whatever event makes sense for your team — PR merge, manual
approval gate, scheduled deploy, or post-test promotion. Carve is
agnostic to the trigger; the command produces the same outcome
regardless of where it runs.

The deploy is **idempotent** by construction: re-running on an
unchanged source is safe. The DDL contract from `P1-06`
(`CREATE OR REPLACE`, `IF NOT EXISTS`, `GRANT IF EXISTS`) makes the
DDL re-application a no-op; the file copy is a byte-for-byte mirror
that becomes a no-op when destination already matches. This means you
can wire deploy into a workflow that retries on transient failures
without worrying about partial-application damage.

Track deploy history via `carve runs --pipeline <name>` filtered to
``kind="deploy"`` rows. Each invocation lands one `Run` whose
`target_id` is the source build's id, so you can correlate "what
shipped" with "where it shipped from".

Carve does **not** open PRs, manage branches, push to a remote, or
know about Git providers. The user's CI/CD wraps the command and owns
those concerns.

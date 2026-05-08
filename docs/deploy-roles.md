# Deploy role / runtime role pattern

Carve's recommended Snowflake setup uses **two** roles per target —
one with DML-only privileges for runtime, one with CREATE/ALTER/GRANT
privileges for deploys. The pattern is documented here, optional in
practice (single-developer dev targets can collapse the two onto a
single all-powerful role), and absolutely worth the effort for any
prod-class target where audit trails and least-privilege hygiene
matter.

## Why two roles

- **Runtime role** runs `carve el run`. It needs `SELECT, INSERT,
  UPDATE, DELETE` on its destination tables — nothing more. Compromise
  of the runtime role's credentials means the attacker can mutate
  data, but they can't change schema, escalate privileges, or attach
  themselves to the deploy role.
- **Deploy role** runs the DDL-application step of `carve el deploy`.
  It needs `CREATE / ALTER / GRANT / OWNERSHIP` on the destination
  schema. Compromise of the deploy role's credentials means the
  attacker can change schema and grants — strictly more dangerous,
  which is why these credentials live behind tighter rotation /
  vaulting / approval controls in CI.

The runtime / deploy split is a Snowflake-native expression of the
"least privilege per workflow" principle and slots cleanly into most
existing access-control schemes.

## How Carve wires it up

`carve/connections.toml` carries one block per target. Carve looks
up `[snowflake.<target>]` for the runtime role and
`[snowflake.<target>_deploy]` for the deploy role. The `_deploy`
suffix is convention, not a magic string — Carve simply requires the
exact name to be present.

```toml
[snowflake.prod]
account   = "${PROD_SNOWFLAKE_ACCOUNT}"
user      = "${PROD_SNOWFLAKE_USER}"
password  = "${PROD_SNOWFLAKE_PASSWORD}"
role      = "TRANSFORMER_PROD"          # runtime role: DML only
warehouse = "${PROD_SNOWFLAKE_WAREHOUSE}"
database  = "${PROD_SNOWFLAKE_DATABASE}"

[snowflake.prod_deploy]
account   = "${PROD_SNOWFLAKE_ACCOUNT}"
user      = "${PROD_SNOWFLAKE_DEPLOY_USER}"
password  = "${PROD_SNOWFLAKE_DEPLOY_PASSWORD}"
role      = "DEPLOYER_PROD"             # has CREATE on PROD.RAW + GRANT to TRANSFORMER_PROD
warehouse = "${PROD_SNOWFLAKE_WAREHOUSE}"
database  = "${PROD_SNOWFLAKE_DATABASE}"
```

`carve el deploy` exits 2 with a doc-link error if the
`<target>_deploy` connection is missing. `carve el run` and
`carve el verify` only need the runtime block; they never touch the
deploy connection.

## Recommended Snowflake setup SQL

The exact roles you create depend on your account's existing
hierarchy, but the smallest workable shape is:

```sql
-- runtime role: DML only on the destination schema
CREATE ROLE IF NOT EXISTS TRANSFORMER_PROD;
GRANT USAGE ON DATABASE PROD TO ROLE TRANSFORMER_PROD;
GRANT USAGE ON SCHEMA PROD.RAW TO ROLE TRANSFORMER_PROD;
GRANT SELECT, INSERT, UPDATE, DELETE
  ON FUTURE TABLES IN SCHEMA PROD.RAW
  TO ROLE TRANSFORMER_PROD;
GRANT SELECT, INSERT, UPDATE, DELETE
  ON ALL TABLES IN SCHEMA PROD.RAW
  TO ROLE TRANSFORMER_PROD;

-- deploy role: CREATE / OWNERSHIP / GRANT on the destination schema
CREATE ROLE IF NOT EXISTS DEPLOYER_PROD;
GRANT USAGE ON DATABASE PROD TO ROLE DEPLOYER_PROD;
GRANT CREATE SCHEMA ON DATABASE PROD TO ROLE DEPLOYER_PROD;
GRANT OWNERSHIP ON SCHEMA PROD.RAW
  TO ROLE DEPLOYER_PROD COPY CURRENT GRANTS;
-- Allow the deploy role to grant DML privileges to the runtime role.
GRANT ROLE TRANSFORMER_PROD TO ROLE DEPLOYER_PROD;

-- Bind the roles to their service users.
GRANT ROLE TRANSFORMER_PROD TO USER PROD_RUNTIME_USER;
GRANT ROLE DEPLOYER_PROD TO USER PROD_DEPLOY_USER;
```

After this, every `carve el deploy --to prod` connects as the
deploy role to apply DDL and grants, then connects as the runtime
role for the smoke-verify pass. `carve el run --target prod` only
ever touches the runtime role.

## When to collapse the split

For single-developer dev targets the deploy/runtime distinction is
ceremony. Point both `[snowflake.dev]` and `[snowflake.dev_deploy]`
at the same role-with-everything, and move on:

```toml
[snowflake.dev]
# ... full-privilege block ...

[snowflake.dev_deploy]
# Same credentials, same role.
```

The split only meaningfully matters once you start caring about who
shipped what, and Carve doesn't force you to care before you're
ready.

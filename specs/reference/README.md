# Reference

Canonical reference material for Carve, derived from the capability specs. Spec [reference-docs](../capabilities/reference-docs.md) keeps these in lock-step with the code at build time via completeness tests (every Typer command appears in the CLI reference; every init-scaffolded file appears in the config schema).

- [cli-reference.md](./cli-reference.md) — every `carve` command, with flags, examples, and exit codes
- [config-schema.md](./config-schema.md) — every config file Carve reads or writes, with schema, defaults, and examples (the control-plane `carve.toml`, `pipelines/<name>.toml`, the `carve/` bundle)
- [glossary.md](./glossary.md) — Carve's vocabulary (control plane, components, the AI harness, the runtime)
- [governance.md](./governance.md) — license (Apache 2.0), DCO, the OSS ↔ hosted relationship, and the RFC + release process

For tutorials and walkthroughs see `docs/` at the repo root (each spec ships its own). For the REST API, the authoritative reference is the OpenAPI schema at `/api/openapi.json` ([rest-api](../capabilities/rest-api.md)); for MCP tools, see `docs/mcp-server.md` ([mcp-server](../capabilities/mcp-server.md)).

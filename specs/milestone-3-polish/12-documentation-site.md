# M3-12 — Documentation site

**Milestone:** 3 — Polish for adoption
**Estimated effort:** 2 days
**Dependencies:** M3-11 (examples to link to); content from all prior specs

## Purpose

A public documentation site at `docs.carve.dev` that gives new users a clear path from "what is this" to "I'm productive." Built with mkdocs-material, deployed via GitHub Pages from the main repo. Versioned alongside Carve releases.

## Why mkdocs-material

- Markdown-native — same format as the design docs we've already written, so content translates directly
- Built-in search, dark mode, code highlighting
- Mature, low-maintenance, used by FastAPI / Pydantic / many similar projects
- mkdocstrings plugin generates Python API reference from docstrings
- Versioning via mike plugin (so docs.carve.dev/0.1, /0.2, /latest all coexist)

We don't need Docusaurus or a custom Next.js site. The content is the product, not the chrome.

## Information architecture

```
docs.carve.dev/
├── /                              Landing — what is Carve, who's it for
├── /getting-started/
│   ├── installation               pip install carve, prereqs
│   ├── quickstart                 5-minute path to first pipeline
│   ├── first-pipeline             Annotated walkthrough
│   └── concepts                   Mental model: agents, skills, steps
├── /guides/
│   ├── existing-dbt-project       Brownfield onboarding
│   ├── connecting-snowflake       Auth, key-pair, role setup
│   ├── github-integration         PRs, CI hooks
│   ├── scheduling-pipelines       Cron, manual triggers
│   ├── writing-conventions        conventions.md as a skill
│   ├── using-mcp                  Adding external MCP servers
│   ├── custom-step-types          Plugin authoring
│   └── deploying-production       Server setup, secrets, monitoring
├── /reference/
│   ├── cli                        Every command, flag, exit code
│   ├── config                     carve.toml schema reference
│   ├── pipeline-spec              Pipeline TOML schema
│   ├── agent-spec                 Agent definition schema
│   ├── skill-spec                 Skill schema and SDK
│   ├── api                        REST API reference (auto-gen from OpenAPI)
│   ├── python-api                 Python module reference (mkdocstrings)
│   └── glossary                   Terms and definitions
├── /examples/
│   ├── ecommerce                  Walks through example 1
│   ├── brownfield                 Walks through example 2
│   └── data-platform              Walks through example 3
├── /architecture/
│   ├── overview                   How Carve is structured
│   ├── agents                     Agent layer deep-dive
│   ├── execution                  Runner, steps, state
│   └── extending                  Hooks for custom code
├── /contributing/
│   ├── development-setup
│   ├── code-style
│   ├── adding-skills              Internal contribution patterns
│   └── governance                 RFC process, decision-making
└── /changelog                     Auto-generated from git tags
```

## Sourcing content

A meaningful chunk of content already exists in the design docs (`carve-docs/`). The migration plan:

| Source | Destination |
|---|---|
| `PRD.md` (sections) | Mostly internal; not on public site |
| `ARCHITECTURE.md` | `/architecture/overview` (lightly edited) |
| Spec files (M1, M2, M3) | Internal — these are work-product specs, not user docs |
| `reference/config-schema.md` | `/reference/config` directly |
| `reference/cli-reference.md` | `/reference/cli` directly |
| `reference/glossary.md` | `/reference/glossary` directly |

New content to write for the site:
- Landing page (high signal, screenshots, code snippet)
- Quickstart and concepts (intentional, didactic)
- Each guide (most are 1-2 pages)
- Architecture overview (user-facing, less spec-heavy)

## Landing page

The landing page does three things in this order:
1. **What it is.** One sentence + a screenshot of the workbench.
2. **Why it's different.** Three short cards: AI-first authoring, code-as-source-of-truth, owns execution.
3. **Try it.** Code block with `pip install carve && carve init --example ecommerce`.

No marketing copy. No "revolutionize." Show, don't tell.

## Quickstart

The five-minute path:

```bash
pip install carve
carve init --example ecommerce
cd carve-ecommerce
export SNOWFLAKE_ACCOUNT=...  # 4 vars total
carve build
carve serve  # opens browser to workbench
```

Each command has a one-line explanation. Screenshots between commands so a reader can confirm they're on the right track.

## Reference docs strategy

**CLI reference** is generated from typer's `--help` output via a script. Drift between docs and reality is the most common documentation failure mode; we eliminate it by generating.

**Config reference** is generated from pydantic schemas using `pydantic.json_schema()` plus a small custom renderer that produces tables of fields, types, defaults, descriptions.

**API reference** is generated from FastAPI's OpenAPI spec via `mkdocs-render-swagger-plugin`.

**Python API reference** is generated from docstrings via `mkdocstrings`.

This means /reference/ is mostly machine-generated and stays correct. Only narrative docs are hand-written.

## Versioning with mike

Docs are versioned per Carve release:
- `docs.carve.dev/latest` → current stable
- `docs.carve.dev/dev` → main branch
- `docs.carve.dev/0.1` → v0.1.x
- `docs.carve.dev/0.2` → v0.2.x

A version-switcher dropdown in the header. Old versions stay accessible — important when users pin Carve versions in production.

## Search

mkdocs-material's built-in search is good enough for v1. If usage data later shows search drives a lot of traffic, consider Algolia DocSearch (free for OSS).

## Build pipeline

`.github/workflows/docs.yml`:
- On push to `main` → build and deploy to `dev/`
- On tag `v*` → build and deploy to `<version>/` and `latest/`
- mike pushes to `gh-pages` branch
- GitHub Pages serves it
- CNAME set to `docs.carve.dev`

## Style conventions

- Sentence case for headings, not Title Case
- Code blocks are copyable (mkdocs-material has a copy button by default — keep it on)
- Every guide ends with "What's next?" pointing to 2-3 related pages
- Inline links are preferred over "see X below" — readers scan, not read linearly
- Screenshots have alt text (accessibility, but also: when GitHub Pages images break, alt text helps)
- No marketing language. State capabilities; let users decide if they're impressed.

## Telemetry

Plausible Analytics (privacy-respecting, no cookies). Tracks:
- Page views
- Search queries (drives gap analysis)
- Outbound clicks to GitHub
- 404s (drives broken-link cleanup)

We do not track users individually. Plausible doesn't make that easy and we wouldn't anyway.

## Acceptance criteria

- [ ] `docs.carve.dev` resolves and serves the site
- [ ] All sections in the IA exist with non-stub content
- [ ] CLI, config, API, Python API references are generated, not hand-written
- [ ] Versioning works: `/latest`, `/dev`, `/0.1` all serve correct content
- [ ] Lighthouse score >90 on landing and quickstart pages
- [ ] Search returns useful results for "snowflake auth", "schedule pipeline", "convert dbt project"
- [ ] All examples (M3-11) are linked from the relevant guides
- [ ] CI deploys docs on push to main and on release tags

## Files this spec produces

```
docs/                          (in main Carve repo)
├── mkdocs.yml                 Config — nav, plugins, theme
├── index.md                   Landing
├── getting-started/
│   ├── installation.md
│   ├── quickstart.md
│   ├── first-pipeline.md
│   └── concepts.md
├── guides/
│   ├── existing-dbt-project.md
│   ├── connecting-snowflake.md
│   ├── github-integration.md
│   ├── scheduling-pipelines.md
│   ├── writing-conventions.md
│   ├── using-mcp.md
│   ├── custom-step-types.md
│   └── deploying-production.md
├── reference/
│   ├── cli.md                 (generated)
│   ├── config.md              (generated)
│   ├── pipeline-spec.md
│   ├── agent-spec.md
│   ├── skill-spec.md
│   ├── api.md                 (generated)
│   ├── python-api.md          (generated)
│   └── glossary.md
├── examples/
│   ├── ecommerce.md
│   ├── brownfield.md
│   └── data-platform.md
├── architecture/
│   ├── overview.md
│   ├── agents.md
│   ├── execution.md
│   └── extending.md
└── contributing/
    ├── development-setup.md
    ├── code-style.md
    ├── adding-skills.md
    └── governance.md

scripts/
├── gen_cli_docs.py            Renders typer --help to markdown
├── gen_config_docs.py         Renders pydantic schemas to markdown
└── docs_check.py              Linter: alt-text, broken anchors, etc.

.github/workflows/docs.yml
```

## What this enables

- Self-service onboarding (no Slack required)
- Reference correctness (generated docs don't drift)
- Searchable knowledge base for the community
- A target for community contribution ("docs PR" is the gateway to "code PR")

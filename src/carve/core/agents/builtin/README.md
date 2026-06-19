# Built-in agent definitions

This directory is the **built-in agent discovery root**: `*.md` files here
are loaded by `carve.core.agents.discovery.AgentDiscovery` as declarative
agents (frontmatter + system-prompt body), and a user file at
`<project>/carve/agents/<name>.md` overrides a built-in of the same name.

It ships **empty of domain agents on purpose.** The extensibility slice
(this increment) builds the *loader, discovery, registry, and router* — the
machinery. The actual domain agents (the dlt engineer, the dbt engineer,
the explorer, recovery, the SQL agent) are authored by their own
capability specs (04 / 08 / 12 / recovery / SQL) in later increments; each
drops its `<name>.md` here against this same loader.

A `.md` file in this directory is parsed by the **safe** loader
(`loader.load_agent_file`): YAML frontmatter via `yaml.safe_load` (no
object construction, no code execution), a 64 KiB size cap, and any
bundled `scripts/`/`resources/` read as inert paths — never executed at
load.

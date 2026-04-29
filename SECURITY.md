# Security policy

## Reporting a vulnerability

If you believe you have found a security vulnerability in Carve, please report it privately. **Do not open a public issue.**

Email **security@carve-dev.org** with:

- A description of the issue
- Steps to reproduce
- The version or commit SHA you tested against
- Any suggested mitigations or patches you've considered

You should receive an acknowledgement within 3 business days. If you do not, please follow up.

## Embargo policy

We follow a coordinated disclosure model:

- **Day 0** — report received and acknowledged.
- **Days 1–14** — triage, reproduce, and develop a fix.
- **Day 14** — target date for a patched release. The window may extend if the fix requires substantial work or coordination with downstream projects.
- **Public disclosure** — happens on or shortly after the patched release ships. Reporters who want credit are credited in the release notes; reporters who prefer anonymity remain anonymous.

If a vulnerability is being actively exploited in the wild, we will accelerate the timeline and may release a patch before a full fix for related issues is complete.

## Scope

Carve is pre-alpha. The threat model evolves as the codebase matures. As of this writing, the in-scope concerns include:

- Credential handling (Snowflake, Anthropic API keys)
- Code execution by `LocalVenvRunner` (subprocess sandboxing, env var leakage)
- SQL generation by agents (parameterization, injection in user-supplied inputs)
- File operations by agents (path traversal in `read_file` / `write_file` tools)
- dbt manifest reading (path traversal if manifest path is user-supplied)
- Dependencies and supply chain

Issues in dependencies should be reported upstream first, with a parallel notification to us so we can pin or patch.

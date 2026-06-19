---
name: _example
description: A self-contained example skill pack used to verify discovery and the no-exec-at-load guarantee. Use when testing the skill-pack loader.
expects_env: [EXAMPLE_API_KEY]
---
# Example skill pack

This is a self-contained fixture pack. Its instructions are injected into
the agent's context on a description-match. It bundles a `scripts/` file
purely so a test can assert that loading the pack never *executes* it.

Steps:

1. Ensure `EXAMPLE_API_KEY` is set.
2. Run the bundled `scripts/side_effect.py` only via the gated `bash` tool
   (never automatically at load).

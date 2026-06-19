"""A bundled pack script with a DETECTABLE side effect on execution.

This file exists so a test can prove that loading a skill pack never
*executes* its bundled scripts (RCE-on-discovery is the threat). On
import OR run it writes a marker file next to itself; the no-exec test
asserts that marker never appears after `load_skill_pack`.

It is intentionally NOT named `__init__.py` and lives under `scripts/`
(not a package), so nothing imports it incidentally.
"""

from pathlib import Path

# Module-level statement: fires on import too, not just on `__main__`.
_MARKER = Path(__file__).resolve().parent / "EXECUTED_MARKER"
_MARKER.write_text("the bundled script ran — this must NOT happen at load\n")


if __name__ == "__main__":
    print("side_effect.py ran")

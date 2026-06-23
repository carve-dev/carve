"""The four dlt authoring-strategy skill packs load and match on description.

The packs live under
``src/carve/core/skills/builtin/dlt_strategies/<strategy>/SKILL.md`` and are
content packs (no ``scripts/`` bundle). Each must:

* load via ``load_skill_pack`` (frontmatter ``name`` + ``description``),
* be discoverable from the ``dlt_strategies`` root,
* match on its strategy keyword via ``SkillPackLibrary.match``
  (curated / rest / native / singer),
* inject its instructions via the ``lookup_skill_pack`` tool.
"""

from __future__ import annotations

from pathlib import Path

import carve
from carve.core.skills.pack_discovery import SkillPackLibrary
from carve.core.skills.packs import load_skill_pack

# The packs ship inside the installed `carve` package.
_STRATEGIES_ROOT = (
    Path(carve.__file__).resolve().parent
    / "core"
    / "skills"
    / "builtin"
    / "dlt_strategies"
)

# (pack name, the keyword its description must match on).
_PACKS = {
    "curated_library": "curated",
    "rest_api_config": "rest",
    "native_dlt": "native",
    "singer_wrapper": "singer",
}


def test_strategies_root_exists() -> None:
    assert _STRATEGIES_ROOT.is_dir(), f"missing strategies root {_STRATEGIES_ROOT}"
    for name in _PACKS:
        assert (_STRATEGIES_ROOT / name / "SKILL.md").is_file(), name


def test_each_pack_loads_with_required_fields() -> None:
    for name in _PACKS:
        pack = load_skill_pack(_STRATEGIES_ROOT / name)
        assert pack.name == name
        assert pack.description  # required, non-empty
        assert pack.instructions  # body guidance present
        # These are content packs — no bundled scripts.
        assert pack.script_paths == ()


def test_all_four_discoverable_from_the_root() -> None:
    library = SkillPackLibrary([_STRATEGIES_ROOT])
    discovered = {p.name for p in library.discover()}
    assert set(_PACKS).issubset(discovered), discovered


def test_each_pack_matches_its_strategy_keyword() -> None:
    library = SkillPackLibrary([_STRATEGIES_ROOT])
    for name, keyword in _PACKS.items():
        matches = library.match(keyword)
        assert any(m.name == name for m in matches), (name, keyword, matches)


def test_lookup_tool_injects_pack_instructions() -> None:
    library = SkillPackLibrary([_STRATEGIES_ROOT])
    tool = library.make_lookup_tool()
    for name in _PACKS:
        content = tool.executor({"pack_name": name})
        assert isinstance(content, str)
        assert name in content
        # The verification step is part of every strategy pack's body.
        assert "Verification" in content

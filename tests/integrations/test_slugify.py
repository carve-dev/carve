"""Tests for `slugify` + the derived workspace dir name.

`slugify(url) + "-" + branch` must be **stable** (same input → same
output, deterministically) and **collision-safe**: equivalent URLs for
the same repo collapse to one slug, distinct repos stay distinct, and
filesystem-hostile characters never leak through.

*(layout spec Tests: unit bullet 3)*
"""

from __future__ import annotations

import re

import pytest

from carve.integrations.component_locator import slugify, workspace_dirname

# Equivalence classes: URLs that denote the SAME repo and must slugify
# identically. Each inner list is one repo across ssh/https, with/without
# `.git`, and with/without an explicit port.
_EQUIVALENT = [
    [
        "https://github.com/myorg/analytics.git",
        "https://github.com/myorg/analytics",
        "git@github.com:myorg/analytics.git",
        "git@github.com:myorg/analytics",
        "ssh://git@github.com/myorg/analytics.git",
        "ssh://git@github.com:22/myorg/analytics",
    ],
    [
        "https://gitlab.example.com/team/dbt-models.git",
        "git@gitlab.example.com:team/dbt-models.git",
    ],
]

# Distinct repos that must NOT collide with each other.
_DISTINCT = [
    "https://github.com/myorg/analytics.git",
    "https://github.com/otherorg/analytics.git",  # different org
    "https://gitlab.com/myorg/analytics.git",  # different host
    "https://github.com/myorg/reporting.git",  # different repo
]


class TestSlugifyStable:
    @pytest.mark.parametrize("url", [u for group in _EQUIVALENT for u in group])
    def test_deterministic(self, url: str) -> None:
        assert slugify(url) == slugify(url)

    def test_filesystem_safe_charset(self) -> None:
        for group in _EQUIVALENT:
            for url in group:
                slug = slugify(url)
                # Only lowercase alphanumerics and single dashes; no slashes,
                # colons, @, or dots that would fork into subdirs or be unsafe.
                assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug), slug


class TestSlugifyCollisionSafe:
    @pytest.mark.parametrize("group", _EQUIVALENT)
    def test_equivalent_urls_share_one_slug(self, group: list[str]) -> None:
        slugs = {slugify(u) for u in group}
        assert len(slugs) == 1, f"expected one slug, got {slugs}"

    def test_distinct_repos_get_distinct_slugs(self) -> None:
        slugs = [slugify(u) for u in _DISTINCT]
        assert len(set(slugs)) == len(slugs), dict(zip(_DISTINCT, slugs, strict=True))


class TestWorkspaceDirname:
    _URL = "git@github.com:org/repo.git"

    def test_ref_wins_over_branch(self) -> None:
        name = workspace_dirname(self._URL, "9f3a1c7", "main")
        assert name.endswith("-9f3a1c7")
        assert "main" not in name

    def test_branch_used_when_no_ref(self) -> None:
        assert workspace_dirname(self._URL, None, "develop").endswith("-develop")

    def test_default_when_neither_ref_nor_branch(self) -> None:
        assert workspace_dirname(self._URL, None, None).endswith("-default")

    def test_distinct_revisions_of_same_repo_differ(self) -> None:
        a = workspace_dirname(self._URL, None, "main")
        b = workspace_dirname(self._URL, None, "dev")
        assert a != b
        # ...but share the repo stem.
        assert a.rsplit("-", 1)[0] == b.rsplit("-", 1)[0]

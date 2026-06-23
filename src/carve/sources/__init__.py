"""Carve's curated dlt source library.

Each subdirectory is a **skill pack** (``SKILL.md`` + an inert bundled
``scripts/`` dlt source) describing a ready-to-use connector. ``dlt_library``
(see :mod:`carve.integrations.dlt.library`) lists/looks-up/copies these packs
into a project's ``el/<component>/`` tree, and ``pack_discovery`` accepts this
directory as an ``extra_roots`` entry for description-match content injection.

This package is a marker for the corpus root — the packs themselves are
data-and-prose, never imported by Carve at runtime (the pack loader records
their script paths but never imports them). ``_reference_hackernews`` is the one
reference pack proving the framework slots a curated source in.
"""

from __future__ import annotations

__all__: list[str] = []

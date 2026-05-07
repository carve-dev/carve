"""`SkillResult` — uniform return shape for skill executions.

Skills wrap their data payload in this dataclass so the agent (and the
caching executor) get a consistent envelope: the data itself, a
`truncated` flag for capped queries, and a `total_count` that reports
how many rows existed before truncation. `next_cursor` is reserved for
Pillar 2's pagination support; Pillar 1 leaves it `None`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class SkillResult:
    """Wrapper for skill output.

    Attributes:
        data: The actual payload — either a list of rows or a dict.
        truncated: True when a cap was hit and rows were dropped.
        total_count: When known, the row count before truncation.
        next_cursor: Reserved for Pillar 2 pagination; always `None` here.
    """

    data: Any
    truncated: bool = False
    total_count: int | None = None
    next_cursor: str | None = None

"""Carve ↔ dlt integration: the substrate the DLT engineer (Increment 3) runs on.

`verify.py` turns a `dlt pipeline run` into the harness verification loop's
`CheckResult` (reading dlt's on-disk load package — the real record of what
loaded, which schema changes applied, and whether any job failed).
`code_emitter.py` writes the Carve provenance header that
`carve.integrations.provenance` reads back.
"""

from __future__ import annotations

from carve.integrations.dlt.code_emitter import emit_provenance_header, with_provenance_header
from carve.integrations.dlt.skills import (
    make_existing_dlt_inspect_tool,
    make_rest_api_explore_tool,
)
from carve.integrations.dlt.verify import (
    LoadPackageReport,
    parse_dlt_run,
    read_latest_load_package,
)

__all__ = [
    "LoadPackageReport",
    "emit_provenance_header",
    "make_existing_dlt_inspect_tool",
    "make_rest_api_explore_tool",
    "parse_dlt_run",
    "read_latest_load_package",
    "with_provenance_header",
]

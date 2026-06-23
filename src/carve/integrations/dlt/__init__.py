"""Carve ↔ dlt integration: the substrate the DLT engineer (Increment 3) runs on.

`verify.py` turns a finished dlt load (executed via Carve's venv runner) into
the harness verification loop's `CheckResult` by reading dlt's on-disk load
package — the real record of what loaded, which schema changes applied, and
whether any job failed.
`code_emitter.py` writes the Carve provenance header that
`carve.integrations.provenance` reads back.
"""

from __future__ import annotations

from carve.integrations.dbt.sources import make_dbt_source_lookup_tool
from carve.integrations.dlt.code_emitter import emit_provenance_header, with_provenance_header
from carve.integrations.dlt.library import make_dlt_library_tool
from carve.integrations.dlt.runner import (
    dlt_inspect_command,
    make_dlt_parse_fn,
    make_dlt_verification_loop,
    run_dlt_check,
)
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
    "dlt_inspect_command",
    "emit_provenance_header",
    "make_dbt_source_lookup_tool",
    "make_dlt_library_tool",
    "make_dlt_parse_fn",
    "make_dlt_verification_loop",
    "make_existing_dlt_inspect_tool",
    "make_rest_api_explore_tool",
    "parse_dlt_run",
    "read_latest_load_package",
    "run_dlt_check",
    "with_provenance_header",
]

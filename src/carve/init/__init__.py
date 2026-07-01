"""`carve init` internals: detect → resolve → scaffold.

The CLI command (`carve.cli.commands.init`) is a thin orchestrator over this
package:

* :mod:`carve.init.detect` — inspect the directory (brownfield dbt/dlt, git,
  docker, re-init) into a :class:`~carve.init.detect.Detection`.
* :mod:`carve.init.plan` — resolve the four orthogonal axes (postgres, dbt,
  dlt, memory) into an :class:`~carve.init.plan.InitPlan`.
* :mod:`carve.init.scaffold` — write the project files idempotently from the
  plan.

See the init capability spec. This lean first pass covers detection, the
control-plane `carve.toml` scaffold (simple-mode + separate-component blocks),
and non-interactive resolution; convention inference, interactive prompts, and
`--migrate-from-targets` are deferred (see DELIVERY). The OSS default API-token
bootstrap is closed by the rest-api capability: `carve init` mints it
best-effort when the state store is reachable, and `carve serve` mints it
reliably on startup.
"""

from carve.init.detect import Detection, detect
from carve.init.plan import ComponentSpec, InitError, InitOptions, InitPlan, resolve
from carve.init.scaffold import ScaffoldResult, scaffold

__all__ = [
    "ComponentSpec",
    "Detection",
    "InitError",
    "InitOptions",
    "InitPlan",
    "ScaffoldResult",
    "detect",
    "resolve",
    "scaffold",
]

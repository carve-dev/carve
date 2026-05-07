"""Built-in skills — register catalog skills into the default registry.

Importing this module is the boundary at which built-in skills join the
process-wide default registry. The registration is idempotent: re-imports
(e.g. across tests) re-register the same function objects under the same
names, which the registry treats as a no-op.
"""

from carve.core.skills.builtin import catalog
from carve.core.skills.registry import default_registry

_REGISTERED = False


def _register_all() -> None:
    """Add every catalog skill to the default registry once."""
    global _REGISTERED
    if _REGISTERED:
        return
    registry = default_registry()
    registry.register(catalog.list_databases)
    registry.register(catalog.list_schemas)
    registry.register(catalog.list_tables)
    registry.register(catalog.describe_table)
    registry.register(catalog.table_exists)
    _REGISTERED = True


_register_all()


"""Built-in skills — register catalog skills into the default registry.

Importing this module is the boundary at which built-in skills join the
process-wide default registry. The registration is idempotent: re-imports
(e.g. across tests) re-register the same function objects under the same
names, which the registry treats as a no-op.

Deferred-emitter seam (spec 16). The explorer needs three *reader* skills
named by the extensibility spec — ``dbt_manifest``, ``dlt_schema``,
``memory_read`` — whose backing data is built in later increments (dbt
manifest = Incr 3, dlt schema/lineage = Incr 5, memory = Incr 2). We do
**not** fabricate their data here. Instead :data:`DEFERRED_READER_SKILLS`
records the namespace + provider seam so the catalog/CLI listing can
accommodate them, and each backing spec registers the real ``@skill``
function against its name when its data lands. Listing the seam here keeps
the reader-skill namespace legible (and collision-checked by the registry)
without shipping a stub that returns fake data.
"""

from carve.core.skills.builtin import catalog
from carve.core.skills.registry import default_registry

# name -> the increment / spec that will register the real reader skill.
# Documentation-only: nothing is registered for these here (their data
# isn't built yet); the owning spec adds the `@skill` function later.
DEFERRED_READER_SKILLS: dict[str, str] = {
    "dbt_manifest": "dbt manifest queries (Increment 3)",
    "dlt_schema": "dlt stored resource->table schema / lineage (Increment 5)",
    "memory_read": "the spec-06 memory loader (Increment 2)",
}

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


"""Scenario data proxy — resolves the ACTIVE PROJECT's scenario document.

Scenario data is PER PROJECT/EVENT (the 2026-07-09 unification): the
authored datasets live in utils/scenario_sources/ (hand-rolled modules,
exported to JSON documents by scripts/export_scenario_dataset.py), each
project gets its own document seeded at creation
(utils.project_scenario), and this module keeps the historical consumer
surface — `from utils import scenario; scenario.KG` — working by
resolving attribute access against the ACTIVE project's document
(utils.project_context binds it at every entry point that threads
project_id).

Module-level `__getattr__` (PEP 562) makes the resolution lazy: nothing
is read at import, so importing this module never binds any event's
data. Reading an attribute WITHOUT an active project raises LookupError
— there is no default event, and pre-unification's module-global
Estonia data must never leak into another event.

MUTATION contract: scenario_ingest's OPORD-derived in-memory keys
(OPORD_OVERLAY, KG mutations) and manual_injects' PROJECT_DOCS overlay
land on the ACTIVE project's cached namespace via the helpers below —
never on a shared global.
"""
from __future__ import annotations

from utils.project_context import active_project
from utils.project_scenario import scenario_for

# Derived in-memory keys some consumers set post-ingest (scenario_ingest
# writes OPORD_OVERLAY; getattr-with-default consumers read it). Stored
# per project alongside the loaded document namespace.
_DERIVED_DEFAULTS = {
    "OPORD_OVERLAY": None,
    "SPATIAL_OVERLAY": None,
}


def _ns():
    return scenario_for(active_project())


def __getattr__(name: str):
    if name.startswith("__"):
        raise AttributeError(name)
    ns = _ns()
    if hasattr(ns, name):
        return getattr(ns, name)
    if name in _DERIVED_DEFAULTS:
        return _DERIVED_DEFAULTS[name]
    raise AttributeError(
        f"scenario document for project {active_project()!r} has no key "
        f"{name!r}")


def set_derived(name: str, value) -> None:
    """Set a derived in-memory key (e.g. OPORD_OVERLAY from
    scenario_ingest) on the ACTIVE project's namespace. Process-lifetime
    only, per the ingest contract — a restart wipes it."""
    setattr(_ns(), name, value)

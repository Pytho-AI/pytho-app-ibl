"""Per-project scenario data — the scenario IS project data.

Scenario datasets are hand-rolled (there is no functional scenario
ingest). Pre-unification, each EVENT meant a separate deployment whose
utils/scenario.py was substituted wholesale (the V Corps stack
bind-mounted a surface-identical module over it). Under the
project/event unification, one deployment hosts every event, so the
scenario becomes a PER-PROJECT DOCUMENT — structured the way pytho-vite
structures project data (attached to the project, not to the build):

    /app/data/projects/<project_id>/scenario.json

The document is the JSON serialization of the scenario module surface:
SCENARIO_MONTH_YEAR, KG_OOB_AUTHORED_AT_MS, AO_BBOX, KG, TERRAIN,
OPERATIONS, WEATHER, PROJECT_DOCS (plus optional SPATIAL_OVERLAY).
`scripts/export_scenario_dataset.py` converts a hand-rolled scenario
module into a dataset document; shipped datasets live in
app/data/scenario_datasets/ and CreateProject seeds the chosen one into
the project's document at creation.

`scenario_for(project_id)` loads the project's document into a
namespace exposing the exact attribute surface consumers read today
(`scenario.KG`, `scenario.AO_BBOX`, ...) so the consumer sweep is a
mechanical `scenario.X` -> `scenario_for(state.project_id).X` on the
same call paths that thread project -> map. A project without a
scenario document raises — there is no global fallback module to leak
another event's data from.
"""
from __future__ import annotations

import json
import logging
import os
from types import SimpleNamespace

log = logging.getLogger(__name__)

PROJECT_DATA_DIR = os.environ.get("PYTHO_PROJECT_DATA_DIR", "/app/data/projects")
# NOT under /app/data — that path is a host bind mount on every
# deployment (compose `./data:/app/data`) and would shadow the shipped
# documents baked into the image.
DATASET_DIR = os.environ.get(
    "PYTHO_SCENARIO_DATASET_DIR", "/app/scenario_datasets")

# The scenario module surface — every key a dataset document carries.
# SPATIAL_OVERLAY is optional (consumers getattr it).
SCENARIO_KEYS = (
    "SCENARIO_MONTH_YEAR",
    "KG_OOB_AUTHORED_AT_MS",
    "AO_BBOX",
    "KG",
    "TERRAIN",
    "OPERATIONS",
    "WEATHER",
    "PROJECT_DOCS",
    "SPATIAL_OVERLAY",
    # Fused resolved-OOB payload (named_units with composition rollups
    # + kg ids) — present when the event has one (Estonia/IDX 26 folds
    # in the last live fusion snapshot at migration); absent events run
    # on the KG floor.
    "RESOLVED_OOB",
)

# (project_id) -> {"mtime": float, "overlay_rev": int, "data": SimpleNamespace}
_CACHE: dict = {}

# Manual-injects doc overlay — analyst-dropped docx files overlay the
# PROJECT_DOCS catalog of EVERY project (the injects folder is a venue
# surface, not a per-event one). utils.manual_injects owns the contents;
# scenario_for applies it at load. Bumping the rev invalidates every
# project's cached namespace.
PROJECT_DOCS_OVERLAY: dict = {}
_OVERLAY_REV = 0


def bump_docs_overlay_rev() -> None:
    global _OVERLAY_REV
    _OVERLAY_REV += 1
    _CACHE.clear()


def _scenario_path(project_id: str) -> str:
    pid = str(project_id or "").strip()
    if not pid:
        raise ValueError("scenario_for requires a project id — thread the "
                         "PROJECT through this caller")
    return os.path.join(PROJECT_DATA_DIR, pid, "scenario.json")


def scenario_for(project_id: str) -> SimpleNamespace:
    """The project's scenario data, as a namespace with the scenario
    module surface. mtime-cached per project. Raises FileNotFoundError
    when the project has no scenario document — a project without
    scenario data must fail loudly, never read another event's."""
    path = _scenario_path(project_id)
    st = os.stat(path)  # FileNotFoundError propagates
    cached = _CACHE.get(project_id)
    if (cached and cached["mtime"] == st.st_mtime
            and cached["overlay_rev"] == _OVERLAY_REV):
        return cached["data"]
    with open(path) as f:
        doc = json.load(f)
    ns = SimpleNamespace(**{k: doc[k] for k in SCENARIO_KEYS if k in doc})
    if PROJECT_DOCS_OVERLAY:
        docs = dict(getattr(ns, "PROJECT_DOCS", {}) or {})
        docs.update(PROJECT_DOCS_OVERLAY)
        ns.PROJECT_DOCS = docs
    _CACHE[project_id] = {"mtime": st.st_mtime, "overlay_rev": _OVERLAY_REV,
                           "data": ns}
    return ns


def list_datasets() -> list:
    """Shipped scenario dataset slugs (files under DATASET_DIR)."""
    try:
        return sorted(
            f[:-5] for f in os.listdir(DATASET_DIR) if f.endswith(".json"))
    except FileNotFoundError:
        return []


def seed_project_scenario(project_id: str, dataset_slug: str) -> str:
    """Copy a shipped dataset document into the project's scenario.json.
    Called by the project-creation bootstrap BEFORE any producer runs.
    Returns the written path. Raises on unknown dataset — creating an
    event without scenario data is an error, not a default."""
    src = os.path.join(DATASET_DIR, f"{dataset_slug}.json")
    if not os.path.exists(src):
        raise ValueError(
            f"unknown scenario dataset {dataset_slug!r} — available: "
            f"{list_datasets()}")
    dst = _scenario_path(project_id)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(src) as f:
        doc = json.load(f)
    with open(dst, "w") as f:
        json.dump(doc, f, default=str)
    _CACHE.pop(project_id, None)
    log.info("seeded scenario dataset %s -> project %s", dataset_slug, project_id)
    return dst

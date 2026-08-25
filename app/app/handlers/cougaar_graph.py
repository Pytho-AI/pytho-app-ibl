"""Turn the COUGAAR tool results a product pulled into a node/edge graph.

Each generated product saves the raw COUGAAR responses it fetched under
uploads/<owner>/<artifact_id>/tool_results/<tool>.json (written by the DAG
executor). This module reads those files and flattens the entities into a
generic {nodes, edges, stats} payload the Graph Viewer renders.

Nodes are entities (units, targets, intel observations, control measures);
edges are the relationships carried in the data (a unit's parent, a target's
linked unit, an observation's observed entity). Live mode feeds the SAME
transform with freshly-fetched tool output.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from handlers.product import UPLOAD_DIR

# Per-type cap so a product that pulled 13k units doesn't try to ship/render
# them all. The response reports total-vs-shown so the UI can say so.
DEFAULT_LIMIT = 500

# tool name -> (list keys to pull entities from, node type)
_TOOL_ENTITIES: dict[str, tuple[list[str], str]] = {
    "get_oob": (["units"], "unit"),
    "list_targets": (["targets"], "target"),
    "get_recent_intel": (["reports", "engagements", "imagery", "osint"], "observation"),
    "get_spatial_overlay": (["control_measures"], "control_measure"),
    "get_civil": (["civil_considerations", "considerations", "items"], "civil"),
}


def _artifact_dir(artifact_id: str) -> Path | None:
    """Find uploads/<owner>/<artifact_id>/ that has a tool_results dir."""
    base = Path(UPLOAD_DIR)
    for p in base.glob(f"*/{artifact_id}"):
        if (p / "tool_results").is_dir():
            return p
    return None


def list_products(limit: int = 300) -> list[dict]:
    """Every artifact that saved COUGAAR tool_results, newest first."""
    base = Path(UPLOAD_DIR)
    out: list[dict] = []
    for tr in base.glob("*/*/tool_results"):
        art_dir = tr.parent
        tools = sorted(f.stem for f in tr.glob("*.json"))
        # Only surface artifacts that actually pulled COUGAAR entity tools.
        if not any(t in _TOOL_ENTITIES for t in tools):
            continue
        product_type = ""
        generated_at = ""
        ss = art_dir / "schema_state.json"
        if ss.exists():
            try:
                d = json.loads(ss.read_text())
                product_type = d.get("product_type") or ""
                generated_at = d.get("generated_at") or ""
            except Exception:
                pass
        out.append({
            "artifactId": art_dir.name,
            "productType": product_type,
            "generatedAt": generated_at,
            "tools": tools,
        })
    out.sort(key=lambda r: (r["generatedAt"], r["artifactId"]), reverse=True)
    return out[:limit]


def _scalar_attrs(entity: dict) -> dict:
    """Keep the small scalar fields for the detail panel; drop big/raw blobs."""
    out = {}
    for k, v in entity.items():
        if k.startswith("_") or k in ("raw_records", "source_kbids", "composition"):
            continue
        if isinstance(v, (str, int, float, bool)) and v not in ("", None):
            out[k] = v
        elif k == "location" and isinstance(v, dict):
            out[k] = v
    return out


def _label(entity: dict, fallback: str) -> str:
    for k in ("name", "entity_name", "designator", "ref_id", "id"):
        v = entity.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return fallback


def build_graph(tool_outputs: dict[str, Any], limit: int = DEFAULT_LIMIT,
                types: set[str] | None = None) -> dict:
    """Flatten {tool_name: tool_response_dict} into {nodes, edges, stats}.

    Shared by snapshot (read from tool_results) and live (freshly fetched).
    """
    nodes: list[dict] = []
    edges: list[dict] = []
    stats: dict[str, dict] = {}
    seen_node_ids: set[str] = set()
    # indexes for edge resolution: natural key -> node id
    by_id: dict[str, str] = {}
    by_name: dict[str, str] = {}
    by_be: dict[str, str] = {}
    # deferred relationships, resolved after all nodes exist
    pending: list[tuple[str, str, str]] = []  # (src_node_id, raw_target, kind)

    def add_node(nid: str, ntype: str, label: str, subtype: str | None,
                 entity: dict) -> None:
        if not nid or nid in seen_node_ids:
            return
        seen_node_ids.add(nid)
        nodes.append({
            "id": nid,
            "label": label,
            "type": ntype,
            "subtype": subtype,
            "attrs": _scalar_attrs(entity),
        })

    for tool, data in tool_outputs.items():
        if not isinstance(data, dict) or tool not in _TOOL_ENTITIES:
            continue  # only COUGAAR entity tools — skip render_map, list_project_docs, etc.
        keys, ntype = _TOOL_ENTITIES[tool]
        if types is not None and ntype not in types:
            stats[tool] = {"type": ntype, "total": 0, "shown": 0, "skipped": True}
            continue

        total = 0
        shown = 0
        for key in keys:
            items = data.get(key)
            if not isinstance(items, list):
                continue
            total += len(items)
            for i, e in enumerate(items):
                if not isinstance(e, dict):
                    continue
                if shown >= limit:
                    continue
                ent_id = str(e.get("id") or e.get("observation_id")
                             or e.get("uuid") or f"{tool}-{i}")
                nid = f"{ntype}:{ent_id}"
                subtype = e.get("source_int") if ntype == "observation" else None
                add_node(nid, ntype, _label(e, ent_id), subtype, e)
                shown += 1
                # index for edge resolution
                by_id.setdefault(str(ent_id), nid)
                nm = (e.get("name") or "").strip().lower()
                if nm:
                    by_name.setdefault(nm, nid)
                be = (e.get("be_number") or "").strip()
                if be:
                    by_be.setdefault(be, nid)
                # collect relationships
                if ntype == "unit" and e.get("parent"):
                    pending.append((nid, str(e["parent"]), "parent"))
                if ntype == "target":
                    if e.get("linked_unit"):
                        pending.append((nid, str(e["linked_unit"]), "linked_unit"))
                    if e.get("parent_target_system"):
                        pending.append((nid, str(e["parent_target_system"]), "parent_system"))
                if ntype == "observation" and e.get("entity_key"):
                    pending.append((nid, str(e["entity_key"]), "observes"))
        stats[tool] = {"type": ntype, "total": total, "shown": shown}

    # resolve relationships against whatever nodes made the cap
    seen_edges: set[str] = set()
    for src, raw, kind in pending:
        key = raw.strip()
        tgt = (by_id.get(key) or by_be.get(key) or by_name.get(key.lower()))
        if not tgt or tgt == src:
            continue
        eid = f"{src}->{tgt}:{kind}"
        if eid in seen_edges:
            continue
        seen_edges.add(eid)
        edges.append({"id": eid, "source": src, "target": tgt, "kind": kind})

    return {"nodes": nodes, "edges": edges, "stats": stats}


def build_from_artifact(artifact_id: str, limit: int = DEFAULT_LIMIT,
                        types: set[str] | None = None) -> dict | None:
    """Snapshot graph for a product, read from its saved tool_results."""
    art_dir = _artifact_dir(artifact_id)
    if art_dir is None:
        return None
    tool_outputs: dict[str, Any] = {}
    for f in sorted((art_dir / "tool_results").glob("*.json")):
        try:
            tool_outputs[f.stem] = json.loads(f.read_text())
        except Exception:
            continue
    graph = build_graph(tool_outputs, limit=limit, types=types)
    graph["artifactId"] = artifact_id
    graph["source"] = "snapshot"
    return graph

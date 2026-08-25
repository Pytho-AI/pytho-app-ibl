"""Schema-to-DAG parser (runtime copy).

In-container twin of scratch/ibl/scratch_dag/dag_parser.py. The scratch copy
stays around for offline DAG-shape experiments + mermaid rendering; this copy
is what handlers/intel_agent.py and runtime/dag_executor.py import inside
docker-api.

Reads a Pytho product schema (the SPEC dict from
templates/products/<name>.py) and emits a list of Node objects with explicit
depends_on edges. The DAG is derived from data already in the schema:

  - consumes_artifacts          -> artifact.<name> nodes
  - row_source                  -> a single cougaar retrieval node (rows depend
                                   on it)
  - header_fields[*].source     -> source-token vocabulary parser
  - row_fields[*].source        -> same vocabulary
  - header_fields[*].depends_on    -> explicit list of cougaar.<tool> /
                                       artifact.<Name> entries
  - row_fields[*].depends_on       -> cougaar.<tool> / artifact.<Name> /
                                       sibling row_field name
  - paragraph_fields[*].depends_on -> cougaar.<tool> / artifact.<Name> /
                                       sibling paragraph name
  - paragraph_fields[*].item_subfields[*].depends_on -> per-subfield
                                       cougaar.<tool> / artifact.<Name>
                                       attachments (informational — surfaces
                                       in paragraph_row_field nodes for DAG
                                       inspection + mermaid)

Output: list[Node].

Node id convention:
  - cougaar.<tool>                  e.g. cougaar.get_oob
  - artifact.<ProductName>          e.g. artifact.WEM
  - <Product>.header.<field>
  - <Product>.row.<row_id>.<field>  (rows are emitted as one node per row_field
                                     when no specific row identity is known;
                                     see notes on row-shape products)
  - <Product>.paragraph.<name>
  - <Product>.submit
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


# Known cougaar tool names. Parser looks for these as bare identifiers in
# guidance prose, with or without a trailing parenthesis. Keep in sync with
# pytho-app/app/app/handlers/intel_agent.py tool_* functions.
KNOWN_COUGAAR_TOOLS: set[str] = {
    "get_oob",
    "get_oob_red",
    "list_targets",
    "get_target",
    "get_target_by_id",
    "get_target_set",
    "get_recent_intel",
    "get_terrain",
    "get_environmental",
    "get_civil",
    "get_phases",
    "get_project_doc",
    "list_project_docs",
    "get_spatial_overlay",
    "render_map",
}

# Shorthand aliases the schema writers use in guidance prose. Maps a bare word
# to the cougaar tool it implies. Only fires inside an "+ X + Y" list context
# where the writer is enumerating data feeds — not as a general
# substring-match (otherwise "civil considerations" prose would always match).
# These are observed in real schemas; add new ones as they appear.
COUGAAR_GUIDANCE_ALIASES: dict[str, str] = {
    "civil":   "get_civil",
    "oob":     "get_oob",
    "terrain": "get_terrain",
    "phases":  "get_phases",
}

# row_source token -> cougaar retrieval tool. row_source declares the upstream
# data the rows iterate over. Only the kg_ tokens that map cleanly to a
# cougaar tool are listed; others (factor_list, etc.) are non-cougaar and have
# no upstream retrieval node.
ROW_SOURCE_TO_COUGAAR: dict[str, str] = {
    "kg_targets_active":    "list_targets",
    "kg_protected_entities": "list_targets",   # NSL/RTL filter list_targets server-side
    "kg_units_active":      "get_oob",
    "kg_red_oob_active": "get_oob_red",  # HVTL-style RED-only OOB; dispatched in dag_executor.py with force_type=RED only (no BLUE concat)
}

# kg:<field> source tokens on row_fields. The kg: prefix means "this column is
# populated from the row_source retrieval payload field" — it shares the
# product's row_source dep, not a new cougaar call. The vocabulary is open
# (kg:name, kg:doi, kg:location_mgrs, ...) — the parser does not enumerate
# them; it just treats `kg:*` and `kg_id` as "use the row_source retrieval".


@dataclass
class Node:
    """A DAG node. id is unique within a single product DAG.

    type ∈ {
      "cougaar_retrieval",
      "artifact_dep",
      "header_field",
      "row_field",
      "paragraph_field",
      "paragraph_row_field",  # informational — per-item_subfield child of a
                              # paragraph_field; does NOT trigger an LLM
                              # dispatch (the paragraph's emit covers the
                              # whole list_of_dicts). Carries the
                              # subfield→cougaar tool inheritance for DAG
                              # inspection and mermaid rendering.
      "submit",
    }
    """
    id: str
    type: str
    depends_on: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


# -----------------------------------------------------------------------------
# Source-token vocabulary parser
# -----------------------------------------------------------------------------

def parse_source_token(src: str, consumes: list[str]) -> list[str]:
    """Map one `source: "..."` string to the dep node ids it implies.

    Handles all 7 forms documented in PARALLEL_DAG_REDESIGN.md + observed in
    the schemas:

      "session"           -> []                        (no upstream node; the
                                                        operations-expert hands
                                                        this in)
      "analytical"        -> []                        (LLM-emit only — the
                                                        deps come from prose
                                                        guidance, not the
                                                        source token)
      "constant"          -> []                        (literal value)
      "default"           -> []                        (literal default —
                                                        synonym of constant)
      "kg_id"             -> []                        (row identity from the
                                                        product's row_source —
                                                        the row_source dep is
                                                        attached at the
                                                        row-field level, not
                                                        here)
      "kg:<field>"        -> []                        (same — column-of-row
                                                        from row_source)
      "ref:<Name>"        -> ["artifact.<Name>"]       (upstream artifact)
      "header_alias:<f>"  -> []                        (alias of another header
                                                        field in the SAME
                                                        product — handled at
                                                        the header-field level
                                                        by linking sibling
                                                        header nodes)

    For ref:<Name>, `consumes` is used as a sanity check — if the ref token
    points at an artifact that is NOT in consumes_artifacts, we still emit the
    edge but flag it in metadata (the caller decides whether to warn).
    """
    if not src or not isinstance(src, str):
        return []
    src = src.strip()
    if src in ("session", "analytical", "constant", "default", "kg_id"):
        return []
    if src.startswith("kg:"):
        return []
    if src.startswith("ref:"):
        ref_name = src[len("ref:"):].strip()
        return [f"artifact.{ref_name}"]
    if src.startswith("lookup:"):
        # lookup:<Artifact>.<key>[target_id].<field>  OR  lookup:<Artifact>.<field>
        # The mechanical resolver lives in dag_executor._resolve_lookup_source.
        # Parser's only job: extract the artifact name so the dep edge wires
        # this field to the upstream artifact_dep payload. Without this branch
        # the parser falls through to __unknown_source__ and the field's only
        # dep edge becomes a sentinel (resolved_deps never carries
        # artifact.<Artifact>), so the resolver sees an empty payload and
        # always returns "" — every TIDAT lookup field publishes blank.
        body = src[len("lookup:"):].strip()
        artifact_name = body.split(".", 1)[0].strip() if body else ""
        if artifact_name:
            return [f"artifact.{artifact_name}"]
        return []
    if src.startswith("header_alias:"):
        # caller resolves the sibling-header edge — we just signal it
        alias = src[len("header_alias:"):].strip()
        return [f"__header_alias__:{alias}"]
    if src.startswith("tool:") or src == "session.user" or src == "computed":
        # Executor-resolved source tokens with no upstream artifact dep:
        #   tool:<name>   → executor calls the named cougaar/intel-agent tool
        #   session.user  → header-context plumbing
        #   computed      → schema-declared post-merge backfill
        # These were silently falling through to __unknown_source__, polluting
        # DAG warnings with sentinels for valid sources. No upstream edge
        # needed — return [].
        return []
    if src.startswith("derive:") or src.startswith("derive_from_dp_catalog:"):
        # derive:* sources are catalog id-pickers backed by scenario.TERRAIN
        # — no upstream artifact dep, executor pulls from the in-memory
        # catalog. Same as the executor-resolved tokens above.
        return []
    # Unknown source token — surface it as a metadata-only edge so the caller
    # can log; do NOT silently drop.
    return [f"__unknown_source__:{src}"]


# -----------------------------------------------------------------------------
# Guidance-prose dependency extraction
# -----------------------------------------------------------------------------

# Cougaar tool detection: tool name followed optionally by ( or end-of-word.
# Word boundary handling so "list_targets" matches but "list_targetss" does
# not.
_COUGAAR_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in sorted(KNOWN_COUGAAR_TOOLS,
                                                    key=len, reverse=True)) + r")\b"
)


def extract_deps_from_guidance(
    text: str,
    consumes: list[str],
    known_tools: set[str],
    known_products: set[str],
    self_product: str | None = None,
) -> list[str]:
    """Scan free-prose guidance for cougaar tool calls and artifact mentions.

    Returns a deduplicated list of dep node ids. Does NOT include the
    sibling-paragraph or current-product self references — those come from
    `depends_on` (when present in the schema) or downstream consolidation.

    Strategy:
      - regex over known_tools (substring match with \\b boundaries)
      - guidance-alias map for bare words like "civil" -> get_civil, applied
        only when the word appears in a "feed list" context (preceded or
        followed by ' + ' / ',' / 'and') to avoid false positives on prose.
      - regex over known_products PLUS consumes (exact match, case-sensitive
        for acronyms, case-insensitive for multi-word product names). Acronyms
        must be >= 3 chars to avoid "CA" / "EW" false positives.
      - drops self-references (a paragraph in INTSUM mentioning "INTSUM"
        should not depend on artifact.INTSUM).
      - returns ['cougaar.<tool>', ...] + ['artifact.<Name>', ...]
    """
    deps: list[str] = []
    if not text:
        return deps

    # Cougaar tools (canonical names)
    for match in _COUGAAR_PATTERN.finditer(text):
        tool = match.group(1)
        if tool in known_tools:
            dep = f"cougaar.{tool}"
            if dep not in deps:
                deps.append(dep)

    # Cougaar guidance aliases — fire only when the alias appears inside a
    # feed-list context, e.g. "+ civil +", "civil,", "and civil ". This keeps
    # generic prose like "civil considerations" from triggering a dep.
    for alias, tool in COUGAAR_GUIDANCE_ALIASES.items():
        # Look for the alias as its own word, preceded or followed by one of
        # the list connectives: ' + ', ',', '/'.
        pat = re.compile(
            r"(?:^|[+,/]\s*|\b(?:and|or)\s+)"
            + re.escape(alias)
            + r"(?=\s*(?:[+,/]|\s+(?:and|or)\b)|$)",
            re.IGNORECASE,
        )
        if pat.search(text):
            dep = f"cougaar.{tool}"
            if dep not in deps:
                deps.append(dep)

    # Artifact mentions. We accept any name in `consumes` OR `known_products`.
    candidates = set(consumes) | set(known_products)
    if self_product:
        candidates.discard(self_product)
    # Sort longest-first to avoid matching "TEM" inside "SITEMP".
    for name in sorted(candidates, key=len, reverse=True):
        if not name:
            continue
        # Acronyms (all caps, no spaces) need word-boundary case-sensitive
        # match AND must be >=3 chars to avoid "CA" / "EW" / "OE" hits.
        # Multi-word names like "Threat Overlay" use case-insensitive phrase
        # match (those are unambiguous).
        if name.isupper() and " " not in name:
            if len(name) < 3:
                continue
            # Allow plural / possessive trailing 's', "'s", or "(s)" — schemas
            # routinely write "KLIEGLIGHTs" / "INTSUMs" / "TACELINT(s)" / etc.
            # in free prose; word-boundary-only matching missed these.
            pat = re.compile(r"\b" + re.escape(name) + r"(?:'s|\(s\)|s)?\b")
            if pat.search(text):
                dep = f"artifact.{name}"
                if dep not in deps:
                    deps.append(dep)
        else:
            pat = re.compile(r"\b" + re.escape(name) + r"(?:'s|\(s\)|s)?\b", re.IGNORECASE)
            if pat.search(text):
                dep = f"artifact.{name}"
                if dep not in deps:
                    deps.append(dep)
    return deps


# -----------------------------------------------------------------------------
# Main parser entry
# -----------------------------------------------------------------------------

def parse_dag_from_schema(
    product_type: str,
    schema: dict,
    known_products: set[str] | None = None,
) -> list[Node]:
    """Build the per-product DAG from a SPEC dict.

    `known_products` is the set of all product registry keys — used to
    disambiguate artifact mentions in guidance prose. If None, only
    consumes_artifacts is used (no cross-product guidance scan).

    Returns a list of Node objects. The list is in deterministic emit order:
    cougaar retrievals → artifact deps → header fields → row fields →
    paragraph fields → submit. The orchestrator will topologically re-sort.
    """
    if known_products is None:
        known_products = set()

    nodes: list[Node] = []
    seen_ids: set[str] = set()

    def add(node: Node) -> None:
        if node.id in seen_ids:
            # Merge depends_on if a duplicate id (e.g. same cougaar tool
            # referenced from two different paragraph guidances).
            for existing in nodes:
                if existing.id == node.id:
                    for d in node.depends_on:
                        if d not in existing.depends_on:
                            existing.depends_on.append(d)
                    existing.metadata.setdefault("merged_from", []).append(
                        node.metadata,
                    )
                    return
        nodes.append(node)
        seen_ids.add(node.id)

    consumes: list[str] = list(schema.get("consumes_artifacts") or [])

    # 1. Artifact deps. One node per consumes entry, plus one for every
    #    ref:<X> token observed in header_fields / row_fields sources where
    #    the target is NOT in consumes_artifacts (this surfaces schema gaps —
    #    e.g. a schema declaring ref:LegacyName in header_fields even
    #    though consumes_artifacts says "RenamedProduct" — a rename
    #    artifact this surfaces as an unresolved orphan).
    # These nodes have no upstream deps in THIS product's DAG — they are
    # roots from this product's POV; the orchestrator wires them to the
    # upstream product's submit node at super-DAG merge time.
    ref_artifacts_from_sources: set[str] = set()
    for fname, fspec in (schema.get("header_fields") or {}).items():
        if isinstance(fspec, dict):
            src = fspec.get("source", "")
            if isinstance(src, str) and src.startswith("ref:"):
                ref_artifacts_from_sources.add(src[len("ref:"):].strip())
    for fname, fspec in (schema.get("row_fields") or {}).items():
        if isinstance(fspec, dict):
            src = fspec.get("source", "")
            if isinstance(src, str) and src.startswith("ref:"):
                ref_artifacts_from_sources.add(src[len("ref:"):].strip())
    all_artifacts = list(consumes)
    for art in ref_artifacts_from_sources:
        if art not in all_artifacts:
            all_artifacts.append(art)
    for art in all_artifacts:
        src_label = "consumes_artifacts" if art in consumes else "ref_source_orphan"
        add(Node(
            id=f"artifact.{art}",
            type="artifact_dep",
            depends_on=[],
            metadata={"artifact_name": art, "source": src_label},
        ))

    # 2. row_source -> cougaar retrieval (if it maps to a tool)
    row_source = schema.get("row_source")
    row_source_cougaar_dep: str | None = None
    if row_source and row_source in ROW_SOURCE_TO_COUGAAR:
        tool = ROW_SOURCE_TO_COUGAAR[row_source]
        cid = f"cougaar.{tool}"
        add(Node(
            id=cid,
            type="cougaar_retrieval",
            depends_on=[],
            metadata={"tool": tool, "source": "row_source", "row_source": row_source},
        ))
        row_source_cougaar_dep = cid

    # 3. Header fields
    header_fields: dict = schema.get("header_fields") or {}
    # Track header field ids in this product so header_alias:<f> can resolve.
    header_field_ids: dict[str, str] = {}
    for fname, fspec in header_fields.items():
        node_id = f"{product_type}.header.{fname}"
        header_field_ids[fname] = node_id

    for fname, fspec in header_fields.items():
        if not isinstance(fspec, dict):
            continue
        node_id = header_field_ids[fname]
        src = fspec.get("source", "")
        deps = parse_source_token(src, consumes)
        # Resolve header_alias edges to actual sibling node ids
        resolved_deps: list[str] = []
        for d in deps:
            if d.startswith("__header_alias__:"):
                alias = d.split(":", 1)[1]
                if alias in header_field_ids:
                    resolved_deps.append(header_field_ids[alias])
                else:
                    resolved_deps.append(f"__unresolved_header_alias__:{alias}")
            else:
                resolved_deps.append(d)
        guidance = fspec.get("guidance", "") or ""
        # Explicit `depends_on` on header_field — accepts cougaar.<tool> /
        # artifact.<Name> entries. Mirrors the paragraph_field pattern; the
        # prior regex-over-guidance fallback was removed once schemas were
        # migrated to declare these deps explicitly (2026-05-26).
        for dep_entry in fspec.get("depends_on", []) or []:
            if not isinstance(dep_entry, str):
                continue
            if dep_entry.startswith("cougaar."):
                tool = dep_entry.split(".", 1)[1]
                if dep_entry not in seen_ids:
                    add(Node(
                        id=dep_entry,
                        type="cougaar_retrieval",
                        depends_on=[],
                        metadata={"tool": tool,
                                   "source": f"depends_on:{product_type}.header.{fname}"},
                    ))
                if dep_entry not in resolved_deps:
                    resolved_deps.append(dep_entry)
            elif dep_entry.startswith("artifact."):
                art = dep_entry.split(".", 1)[1]
                if dep_entry not in seen_ids:
                    add(Node(
                        id=dep_entry,
                        type="artifact_dep",
                        depends_on=[],
                        metadata={"artifact_name": art,
                                   "source": f"depends_on:{product_type}.header.{fname}"},
                    ))
                if dep_entry not in resolved_deps:
                    resolved_deps.append(dep_entry)
            else:
                resolved_deps.append(f"__unresolved_header_dep__:{dep_entry}")
        add(Node(
            id=node_id,
            type="header_field",
            depends_on=resolved_deps,
            metadata={
                "field_name": fname,
                "source_token": src,
                "required": fspec.get("required", False),
                "guidance_brief": (guidance[:80] + "...") if len(guidance) > 80 else guidance,
            },
        ))

    # 4. Row fields. We emit one node per row_field with the row_source
    #    cougaar retrieval as the dep (plus any ref:<X> deps from source).
    #    We do NOT enumerate per-row-instance nodes — that requires runtime
    #    list_targets results. The DAG records the row-field shape; the
    #    orchestrator fans out at run time.
    row_fields: dict = schema.get("row_fields") or {}
    for fname, fspec in row_fields.items():
        if not isinstance(fspec, dict):
            continue
        node_id = f"{product_type}.row_field.{fname}"
        src = fspec.get("source", "")
        deps = parse_source_token(src, consumes)
        # Every row_field implicitly depends on the row_source retrieval (when
        # one exists), because the row iteration set comes from there.
        if row_source_cougaar_dep and row_source_cougaar_dep not in deps:
            deps.append(row_source_cougaar_dep)
        # Strip header_alias / unknown_source sentinels at row level (they
        # don't apply here, but defensively scrub).
        deps = [d for d in deps if not d.startswith("__")]
        guidance = fspec.get("guidance", "") or ""
        # Explicit `depends_on` on the row_field spec — unified namespace:
        #   cougaar.<tool>      → ensure cougaar_retrieval node + add as dep
        #   artifact.<Name>     → ensure artifact_dep node + add as dep
        #   <sibling_name>      → sibling row_field name (e.g. HVTL/HPTL's
        #                          published Justification waits on its
        #                          workpaper inputs so the LLM call producing
        #                          the distilled sentence sees the analytical
        #                          reasoning already written by the upstream
        #                          wave)
        # Replaced the prior regex-over-guidance fallback; schemas declare
        # these deps explicitly (2026-05-26).
        for dep_entry in (fspec.get("depends_on") or []):
            if not isinstance(dep_entry, str) or not dep_entry:
                continue
            if dep_entry.startswith("cougaar."):
                tool = dep_entry.split(".", 1)[1]
                if dep_entry not in seen_ids:
                    add(Node(
                        id=dep_entry,
                        type="cougaar_retrieval",
                        depends_on=[],
                        metadata={"tool": tool,
                                   "source": f"depends_on:{product_type}.row_field.{fname}"},
                    ))
                if dep_entry not in deps:
                    deps.append(dep_entry)
            elif dep_entry.startswith("artifact."):
                art = dep_entry.split(".", 1)[1]
                if dep_entry not in seen_ids:
                    add(Node(
                        id=dep_entry,
                        type="artifact_dep",
                        depends_on=[],
                        metadata={"artifact_name": art,
                                   "source": f"depends_on:{product_type}.row_field.{fname}"},
                    ))
                if dep_entry not in deps:
                    deps.append(dep_entry)
            elif dep_entry in row_fields:
                sibling_id = f"{product_type}.row_field.{dep_entry}"
                if sibling_id not in deps:
                    deps.append(sibling_id)
            elif dep_entry in (schema.get("header_fields") or {}):
                # Cross-shape dep — row_field waits on a header_field.
                # HVTL uses this so its analytical row_fields wait for the
                # writer-emitted selected_hvt_kg_ids list to land BEFORE
                # iterating rows; without this branch the bare field name
                # silently fell into the __unresolved sentinel and the
                # ordering constraint was a no-op. Header field node id
                # format is `{product_type}.header.{fname}` (not
                # `.header_field.` — that's the *type* tag, not the id
                # prefix; see line 378 above).
                header_id = f"{product_type}.header.{dep_entry}"
                if header_id not in deps:
                    deps.append(header_id)
            else:
                deps.append(f"__unresolved_row_field_dep__:{dep_entry}")
        add(Node(
            id=node_id,
            type="row_field",
            depends_on=deps,
            metadata={
                "field_name": fname,
                "source_token": src,
                "required": fspec.get("required", False),
                "view": fspec.get("view"),
                "guidance_brief": (guidance[:80] + "...") if len(guidance) > 80 else guidance,
                # Same batching marker the paragraph_field branch carries
                # (see line ~722). Without this, the DAG executor's wave
                # grouping reads metadata.batch_group as "" for row_field
                # nodes and skips co-firing — so HVTL's six analytical
                # workpaper row_fields, all tagged batch_group=hvt_analytical
                # in the schema, fired as six serial LLM calls instead of
                # one batched call. The earlier dag_executor change that
                # allowed batch_group for row_field nodes was load-bearing
                # only if the parser PROPAGATES the tag here.
                "batch_group": fspec.get("batch_group") or "",
            },
        ))

    # 5. Paragraph fields
    paragraph_fields = schema.get("paragraph_fields") or []
    # First pass: register all paragraph node ids so depends_on between
    # siblings can resolve.
    paragraph_ids: dict[str, str] = {}
    for pf in paragraph_fields:
        if not isinstance(pf, dict):
            continue
        pname = pf.get("name")
        if not pname:
            continue
        paragraph_ids[pname] = f"{product_type}.paragraph.{pname}"

    def _walk_item_subfields(item_subfields: list,
                              parent_path: str,
                              parent_inherited_deps: list[str],
                              ) -> tuple[list[tuple[str, str, str, list[str]]], list[str]]:
        """Recursively walk item_subfields. Returns:
          - row_specs: list of (subfield_node_id, subfield_name, subfield_guidance,
                                aggregated_deps_for_this_subfield)
          - all_deps_seen_in_tree: union of every dep extracted from any
                                    subfield (and its children) in this subtree

        Both top-level item_subfields and nested ones (e.g. WEM.operations.
        parameters.current_forecast_value) are visited. Each subfield's own
        deps come from the subfield's explicit `depends_on` list (entries are
        cougaar.<tool> / artifact.<Name>). Aggregated deps = parent_inherited
        + sf own + every dep its descendants emit. Children inherit parent
        deps so a nested forecast row carries the same cougaar.get_environmental
        attachment its enclosing parameters block does.

        The prior regex-over-guidance fallback was removed once schemas were
        migrated to declare these deps explicitly (2026-05-26).
        """
        row_specs: list[tuple[str, str, str, list[str]]] = []
        tree_deps: list[str] = list(parent_inherited_deps)
        if not item_subfields:
            return row_specs, tree_deps
        for sf in item_subfields:
            if not isinstance(sf, dict):
                continue
            sf_name = sf.get("name") or ""
            if not sf_name:
                continue
            sf_guidance = sf.get("guidance", "") or ""
            # Explicit depends_on — cougaar.<tool> / artifact.<Name>. Anything
            # else is surfaced as an unresolved-dep sentinel for the validator
            # (subfield siblings have no cross-link semantics today).
            sf_own_deps: list[str] = []
            for dep_entry in (sf.get("depends_on") or []):
                if not isinstance(dep_entry, str) or not dep_entry:
                    continue
                if dep_entry.startswith("cougaar."):
                    tool = dep_entry.split(".", 1)[1]
                    if dep_entry not in seen_ids:
                        add(Node(
                            id=dep_entry,
                            type="cougaar_retrieval",
                            depends_on=[],
                            metadata={"tool": tool,
                                       "source": f"depends_on:{parent_path}.row.{sf_name}"},
                        ))
                    sf_own_deps.append(dep_entry)
                elif dep_entry.startswith("artifact."):
                    art = dep_entry.split(".", 1)[1]
                    if dep_entry not in seen_ids:
                        add(Node(
                            id=dep_entry,
                            type="artifact_dep",
                            depends_on=[],
                            metadata={"artifact_name": art,
                                       "source": f"depends_on:{parent_path}.row.{sf_name}"},
                        ))
                    sf_own_deps.append(dep_entry)
                else:
                    sf_own_deps.append(f"__unresolved_item_subfield_dep__:{dep_entry}")
            # Aggregate: inherited + this subfield's own deps.
            sf_aggregated = list(parent_inherited_deps)
            for d in sf_own_deps:
                if d not in sf_aggregated:
                    sf_aggregated.append(d)
            # Recurse into nested item_subfields.
            child_specs, child_tree_deps = _walk_item_subfields(
                sf.get("item_subfields") or [],
                parent_path + "." + sf_name,
                sf_aggregated,
            )
            # Children's deps roll up into this subfield (so the parameters
            # subfield sees deps that current_forecast_value contributed).
            for d in child_tree_deps:
                if d not in sf_aggregated:
                    sf_aggregated.append(d)
            sf_node_id = f"{parent_path}.row.{sf_name}"
            row_specs.append((sf_node_id, sf_name, sf_guidance, sf_aggregated))
            row_specs.extend(child_specs)
            for d in sf_aggregated:
                if d not in tree_deps:
                    tree_deps.append(d)
        return row_specs, tree_deps

    for pf in paragraph_fields:
        if not isinstance(pf, dict):
            continue
        pname = pf.get("name")
        if not pname:
            continue
        node_id = paragraph_ids[pname]
        para_guidance = pf.get("guidance", "") or ""
        # Recursively walk item_subfields purely to register the nested
        # row_field nodes (per-subfield informational nodes). NO dep
        # extraction from prose anymore — the canonical dep mechanism is
        # the explicit depends_on list, scanned below.
        row_specs, _ = _walk_item_subfields(
            pf.get("item_subfields") or [],
            node_id,
            [],
        )
        deps: list[str] = []
        # Explicit depends_on — single source of truth. Unified namespace:
        #   cougaar.<tool>      → ensure cougaar_retrieval node + add as dep
        #   artifact.<Name>     → ensure artifact_dep node + add as dep
        #   <sibling_name>      → resolve to sibling paragraph id
        # Replaced the prior regex-over-guidance fallback; schemas must
        # declare deps here. Removed 2026-05-26 after schema migration —
        # see commit 8549fd7 for the schema-side migration.
        for dep_entry in pf.get("depends_on", []) or []:
            if not isinstance(dep_entry, str):
                continue
            if dep_entry.startswith("cougaar."):
                tool = dep_entry.split(".", 1)[1]
                if dep_entry not in seen_ids:
                    add(Node(
                        id=dep_entry,
                        type="cougaar_retrieval",
                        depends_on=[],
                        metadata={"tool": tool,
                                   "source": f"depends_on:{product_type}.paragraph.{pname}"},
                    ))
                if dep_entry not in deps:
                    deps.append(dep_entry)
            elif dep_entry.startswith("artifact."):
                art = dep_entry.split(".", 1)[1]
                if dep_entry not in seen_ids:
                    add(Node(
                        id=dep_entry,
                        type="artifact_dep",
                        depends_on=[],
                        metadata={"artifact_name": art,
                                   "source": f"depends_on:{product_type}.paragraph.{pname}"},
                    ))
                if dep_entry not in deps:
                    deps.append(dep_entry)
            elif dep_entry.startswith("scenario."):
                # scenario.<TERRAIN|KG|PROJECT_DOCS|...>[.<sub>] — load directly
                # from app.utils.scenario at resolve-time. No upstream node
                # needed (the scenario module is process-local + cached); the
                # node-id IS the dep id, the executor reads scenario.<path>
                # via getattr/getitem chain.
                if dep_entry not in seen_ids:
                    add(Node(
                        id=dep_entry,
                        type="scenario_lookup",
                        depends_on=[],
                        metadata={"path": dep_entry.split(".", 1)[1],
                                   "source": f"depends_on:{product_type}.paragraph.{pname}"},
                    ))
                if dep_entry not in deps:
                    deps.append(dep_entry)
            elif dep_entry in paragraph_ids:
                sib_id = paragraph_ids[dep_entry]
                if sib_id not in deps:
                    deps.append(sib_id)
            else:
                deps.append(f"__unresolved_paragraph_dep__:{dep_entry}")
        add(Node(
            id=node_id,
            type="paragraph_field",
            depends_on=deps,
            metadata={
                "field_name": pname,
                "shape": pf.get("shape"),
                "required": pf.get("required", False),
                "guidance_brief": (para_guidance[:120] + "...") if len(para_guidance) > 120 else para_guidance,
                # Optional batching marker — paragraphs sharing the same
                # batch_group value get co-fired as ONE LLM call by the DAG
                # executor (one combined prompt, JSON response with N
                # paragraph_name keys). Saves the upstream-dep dump cost
                # which is otherwise paid N times for the N paragraphs.
                "batch_group": pf.get("batch_group") or "",
            },
        ))
        # Per-subfield informational nodes. Type `paragraph_row_field` —
        # NOT `row_field`, so the executor's row_field dispatch path doesn't
        # pick them up (they aren't column-of-row writers; the paragraph's
        # LLM dispatch authors the whole list_of_dicts at once). The nodes
        # are emitted so the DAG and mermaid view show inheritance — e.g.
        # WEM.paragraph.operations.row.parameters.row.current_forecast_value
        # depends on cougaar.get_environmental and is a child of
        # WEM.paragraph.operations.
        for row_node_id, row_name, row_guidance, row_deps in row_specs:
            # Each subfield depends on the parent paragraph (so the executor's
            # event stream shows the parent completes first) plus its own
            # accumulated deps. Suppress duplicate deps already implied by
            # the paragraph attachment.
            sf_deps: list[str] = [node_id]
            for d in row_deps:
                if d not in sf_deps:
                    sf_deps.append(d)
            add(Node(
                id=row_node_id,
                type="paragraph_row_field",
                depends_on=sf_deps,
                metadata={
                    "field_name": row_name,
                    "parent_paragraph": pname,
                    "guidance_brief": (row_guidance[:120] + "...") if len(row_guidance) > 120 else row_guidance,
                },
            ))

    # 6. Terminal submit node — depends on every emit-producing node in this
    #    product (header_field / row_field / paragraph_field). Does NOT
    #    depend on retrieval / artifact nodes directly — those are upstream
    #    of the field nodes.
    submit_deps: list[str] = []
    for n in nodes:
        if n.type in ("header_field", "row_field", "paragraph_field"):
            submit_deps.append(n.id)
    add(Node(
        id=f"{product_type}.submit",
        type="submit",
        depends_on=submit_deps,
        metadata={"product": product_type},
    ))

    return nodes


# -----------------------------------------------------------------------------
# Validation helpers
# -----------------------------------------------------------------------------

def validate_dag(nodes: list[Node]) -> list[str]:
    """Return a list of warning strings. Empty list = clean DAG."""
    warnings: list[str] = []
    ids = {n.id for n in nodes}
    for n in nodes:
        for d in n.depends_on:
            if d.startswith("__"):
                warnings.append(f"{n.id}: sentinel dep {d!r}")
            elif d not in ids:
                warnings.append(f"{n.id}: dangling dep {d!r}")
    # Cycle check via Kahn's
    indeg: dict[str, int] = {n.id: 0 for n in nodes}
    for n in nodes:
        for d in n.depends_on:
            if d in indeg:
                indeg[n.id] += 1
    queue = [nid for nid, deg in indeg.items() if deg == 0]
    visited = 0
    by_id = {n.id: n for n in nodes}
    while queue:
        cur = queue.pop()
        visited += 1
        # find children (nodes that depend on cur)
        for n in nodes:
            if cur in n.depends_on:
                indeg[n.id] -= 1
                if indeg[n.id] == 0:
                    queue.append(n.id)
    if visited != len(nodes):
        warnings.append(f"cycle detected (visited {visited}/{len(nodes)})")
    return warnings


def compute_waves(nodes: list[Node]) -> list[list[str]]:
    """Return the parallel-execution wave decomposition.

    Wave k contains every node whose dependencies are all in waves 0..k-1.
    Wave 0 is roots. Wall-time parallelism = len(waves).
    """
    by_id = {n.id: n for n in nodes}
    placed: dict[str, int] = {}
    waves: list[list[str]] = []
    remaining = set(by_id.keys())
    while remaining:
        wave: list[str] = []
        for nid in list(remaining):
            n = by_id[nid]
            # only count internal deps (deps that are also nodes in this DAG)
            internal_deps = [d for d in n.depends_on if d in by_id]
            if all(d in placed for d in internal_deps):
                wave.append(nid)
        if not wave:
            # unresolvable — break to avoid infinite loop, caller already
            # caught cycles via validate_dag
            break
        for nid in wave:
            placed[nid] = len(waves)
            remaining.discard(nid)
        waves.append(sorted(wave))
    return waves


def longest_path(nodes: list[Node]) -> tuple[int, list[str]]:
    """Return (length-in-edges, path) of the longest dependency chain."""
    by_id = {n.id: n for n in nodes}
    memo: dict[str, tuple[int, list[str]]] = {}

    def dfs(nid: str) -> tuple[int, list[str]]:
        if nid in memo:
            return memo[nid]
        n = by_id[nid]
        internal = [d for d in n.depends_on if d in by_id]
        if not internal:
            memo[nid] = (0, [nid])
            return memo[nid]
        best_len = -1
        best_path: list[str] = []
        for d in internal:
            dlen, dpath = dfs(d)
            if dlen > best_len:
                best_len = dlen
                best_path = dpath
        memo[nid] = (best_len + 1, best_path + [nid])
        return memo[nid]

    best = (0, [])
    for nid in by_id:
        L, P = dfs(nid)
        if L > best[0]:
            best = (L, P)
    return best


def edge_count(nodes: list[Node]) -> int:
    by_id = {n.id: n for n in nodes}
    c = 0
    for n in nodes:
        for d in n.depends_on:
            if d in by_id:
                c += 1
    return c


# -----------------------------------------------------------------------------
# Mermaid renderer
# -----------------------------------------------------------------------------

# Mermaid id chars are restricted — replace dots and other special chars.
_MERMAID_ID_RE = re.compile(r"[^A-Za-z0-9_]")


def _mid(node_id: str) -> str:
    return _MERMAID_ID_RE.sub("_", node_id)


_TYPE_LABEL = {
    "cougaar_retrieval":     "Cougaar Retrievals",
    "artifact_dep":          "Upstream Artifacts",
    "header_field":          "Header Fields",
    "row_field":             "Row Fields",
    "paragraph_field":       "Paragraph Fields",
    "paragraph_row_field":   "Paragraph Subfields",
    "submit":                "Submit",
}

_TYPE_ORDER = [
    "cougaar_retrieval",
    "artifact_dep",
    "header_field",
    "row_field",
    "paragraph_field",
    "paragraph_row_field",
    "submit",
]


def render_mermaid(nodes: list[Node]) -> str:
    """Render the DAG as a Mermaid `graph LR` diagram with subgraph clusters
    grouped by node type.
    """
    lines: list[str] = ["graph LR"]
    # Group by type
    by_type: dict[str, list[Node]] = defaultdict(list)
    for n in nodes:
        by_type[n.type].append(n)

    by_id = {n.id: n for n in nodes}

    # Subgraphs in fixed order
    for t in _TYPE_ORDER:
        bucket = by_type.get(t, [])
        if not bucket:
            continue
        lines.append(f"    subgraph {_mid(t)}[{_TYPE_LABEL[t]}]")
        for n in sorted(bucket, key=lambda x: x.id):
            label = n.id
            # Shorten visually for paragraph nodes
            if t == "paragraph_field":
                label = n.metadata.get("field_name", n.id)
            elif t == "cougaar_retrieval":
                label = n.metadata.get("tool", n.id)
            elif t == "artifact_dep":
                label = n.metadata.get("artifact_name", n.id)
            elif t == "header_field":
                label = n.metadata.get("field_name", n.id)
            elif t == "row_field":
                label = n.metadata.get("field_name", n.id)
            elif t == "paragraph_row_field":
                label = n.metadata.get("field_name", n.id)
            elif t == "submit":
                label = "submit"
            # Escape mermaid special chars in labels (quote / bracket).
            safe_label = label.replace('"', "'").replace("[", "(").replace("]", ")")
            lines.append(f'        {_mid(n.id)}["{safe_label}"]')
        lines.append("    end")

    # Edges
    for n in nodes:
        for d in n.depends_on:
            if d in by_id:
                lines.append(f"    {_mid(d)} --> {_mid(n.id)}")
            # else: dangling — silently drop in the visual but
            # validate_dag already surfaced it.

    return "\n".join(lines) + "\n"



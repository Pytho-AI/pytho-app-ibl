"""Async parallel-DAG runner for doctrinal product generation.

Bare-minimum INTSUM-only implementation per
scratch/ibl/reports/PARALLEL_DAG_REDESIGN.md and the five contracts in
pytho-dashboard/DESIGN.md §12. Other products fall back to the legacy
operations-expert / intel-agent / product-writer dispatch path via the
operations_expert tool layer.

Entry point: `run_dag(product_type, args, state, on_event) -> dict`.

Wave-by-wave execution:
  1. Parse the schema into Nodes via runtime.dag_parser.parse_dag_from_schema.
  2. Compute waves (topological levels).
  3. For each wave:
       - retrievals: call existing async cougaar adapters directly.
       - artifact deps: look up upstream artifact_id from state.registry.
                        If absent, mark optional empty and continue.
       - header_field / row_field / paragraph_field: build a per-field prompt
                        from the schema's `guidance` text plus the resolved
                        upstream dep payloads, call invoke_vllm_chat_async
                        with field-sized max_tokens, parse the response.
       - submit: merge field results into structured_data via the existing
                 intel_agent._merge_pw_emit + _validate_emit pipeline;
                 register via state.with_artifact + the same persistence
                 hooks the writer uses; render via
                 handlers.product.render_artifact_files.
  4. Return {artifact_id, structured_data, gaps, turn_total_ms, ...}.

NEVER calls manager.send_personal_message directly — every lifecycle event
goes through the `on_event` chokepoint that the dispatcher passes in.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import replace
from typing import Any, Callable, Optional

from runtime import dag_parser

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gap marker — failed optional nodes store this dict in `completed` instead
# of "" so downstream consumers can distinguish "field intentionally blank"
# from "precursor node failed and we have no data". Renderers use it to emit
# a "<source> fetch failed" notice that doesn't get blamed on the writer.
# ---------------------------------------------------------------------------


def _is_gap_marker(v: Any) -> bool:
    """True if `v` is the structured failure marker stored by the wave
    runner when an optional node fails (RDE-3). Treat as functionally-empty
    in field assemblers / citation builders / provenance, but DO log + emit
    a gap notice rather than silently dropping it."""
    return isinstance(v, dict) and v.get("__gap__") is True


# ---------------------------------------------------------------------------
# Canonical-on-disk resolver — every dep load routes through this helper so
# the resolver reads the FRESH structured_data the finalize-trigger wrote
# to schema_state.json after the analyst edited the rendered file.
#
# PERMISSIVE policy always — no staleness gating. If the analyst hasn't
# finalized after an edit, downstream sees the generation-time emit (which
# is fine — autodraft is a prewarm; the analyst explicitly re-fires
# downstreams via the finalize modal after editing if they want fresh
# downstream data).
# ---------------------------------------------------------------------------


def _load_upstream_structured_data(
    artifact_id: str | None,
    owner_user_id: str,
    fallback_sd: Any,
) -> Any:
    """Return the freshest available structured_data for an upstream artifact.

    Resolution order:
      1. Read uploads/<owner_user_id>/<artifact_id>/schema_state.json. If
         present and parseable, return data["structured_data"].
      2. Fallback to `fallback_sd` (writer just emitted, file not on disk
         yet — autodraft pool entries mid-fire or PROJECT_DOC: pseudo-
         artifacts).
      3. If `owner_user_id` is empty (cross-session pool hit didn't propagate
         owner), scan UPLOAD_DIR for any <owner>/<artifact_id>/schema_state.json.

    Never raises. Disk errors degrade to fallback with log.warning.
    """
    if not artifact_id:
        return fallback_sd
    # PROJECT_DOC pseudo-artifacts have no on-disk file.
    if isinstance(artifact_id, str) and artifact_id.startswith("PROJECT_DOC:"):
        return fallback_sd
    try:
        from handlers.product import UPLOAD_DIR
        from pathlib import Path
    except ImportError:
        return fallback_sd
    base = Path(UPLOAD_DIR)
    candidates = []
    if owner_user_id:
        candidates.append(base / owner_user_id / artifact_id / "schema_state.json")
    candidates.append(base / "_autodraft" / artifact_id / "schema_state.json")
    if not owner_user_id:
        # Cross-session scan.
        try:
            for owner_dir in base.iterdir():
                cand = owner_dir / artifact_id / "schema_state.json"
                if cand.exists() and cand not in candidates:
                    candidates.append(cand)
        except (FileNotFoundError, OSError):
            pass
    for ss_path in candidates:
        try:
            if not ss_path.exists():
                continue
            data = json.loads(ss_path.read_text(encoding="utf-8"))
            sd = data.get("structured_data")
            if isinstance(sd, dict):
                return sd
        except Exception as e:
            log.warning("_load_upstream_structured_data: read failed for %s: %s",
                        ss_path, e)
            continue
    return fallback_sd


# from:/path: resolver — walks an upstream structured_data dict via a
# json-path-flavored string. The grammar is the v3 nested-ref grammar's
# `path` payload (without the leading `ref:<Upstream>:`):
#
#   "header.classification"                 → sd["header"]["classification"]
#   "coas[2].statement.what"                → sd["coas"][2]["statement"]["what"]
#   "coas[<index>].coa_id"                   → ambient `<index>` substitution at iteration time
#   "coas[<this.coa_id>].functional_allocation" → match by sub-field value (lookup)
#
# Returns the leaf value or None when the path doesn't resolve. Never raises.
_PATH_TOKEN_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)|\[([^\]]+)\]|\.")


def _walk_from_path(structured_data: Any, path: str,
                    *, ambient: dict[str, Any] | None = None) -> Any:
    """Walk a dotted/bracket path into structured_data. `ambient` substitutes
    `<index>` and `<this.K>` tokens with values from the iteration cursor."""
    if not isinstance(structured_data, (dict, list)):
        return None
    if not path:
        return structured_data
    ambient = ambient or {}
    # Tokenize.
    tokens = []
    i = 0
    while i < len(path):
        ch = path[i]
        if ch == ".":
            i += 1
            continue
        if ch == "[":
            close = path.find("]", i)
            if close < 0:
                return None
            tokens.append(("idx", path[i + 1:close]))
            i = close + 1
            continue
        # identifier
        j = i
        while j < len(path) and path[j] not in ".[":
            j += 1
        tokens.append(("key", path[i:j]))
        i = j

    cur: Any = structured_data
    for kind, val in tokens:
        if kind == "key":
            if not isinstance(cur, dict):
                return None
            cur = cur.get(val)
            continue
        # idx — could be int, <index>, or <this.K>=<value> lookup
        v = val.strip()
        if v == "<index>":
            v = str(ambient.get("index", ""))
        elif v.startswith("<this.") and v.endswith(">"):
            key = v[len("<this."):-1]
            v = str(ambient.get(f"this.{key}", ambient.get(key, "")))
        # equality lookup?
        if "=" in v:
            lhs, rhs = v.split("=", 1)
            lhs = lhs.strip()
            rhs = rhs.strip().strip('"').strip("'")
            # Resolve angle-bracket tokens in rhs
            if rhs.startswith("<this.") and rhs.endswith(">"):
                tkey = rhs[len("<this."):-1]
                rhs = str(ambient.get(f"this.{tkey}", ambient.get(tkey, "")))
            elif rhs == "<index>":
                rhs = str(ambient.get("index", ""))
            if isinstance(cur, list):
                found = None
                for item in cur:
                    if not isinstance(item, dict):
                        continue
                    # walk lhs as dotted path
                    sub = _walk_from_path(item, lhs)
                    if str(sub) == rhs:
                        found = item
                        break
                cur = found
                continue
            return None
        # plain integer index
        try:
            idx = int(v)
        except ValueError:
            return None
        if isinstance(cur, list) and 0 <= idx < len(cur):
            cur = cur[idx]
        else:
            return None
    return cur


# Product-name → loader for the from:/path: resolver. Each loader takes
# (artifact_id, owner_user_id, fallback_sd) and returns the upstream's
# structured_data dict (or None).
def _load_from_product(product_name: str, artifact_id: str | None,
                       owner_user_id: str, fallback_sd: Any) -> Any:
    """Dispatch the `from:` declaration to the right loader.

    Recognized product-name forms:
      - Schema artifact name (e.g. "Threat COA Statement") -> on-disk
        schema_state.json via _load_upstream_structured_data.
      - "scenario" -> read scenario module attributes via the path.
      - "cougaar.<tool>" -> NOT YET WIRED (would call the tool; this is
        a runner-side dep, not a structured_data loader). Reserved.
    """
    if not product_name:
        return None
    if product_name == "scenario":
        try:
            from utils import scenario as _scenario
            return _scenario
        except ImportError:
            return None
    if product_name.startswith("cougaar."):
        # Reserved for future use — cougaar tools are runner-bound, not
        # structured_data loaders. The from:/path: resolver leaves these
        # for the writer LLM to author (no mechanical resolution).
        return None
    # Default: artifact-pool loader.
    return _load_upstream_structured_data(artifact_id, owner_user_id, fallback_sd)


def _resolve_from_path_subfield(subfield_spec: dict,
                                  resolved_deps: dict[str, Any],
                                  *, ambient: dict[str, Any] | None = None,
                                  owner_user_id: str = "") -> Any:
    """Resolve an item_subfield's value via its from:/path: declaration.

    Returns the resolved value or None if the declaration is absent /
    cannot be resolved. The writer LLM is bypassed for the subfield when
    this returns a non-None value.
    """
    if not isinstance(subfield_spec, dict):
        return None
    from_product = subfield_spec.get("from")
    path = subfield_spec.get("path")
    if not from_product or not path:
        return None
    # Find the upstream dep payload by product name.
    dep_payload = (resolved_deps or {}).get(f"artifact.{from_product}")
    if isinstance(dep_payload, dict):
        artifact_id = dep_payload.get("artifact_id")
        fallback_sd = dep_payload.get("structured_data")
    else:
        artifact_id = None
        fallback_sd = None
    sd = _load_from_product(from_product, artifact_id, owner_user_id, fallback_sd)
    if sd is None:
        return None
    if from_product == "scenario":
        # scenario.PROJECT_DOCS.<tag> etc. Walk via getattr semantics.
        parts = path.split(".")
        cur: Any = sd
        for p in parts:
            try:
                cur = getattr(cur, p)
            except AttributeError:
                if isinstance(cur, dict):
                    cur = cur.get(p)
                else:
                    return None
        return cur
    return _walk_from_path(sd, path, ambient=ambient)


# ---------------------------------------------------------------------------
# source_transform handlers — deterministic JOINs that take a resolved
# `from:/path:` value and project it into the consuming product's shape.
#
# Each handler signature:
#
#     handler(upstream_value, *, oob_units, ambient, item) -> Any
#
# `upstream_value` is whatever `_resolve_from_path_subfield` returned for
# the declared from:/path:. `oob_units` is the cougaar.get_oob units list
# (or [] when get_oob wasn't a dep / hasn't run). `ambient` is the
# iteration cursor (carries `index`, `this.<K>`). `item` is the parent
# list-of-dicts entry being filled in (so the handler can read sibling
# subfields like coa_id).
#
# Handlers return None to opt out (leaves the writer's emit, if any,
# untouched).
# ---------------------------------------------------------------------------


# Echelon canonicalization — upstream functional_allocation entries
# come in as raw unit designation strings ("604 TANK BDE", "60 MECH
# DIV", "WESTERN ARMY HQ"). The slide sidebar enum is the closed set
# CORPS / DIVISION / BRIGADE / BATTALION. Lookup against the OOB
# echelon field first; fall back to substring sniff so the handler
# stays useful when the OOB row's `echelon` is empty or absent.
def _canonicalize_echelon(raw: str | None) -> str:
    if not raw:
        return ""
    u = str(raw).upper().strip()
    if "CORPS" in u or "ARMY" in u or u.endswith("HQ") or "HEADQUARTERS" in u:
        return "CORPS"
    if "DIV" in u:
        return "DIVISION"
    if "BDE" in u or "BRIGADE" in u or "BRG" in u:
        return "BRIGADE"
    if " BN" in u or u.endswith(" BN") or "BATTALION" in u or "BTLN" in u:
        return "BATTALION"
    return ""


# Sort order for the sidebar — CORPS → DIVISION → BRIGADE → BATTALION.
# Unknown echelons sort last but keep their relative order.
_ECHELON_SORT_RANK = {
    "CORPS": 0,
    "DIVISION": 1,
    "BRIGADE": 2,
    "BATTALION": 3,
}


def _task_org_sidebar_transform(upstream_value: Any,
                                  *, oob_units: list,
                                  ambient: dict | None = None,
                                  item: dict | None = None) -> list[dict]:
    """Project upstream Threat COA Statement coas[i].functional_allocation
    into the Threat COAs slide-sidebar list-of-dicts shape.

    Input: a `functional_allocation` dict like
        {action_force: "60 MECH DIV", enabling_force: "604 TANK BDE",
         support_by_fires: "...", c2: "WESTERN ARMY HQ", reserve: "62 MTZD DIV", ...}

    Each value MAY be a single designation string, a list of designations,
    or a comma/semicolon-joined string. The handler splits and emits one
    row per distinct designation, joining each row to the cougaar.get_oob
    row by name/id match to recover the canonical echelon.

    Output: list of {echelon, designation, doctrinal_role} dicts, sorted
    CORPS → DIVISION → BRIGADE → BATTALION (then by emission order within
    an echelon).

    Returns [] when upstream_value isn't a dict (so the writer's emit is
    preserved by the post-emit pass — `None` would also skip the assign,
    but we prefer [] so the schema-required slot is filled deterministically
    and the empty-sidebar case renders as an empty list, not a writer
    fabrication).
    """
    if not isinstance(upstream_value, dict):
        return []
    # OOB index by designation (case-insensitive) AND by raw id token.
    oob_by_name: dict[str, dict] = {}
    for u in (oob_units or []):
        if not isinstance(u, dict):
            continue
        for k in ("name", "id"):
            tok = (u.get(k) or "").strip().upper()
            if tok and tok not in oob_by_name:
                oob_by_name[tok] = u

    def _split_designations(val: Any) -> list[str]:
        if val is None:
            return []
        if isinstance(val, list):
            out: list[str] = []
            for v in val:
                out.extend(_split_designations(v))
            return out
        if not isinstance(val, str):
            return [str(val).strip()] if val else []
        s = val.strip()
        if not s:
            return []
        # Comma / semicolon split. Keep slash + parenthetical content
        # together (e.g. "60 MECH DIV (- 604 TANK BDE)").
        parts = [p.strip() for p in re.split(r"[,;]", s) if p.strip()]
        return parts or [s]

    rows: list[dict] = []
    seen_keys: set[tuple] = set()
    for role, val in upstream_value.items():
        if not role or role.startswith("_"):
            continue
        for designation in _split_designations(val):
            # Strip parenthetical task-org notation for the OOB lookup key,
            # keep the full designation as the rendered designation.
            lookup = re.sub(r"\s*\(.*?\)\s*", "", designation).strip().upper()
            oob_row = oob_by_name.get(lookup) or oob_by_name.get(
                designation.strip().upper()
            )
            echelon = ""
            if isinstance(oob_row, dict):
                echelon = (oob_row.get("echelon") or "").strip().upper()
            if not echelon:
                echelon = _canonicalize_echelon(designation)
            key = (echelon, designation)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            rows.append({
                "echelon": echelon,
                "designation": designation,
                "doctrinal_role": role,
            })

    # Stable sort by echelon rank — preserve emission order WITHIN an
    # echelon (functional_allocation dict iteration order ≈ doctrinal
    # role priority: action_force first, sustainment / c2 later). Python's
    # `sort` is stable, so we only need to key on echelon rank.
    rows.sort(key=lambda r: _ECHELON_SORT_RANK.get(r.get("echelon") or "", 99))
    return rows


# source_transform registry — name → handler. Lookup by the schema's
# `source_transform` attribute on an item_subfield. Unknown transforms
# return None (the post-emit pass leaves the writer's emit untouched).
_SOURCE_TRANSFORMS = {
    "task_org_sidebar": _task_org_sidebar_transform,
}


def _resolve_source_transform_subfield(subfield_spec: dict,
                                         resolved_deps: dict[str, Any],
                                         *,
                                         completed: dict[str, Any] | None = None,
                                         ambient: dict[str, Any] | None = None,
                                         item: dict | None = None,
                                         owner_user_id: str = "") -> Any:
    """Resolve an item_subfield via its source_transform declaration.

    Pipeline:
      1. Read `subfield_spec["source_transform"]` to pick the handler.
      2. Resolve the from:/path: upstream value (handlers consume the
         upstream BEFORE projecting — no handler reads raw artifact dicts
         directly; they read the slice the from:/path: declaration names).
      3. Call the handler with the upstream value + cougaar.get_oob units
         (if get_oob was a dep / has run).

    Returns the handler's output, or None if the transform name isn't
    registered.
    """
    if not isinstance(subfield_spec, dict):
        return None
    transform_name = subfield_spec.get("source_transform")
    if not transform_name:
        return None
    handler = _SOURCE_TRANSFORMS.get(transform_name)
    if handler is None:
        log.warning(
            "source_transform=%r not registered; subfield %r left to writer-LLM",
            transform_name, subfield_spec.get("name"),
        )
        return None
    upstream_value = _resolve_from_path_subfield(
        subfield_spec, resolved_deps,
        ambient=ambient, owner_user_id=owner_user_id,
    )
    # cougaar.get_oob units list (when the dep fired). The `completed`
    # dict is node-id-keyed; cougaar nodes are id'd as `cougaar.<tool>`.
    oob_payload = (completed or {}).get("cougaar.get_oob") or {}
    oob_units = (oob_payload.get("units") if isinstance(oob_payload, dict) else []) or []
    return handler(
        upstream_value,
        oob_units=oob_units, ambient=ambient, item=item,
    )


# Field-sized max_tokens budgets per the redesign doc.
#
# paragraph_field bumped to 3000 because list_of_dicts paragraphs (templated_units,
# protected_targets_list, named_areas_of_interest) need to emit one JSON record per
# OOB/HPT/NAI entry — for SITEMP's ~11 OOB units with full provenance footnotes
# the 800-token budget truncated emissions mid-string, leaving _try_json with
# malformed input and the raw truncated string stored as the field value (showed
# up as the "1696 backticks" symptom when downstream iterated a string by char).
_MAX_TOKENS = {
    "paragraph_field": 3000,
    # row_field is per-COLUMN — ONE call emits the JSON object for every
    # row's value in that column. Sizing was 1500 but on Threat Overlay
    # + Threat Description Table (each ~222 doctrinal-echelon rows) the
    # writer's per-column JSON consistently hit finish_reason=length at
    # 1500 tokens, emitting only 33-38 of 222 rows of partial-and-unclosed
    # JSON that the downstream parser then discarded entirely (audit:
    # Threat Description Table shipped 0/222 populated analytical
    # columns). Mechanical kg-sourced columns are short-circuited
    # entirely (see _kg_row_field_value); the LLM only writes the
    # truly-analytical columns (description, additional_information,
    # strength_pct, last_observed_dtg). 6000 is enough headroom for
    # 222 rows × ~25 tok/cell of analytical prose without truncation.
    "row_field":       6000,
    "header_field":    80,
}

# Concurrency cap on simultaneous vLLM calls is now context-aware: the
# TokenWeightedGate in utils.llm caps the SUM of in-flight Gemma-BPE input
# tokens (default 150,000, configurable via VLLM_TOKEN_BUDGET). A 100k
# request takes 100k of the budget; a 5k request takes 5k — the gate admits
# more small requests in parallel and fewer large ones. Eliminates the KV
# thrash the old flat _MAX_CONCURRENT_LLM=8 cap allowed (8 × 100k = 800k
# of KV demand against a pool sized for ~16 × 131k).


# Cougaar tool dispatch in this module USED to live here as parallel
# async-native helpers (`_call_cougaar` + `_state_dependent_cougaar`)
# that bypassed `handlers.intel_agent.tool_*` because the originals
# wrapped async adapters with `asyncio.run(...)` (which fails inside a
# running event loop). That shortcut cost 5 days of regressions — the
# parallel impl drifted from the canonical one (missed affiliation
# normalization, OPORD-derived merge, freshest-source-wins ts logic,
# call-summary recording, filter / no_geometry / fixture-engagement
# merge, etc).
#
# The fix is documented in:
#   memory: feedback_no_duplicate_tool_stack.md
#   scratch/ibl/reports/COUGAAR_TOOL_DIVERGENCE_2026-05-26.md
#
# Don't write a parallel impl. Don't add wrappers around a sync tool
# to make it work in async. Fix the SHAPE — the adapters are async;
# the tools are now async-native (handlers.intel_agent.tool_get_oob
# etc. are `async def`); the DAG just `await`s them directly. Inline
# dispatch lives at the cougaar_retrieval branch in _run_one below.


# ----------------------------------------------------------------------------
# Per-field LLM call helpers
# ----------------------------------------------------------------------------
_FENCE_OPEN_RE = re.compile(r"```(?:json|JSON)?\s*\n?", re.IGNORECASE)
_LEADING_PROSE_RE = re.compile(r"^[^\[{]*", re.DOTALL)


def _strip_code_fences(s: str) -> str:
    """LLM wraps JSON output in ```json ... ``` (sometimes with extra prose around
    it, sometimes with nested or stray backticks inside, sometimes without the
    closing fence at all). Robust extraction:

      1. Find the first opening fence ```json (optional) and strip it + everything
         before it.
      2. Find the LAST occurrence of the closing fence ``` (greedy from the end)
         and strip everything from it onward.
      3. If no opening fence at all, fall through and trust the caller to parse —
         but trim leading prose up to the first [ or { so JSON-shaped tails parse.
    """
    s = (s or "").strip()
    open_match = _FENCE_OPEN_RE.search(s)
    if open_match:
        s = s[open_match.end():]
        close_idx = s.rfind("```")
        if close_idx != -1:
            s = s[:close_idx]
        s = s.strip()
    else:
        # No fence — strip leading prose up to first JSON-opening token if one exists.
        if s and s[0] not in "[{":
            stripped = _LEADING_PROSE_RE.sub("", s, count=1).strip()
            if stripped and stripped[0] in "[{":
                s = stripped
    return s


def _try_json(s: str):
    s = _strip_code_fences(s)
    try:
        return json.loads(s)
    except Exception:
        return None


# Trailing-provenance markers the writer keeps emitting despite the system-
# prompt rule against them. Patterns observed in 2026-05-21 autodraft renders:
#   "1234.0 pps. IDXTacElintReport.kbID=bc9c1a86-..."
#   "Not reported IDXTacElintReport.kbID=..."
#   "value _provenance: IDXTacElintReport.kbID=..."
#   "value _provenance IDXHumintReport.kbID=..."
#   "value _provenance: kg:get_oob"  (synthetic dep sources)
#   "value _provenance: ref:INTSUM_..."  (upstream artifact refs)
# Strip everything from the first such marker to end-of-string. The
# auto-stamp at emit-merge time owns _provenance proper as a separate dict
# key (see _build_skeleton.emit auto-stamp), so we lose nothing by removing
# the inline echo.
_PROV_LEAK_RE = re.compile(
    r"\s*(?:[._;,]?\s*)?"
    r"(?:_provenance(?:\s*:|\b)\s*)?"
    r"(?:"
        r"IDX[A-Z][A-Za-z0-9]*\.kbID=[\w-]+"
        r"|kg:[A-Za-z][\w.]*"
        r"|ref:[A-Z][\w-]+"
        r"|get_(?:terrain|environmental|civil|oob|recent_intel)\.[\w.]+"
    r")"
    r".*$"
)


def _strip_inline_provenance(value):
    """Recursively strip the writer's inline-provenance leak from any string
    leaf. The writer is INSTRUCTED via system prompt not to do this, but it
    ignores the instruction roughly half the time. Cheap regex strip keeps
    the rendered DOCX clean without re-prompting.

    Handles strings directly, dicts/lists recursively. Non-string leaves
    pass through unchanged."""
    if isinstance(value, str):
        return _PROV_LEAK_RE.sub("", value).rstrip(" .,;")
    if isinstance(value, dict):
        return {k: _strip_inline_provenance(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_inline_provenance(x) for x in value]
    return value


def _count_tokens(s: str) -> int:
    """Token count for dep-payload budget enforcement. Delegates to
    utils.tokenizer (single source of truth, locked to o200k_base — closest
    open encoding to modern SentencePiece tokenizers, within ~5% for
    English / structured-JSON content, which is fine for budget enforcement)."""
    from utils.tokenizer import count_tokens
    return count_tokens(s, fallback_chars_per_token=4)


# Live vllm reports max_model_len=131072 for the IBL Gemma-4-26B-A4B-NVFP4
# build. Total INPUT budget = context - max output - headroom. _DEP_PAYLOAD
# budget is 90% of that — the remaining 10% absorbs tokenizer drift
# (tiktoken vs Gemma's native SentencePiece can differ ~5%) plus the
# system prompt + field guidance + UPSTREAM section markers.
#
# Previously this constant was 256_000 (stale: anticipated context size).
# The mismatch meant DepBudgetExceeded never fired — vllm rejected at 131k
# with a 400 BadRequest instead, costing a full DAG node.
_GEMMA4_CONTEXT_TOKENS = 262_144 # 131_072
_PROMPT_HEADROOM_TOKENS = 8_000
_TOTAL_DEP_BUDGET_TOKENS = int(
    0.9 * (_GEMMA4_CONTEXT_TOKENS - max(_MAX_TOKENS.values()) - _PROMPT_HEADROOM_TOKENS)
)


def _serialize_payload(payload: Any) -> str:
    """Render a dep payload for the writer prompt.

    Uses COMPACT JSON (no whitespace, no indentation). Pretty-printing burns
    ~30% extra tokens on every dep payload — indent=2 was costing ~100k chars
    on the SITEMP templated_units prompt alone (artifact.Threat Overlay was
    201k chars indented vs ~140k compact). The model parses compact JSON
    just as well as pretty-printed; this is pure overhead.
    """
    try:
        return json.dumps(payload, default=str, separators=(",", ":"))
    except Exception:
        return str(payload)


class DepBudgetExceeded(Exception):
    """Raised when the assembled dep payloads exceed the model context
    budget — loud failure rather than silent truncation. The right fix is
    producer-side filtering (paragraph-level data_source_filter, an
    intermediate rollup paragraph that the downstream depends on instead of
    raw, or a tighter cougaar tool query) — NOT trimming rows out of the
    payload, which would silently corrupt the analysis."""


def _filter_payload_by_keys(payload: Any, keys: list[str]) -> Any:
    """Narrow a dict-shaped payload to just the listed keys.
    Non-dict payloads pass through unchanged.

    Two payload shapes:
      - flat dict (cougaar tool return): {avenues_of_approach, key_terrain,
        ...}. Narrow at the top level.
      - artifact_dep envelope: {artifact_id, structured_data: {...}, dtg,
        summary, _source}. Narrow INSIDE structured_data — the writer-facing
        bulk is in structured_data, the envelope keys are metadata. This
        keeps `["coas"]` on artifact.Threat COA Statement meaning
        "structured_data.coas", which is what schemas actually want.

    Use cases (flat dict):
      - get_terrain returns {avenues_of_approach, key_terrain, obstacles,
        mobility_classifications, cover_and_concealment, ...}. MCOO's
        avenues_of_approach paragraph only needs ['avenues_of_approach',
        'obstacles'] (obstacles is referenced for AA nesting). Filtering
        drops the other ~30k tokens.
      - get_civil returns {civil_entities, count, ...}. A paragraph that
        only needs entities filters out the metadata.

    `keys` may include `*` to mean "everything" (passthrough).
    """
    if not keys or "*" in keys or not isinstance(payload, dict):
        return payload
    # Artifact envelope detection — keep the envelope metadata, narrow the
    # nested structured_data. envelope fields (artifact_id, dtg, summary,
    # _source) are kept so the writer can still cite the source artifact.
    if isinstance(payload.get("structured_data"), dict) and "artifact_id" in payload:
        sd = payload["structured_data"]
        narrowed_sd = {k: sd[k] for k in keys if k in sd}
        out = {k: v for k, v in payload.items() if k != "structured_data"}
        out["structured_data"] = narrowed_sd
        out["_filtered_keys"] = keys
        return out
    out = {k: payload[k] for k in keys if k in payload}
    # Always preserve provenance metadata so the writer can cite source.
    for meta in ("_source", "_supplemented_from_fixture", "_note", "_source_note", "source_note"):
        if meta in payload and meta not in out:
            out[meta] = payload[meta]
    return out


def _strip_geo_agent_layers(payload: Any) -> Any:
    """Drop `geo_agent_<layer>` keys from a get_terrain return.

    Those keys carry the full per-feature GeoJSON FeatureCollection (~1M
    tokens per layer when geo-agent has loaded overlays). They're for the
    MAP RENDER (consumed via map_overlays metadata, not the prose payload).
    For ANY writer-facing paragraph, dropping these is safe — the writer
    cites by scenario.TERRAIN.<layer> entries (which carry friendly
    names and short MGRS strings, ≤2KB total), and the geo-agent ids
    surface on the rendered map figure.

    Applied universally so products that didn't declare a
    data_source_filter still don't get a 1.4M-token cougaar.get_terrain
    dropped on their prompt (OED hit DepBudgetExceeded with 1.44M tokens
    from get_terrain alone before this filter landed).
    """
    if not isinstance(payload, dict):
        return payload
    out = {k: v for k, v in payload.items() if not k.startswith("geo_agent_")}
    # Preserve overlay-metadata keys (count + fetched flag) so the writer
    # knows geo-agent had data, just not the per-feature geometry.
    return out


# Echelons the writer's prose actually operates on. Anything below
# BATTALION is org-chart noise for IPB doctrine (FM 100-2-3 / ATP 7-100.1
# / ATP 2-01.3 §3): the Significant Characteristics, Threat Model, Threat COA Statement,
# Threat Capability Statement etc. all reason at maneuver-BN-and-above
# granularity. Sub-BN org rows would only matter if cougaar populated
# location / strength / activity on them — see `_has_intel_grade_fields`
# below for the override that keeps any sub-BN unit that does.
_OOB_KEEP_ECHELONS = {
    "BATTALION", "BN",
    "BRIGADE", "BDE",
    "REGIMENT", "REGT",
    "DIVISION", "DIV",
    "CORPS",
    "ARMY",
}


def _has_intel_grade_fields(unit: dict) -> bool:
    """True if a unit row carries any field a writer can cite as intel
    (location, MGRS, current strength, observed activity). Cougaar's
    IDXRedUnit vertex currently leaves all four null on every sub-BN
    org-chart stub — but if that changes upstream, those rows survive
    the org-chart filter so the writer doesn't silently lose signal."""
    return any(unit.get(k) for k in ("loc", "mgrs", "strength", "activity"))


def _strip_org_chart_only_units(payload: Any) -> Any:
    """Trim `cougaar.get_oob` to the doctrinal ORBAT.

    The IDXRedUnit vertex ingests Donovian force structure all the way
    down to TEAM / SQUAD / SECTION — ~75 sub-BN slots per BN. With the
    full Donovian Western Army loaded (3 motor rifle DIVs + army-level
    enablers ≈ 178 maneuver/CS/CSS BNs), the raw payload runs ~13,420
    units / 1.49M tokens, blowing past the 220.5k DepBudgetExceeded cap
    on every paragraph that depends on `cougaar.get_oob`.

    The writer's prose lives at BATTALION-and-above per FM 100-2-3 /
    ATP 7-100.1 ORBAT doctrine. Sub-BN org rows are template TO&E
    expansions (CMD SEC, COUR SQD, 1 PLT 1, etc. each replicated
    100+ times across parent BNs) — pure noise to a doctrinal writer.
    Drop them universally, EXCEPT for any sub-BN unit that carries
    intel-grade fields (loc / mgrs / strength / activity) the writer
    could legitimately cite.

    Pre-filter applied alongside `_strip_geo_agent_layers` so every
    paragraph depending on cougaar.get_oob automatically lands under
    budget without each schema having to declare `data_source_filter`.

    Independent of the map render path: `intel_agent.tool_render_map`
    consumes its own copy of get_oob via `intel_data["get_oob"]` and
    filters by mgrs client-side, so dropping null-location stubs here
    is a no-op for the map.
    """
    if not isinstance(payload, dict):
        return payload
    units = payload.get("units")
    if not isinstance(units, list):
        return payload
    kept = []
    for u in units:
        if not isinstance(u, dict):
            continue
        ech = (u.get("echelon") or "").upper()
        if ech in _OOB_KEEP_ECHELONS or _has_intel_grade_fields(u):
            kept.append(u)
    if len(kept) == len(units):
        return payload
    out = dict(payload)
    out["units"] = kept
    # `count` is the published row count; refresh it so downstream
    # consumers don't see a stale 13420.
    if "count" in payload:
        out["count"] = len(kept)
    # Provenance breadcrumb so the writer (and anyone reading the
    # prompt payload in a trace) knows the rows were doctrinally
    # filtered, not silently truncated.
    out["_filtered_from"] = len(units)
    out["_filter_rule"] = (
        "echelon>=BATTALION OR any(loc|mgrs|strength|activity)"
    )
    return out


def _narrow_to_bound_coa(payload: Any, bound_coa_id: str) -> Any:
    """Narrow any artifact_dep payload's coas / coa_slides / per_coa arrays
    to just the entry whose coa_id matches `bound_coa_id`.

    Per-COA SITEMP / per-COA Threat-COA-Statement / per-COA Event-Template
    chains carry state.coa_focus.coa_id = "COA-X". Without this narrowing,
    every artifact_dep payload (Threat COA Statement, Threat COAs, etc.)
    surfaces ALL 4 COAs' mission_statement / scheme_of_maneuver /
    functional_allocation / decisive_engagement / branches_and_sequels /
    continuing_operations / phased_actions in the writer's prompt, even
    though THIS chain is only writing for ONE coa_id. That's how
    SITEMP.paragraph.axis_role_assignments balloons to 79k prompt tokens
    when it doctrinally needs ~6k.

    Safe to call universally: when bound_coa_id is empty (Threat COAs root
    chain, doctrine products, etc.) or when the payload has no coas-shaped
    list, returns payload unchanged.
    """
    if not bound_coa_id or not isinstance(payload, dict):
        return payload
    # Artifact-dep envelope shape: {artifact_id, structured_data: {...}}.
    sd = payload.get("structured_data")
    if isinstance(sd, dict):
        narrowed_sd: dict[str, Any] | None = None
        for list_key in ("coas", "coa_slides", "per_coa", "coa_list"):
            v = sd.get(list_key)
            if not isinstance(v, list) or not v:
                continue
            kept = [
                row for row in v
                if isinstance(row, dict) and (row.get("coa_id") or "").strip() == bound_coa_id
            ]
            # If nothing matched, leave the array alone — the writer would
            # benefit from seeing SOMETHING about the COAs rather than an
            # empty list. (Matching nothing usually means a stale
            # bound_coa_id naming convention mismatch.)
            if kept and len(kept) < len(v):
                narrowed_sd = narrowed_sd or dict(sd)
                narrowed_sd[list_key] = kept
        if narrowed_sd is not None:
            out = {k: v for k, v in payload.items() if k != "structured_data"}
            out["structured_data"] = narrowed_sd
            out["_coa_focused_to"] = bound_coa_id
            return out
        return payload
    # Flat-dict payload — narrow top-level coa-shaped keys directly.
    narrowed: dict[str, Any] | None = None
    for list_key in ("coas", "coa_slides", "per_coa", "coa_list"):
        v = payload.get(list_key)
        if not isinstance(v, list) or not v:
            continue
        kept = [
            row for row in v
            if isinstance(row, dict) and (row.get("coa_id") or "").strip() == bound_coa_id
        ]
        if kept and len(kept) < len(v):
            narrowed = narrowed or dict(payload)
            narrowed[list_key] = kept
    return narrowed if narrowed is not None else payload


def _apply_field_filters(node: dag_parser.Node, schema: dict,
                          resolved_deps: dict[str, Any],
                          bound_coa_id: str = "") -> dict[str, Any]:
    """Honor a paragraph's `data_source_filter` spec by narrowing or
    dropping each matching dep's payload BEFORE the writer sees it.

        "data_source_filter": {
            "cougaar.get_terrain": ["avenues_of_approach", "obstacles"],
            "cougaar.get_civil": ["civil_entities"],
            "cougaar.get_spatial_overlay": [],  # DROP entirely
        }

    Semantics:
      - dep_id NOT in filter dict        → passthrough (unfiltered).
      - `keys` is a non-empty list       → narrow payload to those keys.
      - `keys` is `[]` (empty list)      → DROP the dep from the writer's
                                            payload entirely. Used by rollup
                                            paragraphs (MCOO map / legend)
                                            that should consume only sibling
                                            paragraph rollups via
                                            `depends_on`, never raw cougaar
                                            tool returns.
    """
    if not isinstance(node.metadata, dict):
        return _finalize_for_prompt(resolved_deps)
    fname = node.metadata.get("field_name", "")
    if not fname:
        return _finalize_for_prompt(resolved_deps)
    spec = None
    if node.type == "paragraph_field":
        for pf in schema.get("paragraph_fields") or []:
            if isinstance(pf, dict) and pf.get("name") == fname:
                spec = pf
                break
    elif node.type == "header_field":
        spec = (schema.get("header_fields") or {}).get(fname)
    elif node.type == "row_field":
        # Row fields also support per-field data_source_filter (and the
        # schema-level default). HVTL's justification wave was emitting a
        # 110k-token prompt under default_data_source_filter because this
        # branch was missing — row_field nodes silently bypassed the
        # filter and dumped the full cougaar.get_oob payload on every
        # per-row prompt. See per-coa-product-scope.md note about HVTL
        # wave 3 being the single biggest call in the chain.
        spec = (schema.get("row_fields") or {}).get(fname)
    if not isinstance(spec, dict):
        return _finalize_for_prompt(resolved_deps)
    flt = spec.get("data_source_filter")
    # Schema-level default — applies to every paragraph_field that doesn't
    # override. Lets a product schema declare "every paragraph here gets
    # scoped artifact.X to keys Y" once, instead of repeating the filter on
    # 10 paragraphs. Per-paragraph data_source_filter still wins.
    default_flt = schema.get("default_data_source_filter")
    if not isinstance(flt, dict) or not flt:
        flt = default_flt if isinstance(default_flt, dict) else None
    elif isinstance(default_flt, dict):
        # Merge: paragraph filter wins for keys it sets; schema default
        # fills in keys the paragraph didn't mention.
        merged = dict(default_flt)
        merged.update(flt)
        flt = merged
    if not isinstance(flt, dict) or not flt:
        # No keys-filter — still apply COA narrowing so per-COA chains don't
        # see other COAs' narratives.
        if bound_coa_id:
            narrowed = {
                dep_id: _narrow_to_bound_coa(payload, bound_coa_id)
                for dep_id, payload in resolved_deps.items()
            }
            return _finalize_for_prompt(narrowed)
        return _finalize_for_prompt(resolved_deps)
    out: dict[str, Any] = {}
    for dep_id, payload in resolved_deps.items():
        if dep_id not in flt:
            out[dep_id] = payload
            continue
        keys = flt[dep_id]
        if not isinstance(keys, list):
            out[dep_id] = payload
            continue
        if not keys:
            continue
        out[dep_id] = _filter_payload_by_keys(payload, keys)
    # Per-COA narrowing: applied AFTER the keys-filter so we don't have to
    # repeat coa narrowing in every product's data_source_filter. When
    # state.coa_focus.coa_id is set (per-COA SITEMP / Threat COA Statement /
    # Event Template fanout), every artifact_dep payload's coas /
    # coa_slides array gets pruned to just the bound entry. Empty
    # bound_coa_id (Threat COAs root chain, etc.) = no-op.
    if bound_coa_id:
        out = {
            dep_id: _narrow_to_bound_coa(payload, bound_coa_id)
            for dep_id, payload in out.items()
        }
    # Even when an explicit filter ran, still drop geo_agent_<layer>
    # geometry — that always belongs on the map render, never in prose.
    return _finalize_for_prompt(out)


def _finalize_for_prompt(deps: dict[str, Any]) -> dict[str, Any]:
    """Apply the writer-prompt-only finalizing steps:
      (1) _apply_global_prefilters — strips geo_agent layers + slim oob
          units (per-unit field drop, keeps flat units:[...] shape) +
          slim get_recent_intel observation rows (drops raw_records /
          source_kbids explosions).
      (2) _group_oob_for_prompt — bucket cougaar.get_oob units by
          force_type then category for the writer.
      (3) _group_recent_intel_for_prompt — bucket
          cougaar.get_recent_intel reports by source_int.
    Group-by passes run ONLY at prompt time — runtime consumers
    (templated_units backfill, seen_ids harvest, render_map per-COA
    templating, _extract_writer_* picks) read intel_agent_data's flat-
    list shape and never see this group-by.
    """
    out = _apply_global_prefilters(deps)
    if "cougaar.get_oob" in out:
        out["cougaar.get_oob"] = _group_oob_for_prompt(out["cougaar.get_oob"])
    if "cougaar.get_recent_intel" in out:
        out["cougaar.get_recent_intel"] = _group_recent_intel_for_prompt(
            out["cougaar.get_recent_intel"]
        )
    return out


# Per-unit fields the writer needs in prose. force_type and category
# become group-by KEYS in the prompt shape (RED/BLUE split + nested
# category buckets) — _group_oob_for_prompt drops them from per-unit
# dicts BUT the flat _slim_oob_units output keeps them so downstream
# runtime consumers (templated_units backfill, _push_cougaar_to_geo_agent,
# render_map per-COA templating) can read them.
#
# Drop entirely:
#   - type_five_o: always "organization"
#   - doi: duplicates header DTG
#   - strength: mostly "full" (low-signal)
#
# 2026-06-01: sidc was DROPPED initially as "APP-6 symbol code,
# renderer-only" — that was WRONG. _push_cougaar_to_geo_agent uses
# sidc[1] as the canonical affiliation source for the geo-agent /sync
# payload (cougaar_adapters.build_geo_agent_sync_payload:722-731). With
# sidc stripped + force_type="HOSTILE"/"FRIENDLY" not matching the
# adapter's RED/BLUE fallback, every pushed mil_unit entity defaulted
# to affiliation="Unknown" — and the renderer's hostile-symbol path
# never engaged, so SITEMP maps drew 0 mil_units even though 288 were
# pushed to geo-agent. Putting sidc back into KEEP fixes affiliation
# derivation. Cost: ~15 chars/unit × 288 units = ~4.3k extra prompt
# chars — trivial.
_OOB_UNIT_KEEP_KEYS = (
    "id",                       # U-DN-* / U-BL-* canonical id
    "name",                     # short designation ("60 MECH IFV DIV")
    "echelon",                  # DIV / BDE / BN — drives two-levels-down rule
    "location_mgrs",            # templated_units position
    "doi",                      # date-of-information per-unit (row builder kg:doi reads this)
    "size_posture_activity",    # current activity in prose form
    "parent",                   # parent unit id (parent-subordinate uniqueness)
    "sidc",                     # MIL-STD-2525 symbol code — drives geo-agent
                                # affiliation derivation + APP-6 rendering
    # § 12 resolved-OOB fields — composition rollup + multi-position view
    # threaded through to writer prompts. Composition_rollup can be large
    # (~50 items per unit) so the dep-filter callers should narrow at
    # field level; KEEP carries it through so HVTL/CARVER can opt in.
    "composition_rollup",
    "composition_by_fm360_category",
    "attributed_positions",
    "rate_of_march_kmh",
    "hvt_worthy_count",
    "disposition",
)


def _slim_oob_units(payload: Any) -> Any:
    """Field-slim every cougaar.get_oob unit row. KEEPS the flat units:[...]
    shape so downstream consumers (templated_units backfill, seen_ids
    harvest, render_map per-COA templating, _extract_writer_* picks) keep
    working — they iterate units[*] as a list.

    Drops noisy keys per-unit: type_five_o (always 'organization'), doi
    (duplicates header DTG), strength (mostly 'full'), sidc (renderer
    symbol code), category and force_type (will become group-by KEYS in
    the prompt-only grouping pass).

    A separate _group_oob_for_prompt restructures into bucketed RED/BLUE +
    category shape, applied ONLY at the writer-prompt rendering site so
    runtime consumers iterating units[*] don't break.
    """
    if not isinstance(payload, dict):
        return payload
    units = payload.get("units")
    if not isinstance(units, list):
        return payload
    slim: list[dict] = []
    dropped_keys: set[str] = set()
    # Keep category + force_type on the unit row (the grouping pass needs
    # them to bucket; runtime consumers can ignore them).
    KEEP = _OOB_UNIT_KEEP_KEYS + ("category", "force_type")
    for u in units:
        if not isinstance(u, dict):
            slim.append(u)
            continue
        kept = {k: u[k] for k in KEEP if k in u}
        for k in u.keys():
            if k not in kept:
                dropped_keys.add(k)
        slim.append(kept)
    out = dict(payload)
    out["units"] = slim
    if dropped_keys:
        out["_slim_oob_dropped_keys"] = sorted(dropped_keys)
    return out


def _group_oob_for_prompt(payload: Any) -> Any:
    """Prompt-only restructure: bucket cougaar.get_oob units by force_type
    then by category. Drops the per-unit category + force_type fields
    (they become bucket keys). Output shape:
      {
        "red_units_by_category": {
          "command_and_control": [unit, unit, ...],
          "precision_fire_systems": [...],
          ...
        },
        "blue_units_by_category": {...},
        "red_count": ..., "blue_count": ...,
        "_shape": "grouped_by_force_type_then_category"
      }

    Applied at _apply_field_filters' end (prompt rendering only). Runtime
    consumers that iterate units[*] read the flat-list shape from
    intel_agent_data, which is left untouched by this pass.

    Savings vs flat-list slim: per-unit drops `category` + `force_type`
    (~50 chars/unit) — for 288 units, that's ~14k chars saved on top of
    the per-unit field slim from _slim_oob_units.
    """
    if not isinstance(payload, dict):
        return payload
    units = payload.get("units")
    if not isinstance(units, list):
        return payload
    from collections import defaultdict
    red_groups: dict[str, list[dict]] = defaultdict(list)
    blue_groups: dict[str, list[dict]] = defaultdict(list)
    for u in units:
        if not isinstance(u, dict):
            continue
        kept = {k: u[k] for k in _OOB_UNIT_KEEP_KEYS if k in u}
        category = (u.get("category") or "uncategorized") or "uncategorized"
        force_type = (u.get("force_type") or "").upper()
        if force_type in ("HOSTILE", "RED"):
            red_groups[category].append(kept)
        elif force_type in ("FRIENDLY", "BLUE"):
            blue_groups[category].append(kept)
        else:
            red_groups[category].append(kept)
    out = {
        "red_units_by_category": dict(red_groups),
        "blue_units_by_category": dict(blue_groups),
        "red_count": payload.get("red_count"),
        "blue_count": payload.get("blue_count"),
        "_shape": "grouped_by_force_type_then_category",
    }
    if "_source" in payload:
        out["_source"] = payload["_source"]
    return out


# cougaar.get_recent_intel rows the writer cites in prose. The tool
# returns fused observations whose `raw_records` carry one entry per
# source kbID — for the OOB bucket that's often 5-20 near-identical dicts
# {uuid, ref_id, unit_key, enemy_unit, activity, observation_time=null,
# location=null} per observation. Plus a `source_kbids` array repeating
# every uuid. Per fused observation that's ~1.5kB of duplication; with
# the default 25-per-bucket × 5 INTs cap that's ~190kB the writer never
# reads — directly responsible for the INTSUM paragraph_2 400 from vllm
# (prompt 128k input + 3k output > 131k Gemma-4 context). Keep the
# evidence_text rollup that summarizes the duplicates ("11 OOB report(s);
# activities=RECON") plus the disambiguating fields — observation_id,
# entity_key/name, source_int, observed_time, reported_time, location/
# location_mgrs, confidence, body — and drop the raw_records explosion.
_RECENT_INTEL_OBS_KEEP_KEYS = (
    "observation_id",
    "entity_key",
    "entity_name",
    "name",
    "source_int",
    "observed_time",
    "reported_time",
    "location",
    "location_mgrs",
    "mgrs",
    "confidence",
    "evidence_text",
    "summary",
    "body",
    "type",
    "activity",
    "event",
    # IDXReccexrepReport fields surfaced into cougaar.get_recent_intel.
    # engagements[*] by handlers/intel_agent.tool_get_recent_intel. The
    # writer needs the GRVTY-supplied target_status / gentext / ben /
    # item_name / mission_code / mission_platform_or_type / time_on_target
    # to author the engagement_record + Phase 1 PDA + Phase 2 FDA
    # subfields. Stripping these (as the older allow-list did) left the
    # writer with bare {type:'ENGAGEMENT'} husks and forced 'Not reported'
    # across every BDA artifact.
    "kbID",
    "source_vertex",
    "mission_type", "mission_code", "mission_platform_or_type",
    "target_type", "target_status",
    "item_name", "item_number",
    "ben", "gentext",
    "time_on_target", "report_time",
    "target_id", "target_kg_id", "target_name",
    # IIR enrichment surfaced into cougaar.get_recent_intel.imagery[*]
    # — IDXImageIirObservation.additionalInfo carries GRVTY narratives
    # like 'ENEMY RECON BN STATIONARY WITH DESTROYED EQUIPMENT';
    # imageryChangeSignificance is NAC/UNK/SC; iir_file_url is the
    # IDXImageIirReport.fileURL the exporter fetches + embeds.
    "country",
    "iir_additional_info", "iir_change_significance",
    "iir_oob_type", "iir_observation_time",
    "iir_file_url",
    "image_weather", "image_quality_code",
    "grid",
)


def _slim_recent_intel_row(row: Any) -> Any:
    if not isinstance(row, dict):
        return row
    return {k: row[k] for k in _RECENT_INTEL_OBS_KEEP_KEYS if k in row}


def _slim_recent_intel(payload: Any) -> Any:
    """Field-slim every cougaar.get_recent_intel observation row across
    every bucket (reports, imagery, engagements, osint). Keeps the bundle
    shape so downstream consumers iterating bundle["reports"][*] etc.
    keep working — only per-row keys change.

    Drops per-row: raw_records (the writer reads evidence_text rollups,
    never the per-kbID body which is mostly nulls), source_kbids (UUIDs
    the writer never cites in prose; provenance is keyed off the
    observation_id), and any other keys outside _RECENT_INTEL_OBS_KEEP_KEYS.
    The dropped keys land in _slim_recent_intel_dropped_keys for audit.
    """
    if not isinstance(payload, dict):
        return payload
    out = dict(payload)
    dropped: set[str] = set()
    for bucket in ("reports", "imagery", "engagements", "osint"):
        rows = payload.get(bucket)
        if not isinstance(rows, list):
            continue
        slim_rows: list[dict] = []
        for r in rows:
            if not isinstance(r, dict):
                slim_rows.append(r)
                continue
            kept = _slim_recent_intel_row(r)
            for k in r.keys():
                if k not in kept:
                    dropped.add(k)
            slim_rows.append(kept)
        out[bucket] = slim_rows
    if dropped:
        out["_slim_recent_intel_dropped_keys"] = sorted(dropped)
    return out


def _group_recent_intel_for_prompt(payload: Any) -> Any:
    """Prompt-only restructure: bucket cougaar.get_recent_intel reports
    by source_int (OOB / HUMINT / TACTICAL / SIGINT / ELINT). The flat
    `reports` list merges every INT family — the writer can ingest it
    flat, but a by-INT grouping gives the LLM a clearer scaffold (and
    drops the per-row source_int field once it becomes a bucket key).
    imagery / engagements / osint stay as their own buckets.

    Output shape:
      {
        "reports_by_source_int": {
          "OOB":      [obs, obs, ...],
          "HUMINT":   [...],
          "TACTICAL": [...],
          "SIGINT":   [...],
          "ELINT":    [...],
        },
        "imagery":     [...],
        "engagements": [...],
        "osint":       [...],
        "window_hours": ..., "types_requested": [...],
        "_int_counts": {...}, "_per_vertex_counts": {...},
        "_per_vertex_populated": {...}, "_total_count": ...,
        "_supplemented_from_fixture": ..., "_source": ...,
        "_note": ..., "_shape": "grouped_by_source_int",
      }

    Applied at _finalize_for_prompt's end (prompt rendering only).
    Runtime consumers that iterate bundle["reports"][*] read the flat-
    list shape from intel_agent_data, which is left untouched.
    """
    if not isinstance(payload, dict):
        return payload
    from collections import defaultdict
    by_int: dict[str, list[dict]] = defaultdict(list)
    for r in (payload.get("reports") or []):
        if not isinstance(r, dict):
            continue
        kept = {k: v for k, v in r.items() if k != "source_int"}
        si = (r.get("source_int") or "UNKNOWN").upper()
        by_int[si].append(kept)
    out: dict[str, Any] = {
        "reports_by_source_int": dict(by_int),
        "imagery":     payload.get("imagery") or [],
        "engagements": payload.get("engagements") or [],
        "osint":       payload.get("osint") or [],
        "_shape": "grouped_by_source_int",
    }
    for k in ("window_hours", "types_requested", "_int_counts",
              "_per_vertex_counts", "_per_vertex_populated",
              "_total_count", "_supplemented_from_fixture",
              "_source", "_note", "_error",
              "_fixture_engagement_count", "_fixture_osint_count",
              "_slim_recent_intel_dropped_keys",
              "_ems_volume_summary"):
        if k in payload:
            out[k] = payload[k]
    return out


def _strip_row_level_provenance(value: Any) -> Any:
    """Recursively drop `_provenance` keys from dicts that are LIST
    elements anywhere in the payload tree. Top-level (non-list-element)
    `_provenance` is preserved.

    Rationale: provenance is a PRODUCT-LEVEL concept — the artifact's
    overall _provenance dict cites the sources for its claims. But the
    INPUT data (raw cougaar tool returns + upstream artifact rows) does
    NOT need per-row provenance bloat — every OOB unit row carries an
    eight-key `_provenance` dict that's just "kg:get_oob" repeated for
    each field. 200 OOB units × 8-key provenance = 30-40k token bloat
    PER paragraph that consumes OOB-bearing payloads (cougaar.get_oob
    directly, AND artifact.Threat Overlay.rows where the OOB rows ride
    embedded in an upstream artifact).

    Heuristic: ONLY strip `_provenance` from dicts that are themselves
    list elements — leaves on a list value path. A top-level dict's
    `_provenance` is the artifact-level citation block and stays.
    """
    if isinstance(value, list):
        return [_strip_row_level_provenance_in_list_element(v) for v in value]
    if isinstance(value, dict):
        return {k: _strip_row_level_provenance(v) for k, v in value.items()}
    return value


def _strip_row_level_provenance_in_list_element(value: Any) -> Any:
    """List-element variant: drop `_provenance` from this dict if it's a
    dict, then recurse into nested lists. Used to mark the descent into
    a list — list-element dicts are the rows we want to slim."""
    if isinstance(value, dict):
        return {
            k: _strip_row_level_provenance(v)
            for k, v in value.items()
            if k != "_provenance"
        }
    if isinstance(value, list):
        return [_strip_row_level_provenance_in_list_element(v) for v in value]
    return value


# Tokens that are pointers/markers rather than real underlying sources —
# we drop these when flattening an upstream artifact's provenance so a
# downstream product inherits the GROUND-TRUTH sources, not another layer
# of artifact pointers or gap/failure markers. Matched case-insensitively
# against the citation segment's leading label.
_NON_SOURCE_PROV_PREFIXES = (
    "pytho artifact:",
    "pytho autodraft pool:",
    "artifact:",
)


def _flatten_upstream_provenance(structured_data: Any) -> list[str]:
    """Collect every underlying-source citation segment from an upstream
    artifact's structured_data, so a downstream product can inherit the
    sources transitively rather than citing only a one-hop artifact pointer.

    Reads the TOP-LEVEL `structured_data["_provenance"]` dict — the
    artifact-level citation block. Per the _strip_row_level_provenance
    design, per-row provenance is input bloat and is NOT the canonical
    source record; the top-level block already carries the rolled-up,
    " | "-joined sources for the artifact's claims (and, because each
    product stamps its own inherited sources here when it is built, this
    block is already transitive across the full chain — reading one hop
    yields the whole lineage).

    Values are " | "-joined multi-source strings with an optional
    " — As of <DTG>" tail (see the citation builder). We split on " | ",
    drop the DTG tail and any artifact-pointer / gap-marker segments
    (those are not ground-truth sources), and return deduped segments in
    first-seen order. The caller re-stamps these so they ride forward and
    keep flattening at the next hop.
    """
    if not isinstance(structured_data, dict):
        return []
    prov = structured_data.get("_provenance")
    if not isinstance(prov, dict):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for raw in prov.values():
        if not isinstance(raw, str) or not raw.strip():
            continue
        # Drop the trailing "— As of <DTG>" acquisition stamp; the
        # downstream emit appends its own.
        body = raw.split(" — As of ", 1)[0]
        for seg in body.split(" | "):
            seg = seg.strip()
            if not seg:
                continue
            low = seg.lower()
            if any(low.startswith(p) for p in _NON_SOURCE_PROV_PREFIXES):
                continue  # artifact pointer — not a ground-truth source
            if "fetch failed" in low:
                continue  # gap/failure marker
            if seg in seen:
                continue
            seen.add(seg)
            out.append(seg)
    return out


def _apply_global_prefilters(deps: dict[str, Any]) -> dict[str, Any]:
    """Apply per-tool global pre-filters that strip payload sections no
    writer can use, BEFORE the dep-budget cap is enforced. Each filter
    is a doctrinal narrowing, not a truncation — anything dropped is
    either map-render-only geometry or org-chart noise the writer never
    cites in prose.
      - cougaar.get_terrain → drop geo_agent_<layer> (multi-megabyte
        FeatureCollections; the map-render reads them via map_overlays).
      - cougaar.get_oob → drop sub-BN org-chart stubs with no intel-grade
        fields (loc/mgrs/strength/activity). THEN slim each remaining unit
        row to the writer-essential keys via _slim_oob_units.
      - cougaar.get_recent_intel → slim every observation row to the
        writer-essential keys (drop raw_records duplicate-uuid arrays
        and source_kbids); _group_recent_intel_for_prompt then bucket by
        source_int at prompt-rendering time.
      - EVERY payload → strip row-level `_provenance` (per-row
        provenance metadata is bloat; provenance is product-level).
    Hook for future tools that return similarly-large payloads."""
    out: dict[str, Any] = {}
    for dep_id, payload in deps.items():
        if dep_id == "cougaar.get_terrain":
            payload = _strip_geo_agent_layers(payload)
        elif dep_id == "cougaar.get_oob":
            payload = _slim_oob_units(_strip_org_chart_only_units(payload))
        elif dep_id == "cougaar.get_recent_intel":
            payload = _slim_recent_intel(payload)
        # Universal: strip per-row _provenance bloat. Top-level
        # _provenance (artifact-citation dict) is preserved by the
        # heuristic — only list-element dicts get their _provenance
        # dropped.
        out[dep_id] = _strip_row_level_provenance(payload)
    return out


def _citations_from_cougaar_payload(tool: str, payload: dict, schema: dict,
                                       node: dag_parser.Node,
                                       fname: str) -> list[str]:
    """Extract granular source citations from a cougaar tool's return.

    Tools are CARRIERS, not sources. The Sources block must name the
    underlying records (KG vertex + kbID, OPORD doc tag + fragment,
    scenario fixture key, geo-agent overlay map UUID) the tool surfaced.
    Naming the tool tells the reader nothing — `get_terrain` is just an
    accessor; the analyst needs to know whether the value came from a
    scenario-authored fixture, a live KG query against a specific
    vertex, or an OPORD parse.

    Returns a list of distinct citation strings (one per source family).
    Honors the paragraph's `data_source_filter` so we cite only the keys
    the writer actually saw, not every key the tool happened to return.
    """
    # Resolve which keys this field's data_source_filter narrows the
    # payload to (matches the writer's visible view). Missing filter
    # means the writer saw the whole payload.
    keys_filter: Optional[list[str]] = None
    spec = None
    if node.type == "paragraph_field":
        for pf in schema.get("paragraph_fields") or []:
            if isinstance(pf, dict) and pf.get("name") == fname:
                spec = pf
                break
    elif node.type == "header_field":
        spec = (schema.get("header_fields") or {}).get(fname)
    if isinstance(spec, dict):
        flt = spec.get("data_source_filter") or {}
        ks = flt.get(f"cougaar.{tool}")
        if isinstance(ks, list):
            if not ks:
                # Empty list = dropped dep; no citation contribution.
                return []
            keys_filter = ks

    bits: list[str] = []

    # ----- Tier 1: row-shaped payloads (get_oob, get_civil, get_targets,
    # get_recent_intel, etc.) — drill into `rows`/`items`/`civil_entities`/
    # `units` lists. Each row may carry _source_vertex, _source_doc_tag,
    # kbID. Aggregate distinct vertex types + collect ID ranges.
    row_containers = []
    for k in ("rows", "items", "units", "civil_entities", "targets",
              "signals", "phases", "events"):
        v = payload.get(k)
        if isinstance(v, list) and v:
            row_containers.append((k, v))

    for container_key, rows in row_containers:
        vertex_groups: dict[str, list[str]] = {}
        doc_tag_groups: dict[str, list[str]] = {}
        scenario_authored_ids: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_id = row.get("kbID") or row.get("id") or ""
            vertex = row.get("_source_vertex") or ""
            doc_tag = row.get("_source_doc_tag") or ""
            row_src = row.get("_source") or ""
            if vertex:
                vertex_groups.setdefault(vertex, []).append(str(row_id))
            elif doc_tag:
                doc_tag_groups.setdefault(doc_tag, []).append(str(row_id))
            elif row_src == "opord_derived":
                # OPORD-derived without explicit doc_tag — group under
                # generic "OPORD parse"
                doc_tag_groups.setdefault("OPORD parse (Gemma4-extracted)",
                                            []).append(str(row_id))
            else:
                # Scenario fixture row (no vertex tag).
                if row_id:
                    scenario_authored_ids.append(str(row_id))
        for vertex, ids in vertex_groups.items():
            id_summary = _summarize_ids(ids)
            bits.append(f"KG vertex {vertex}: {id_summary} ({len(ids)} entries)")
        for doc_tag, ids in doc_tag_groups.items():
            id_summary = _summarize_ids(ids) if any(ids) else ""
            tail = f": {id_summary}" if id_summary else ""
            bits.append(f"OPORD-derived doc {doc_tag}{tail} ({len(ids)} entries)")
        if scenario_authored_ids:
            id_summary = _summarize_ids(scenario_authored_ids)
            bits.append(
                f"Scenario fixture KG.{container_key}: {id_summary} "
                f"({len(scenario_authored_ids)} entries)"
            )

    # ----- Tier 2: dict-of-layers payloads (get_terrain). The TERRAIN
    # fixture is scenario-authored; geo_agent_<layer> siblings come from
    # the geo-agent master map. Cite per consumed layer.
    if tool == "get_terrain":
        geo_map_id = payload.get("_geo_agent_map_id") or ""
        for layer_key in (keys_filter
                           if keys_filter is not None
                           else list(payload.keys())):
            if layer_key in ("aoi", "summary", "_source", "source_note",
                              "_geo_agent_map_id", "_geo_agent_fetched",
                              "_geo_agent_overlay_count", "_geo_agent_error"):
                continue
            layer_val = payload.get(layer_key)
            if layer_key.startswith("geo_agent_"):
                if isinstance(layer_val, list) and layer_val and geo_map_id:
                    bits.append(
                        f"Geo-Agent overlay map {geo_map_id} "
                        f"layer {layer_key} ({len(layer_val)} features)"
                    )
            elif isinstance(layer_val, list) and layer_val:
                bits.append(
                    f"Scenario fixture TERRAIN.{layer_key} "
                    f"({len(layer_val)} entries)"
                )

    # ----- Tier 3: metadata-blob payloads (get_phases scalar fields,
    # get_environmental WEATHER record). Cite by source identifier when
    # present in the payload root.
    if not bits:
        # Final fallback: cite scenario / KG generically with the tool's
        # `_source` tag. NOT the tool name — that's not a source.
        src_tag = (payload.get("_source") or "").strip()
        if src_tag == "scenario_authored":
            bits.append(f"Scenario fixture (via {tool})")
        elif src_tag in ("cougaar", "cougaar_live", "geo_agent_plus_scenario"):
            bits.append(f"Cougaar KG (via {tool})")
        elif src_tag:
            bits.append(f"{src_tag} (via {tool})")

    return bits


def _summarize_ids(ids: list[str]) -> str:
    """Compact ID-list summary for citations. Strips empty/None, dedupes,
    and either lists them inline (≤6) or shows first..last with a count."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for x in ids:
        if not x or x in seen_set:
            continue
        seen_set.add(x)
        seen.append(x)
    if not seen:
        return ""
    if len(seen) <= 6:
        return ", ".join(seen)
    return f"{seen[0]} … {seen[-1]}"


def _summarize_all_deps(resolved_deps: dict[str, Any]) -> list[str]:
    """Serialize every dep payload and enforce the TOTAL token budget.

    Producer-side filters (`data_source_filter`, intermediate rollup deps)
    are expected to keep the assembled prompt under the cap. When they
    don't, this raises `DepBudgetExceeded` — LOUD failure surfaces the
    schema gap rather than silently dropping rows.
    """
    if not resolved_deps:
        return []
    out: list[str] = []
    sizes: list[tuple[str, int]] = []
    for dep_id, payload in resolved_deps.items():
        s = _serialize_payload(payload)
        out.append(f"# {dep_id}\n{s}")
        sizes.append((dep_id, _count_tokens(s)))
    total = sum(n for _, n in sizes)
    if total > _TOTAL_DEP_BUDGET_TOKENS:
        offenders = sorted(sizes, key=lambda p: -p[1])[:5]
        raise DepBudgetExceeded(
            f"Assembled dep payloads sum to {total} tokens, cap is "
            f"{_TOTAL_DEP_BUDGET_TOKENS}. Largest deps: "
            + ", ".join(f"{d}={n}" for d, n in offenders)
            + ". Add `data_source_filter` to the paragraph spec to narrow "
            "each dep to the keys this field actually needs, OR introduce "
            "an intermediate rollup paragraph the field depends on instead "
            "of consuming raw."
        )
    return out


def _resolve_row_count_config(schema: dict, pf_spec: dict) -> dict:
    """Resolve a paragraph_field's `row_count_source` into the configured
    row-count block. Returns the resolved dict (e.g. SPEC["product_config"]
    ["coa_set"] when the source is "product_config.coa_set") or an empty dict
    when the source is missing or unresolvable.

    The resolved block carries `count` (int) and `ranks` (list of rank
    entries with `flag_fields` + `narrative_doctrine_ref` per the configured
    coa_set shape). Schemas declare row_count_source on a list_of_dicts
    paragraph_field when row count + rank->flag/narrative_doctrine_ref
    binding is configuration data the writer must NOT discretionarily pick.
    See enemy_course_of_action.py SPEC["product_config"]["coa_set"]."""
    src = (pf_spec or {}).get("row_count_source") or ""
    if not src or not isinstance(src, str):
        return {}
    node = schema
    for part in src.split("."):
        if not isinstance(node, dict):
            return {}
        node = node.get(part)
        if node is None:
            return {}
    return node if isinstance(node, dict) else {}


def _collect_all_flag_field_names(coa_set_config: dict) -> list[str]:
    """Collect the union of every flag_field name declared across the
    configured ranks. Used by the deterministic post-emit pass to know which
    row keys to FORCE False for ranks that don't carry that flag (so the
    writer's emitted flag values get fully overwritten, not just
    overlaid)."""
    names: list[str] = []
    for r in (coa_set_config or {}).get("ranks") or []:
        for f in (r or {}).get("flag_fields") or []:
            if f and f not in names:
                names.append(f)
    return names


def _apply_row_count_config_post_emit(schema: dict, merged_sd: dict) -> dict:
    """Deterministic post-emit pass for paragraph_fields that declare
    `row_count_source`. For each row in the list:

      * Look up the configured rank entry at
        product_config.<row_count_source>.ranks[row.rank - 1].
      * Set every flag_field name in that rank entry's `flag_fields` to True
        on the row; set every OTHER flag-field name declared across the
        coa_set's ranks to False. Overwrites the writer's flag emission so
        the rank->flag binding is deterministic (writer-discretion was the
        source of the rank=3 / most_dangerous misalignment).
      * Copy the rank entry's `narrative_doctrine_ref` value to the row's
        `narrative_doctrine_ref` field (None when the rank has no
        narrative-doctrine reference bound, e.g. the most_deceptive rank
        in the IDX-26 coa_set carries "SunTzuOPFOR" while the others
        carry None).

    Returns the stats dict {paragraph: applied_count} for logging. Mutates
    merged_sd in place. Quietly no-ops for paragraph_fields without
    row_count_source, for rows missing a rank value, and for ranks that
    fall outside the configured range (those are surfaced as warnings)."""
    stats: dict[str, int] = {}
    for pf in (schema.get("paragraph_fields") or []):
        if not isinstance(pf, dict):
            continue
        if pf.get("shape") != "list_of_dicts":
            continue
        cfg = _resolve_row_count_config(schema, pf)
        if not cfg:
            continue
        ranks_cfg = cfg.get("ranks") or []
        all_flag_names = _collect_all_flag_field_names(cfg)
        para_name = pf.get("name", "")
        rows = merged_sd.get(para_name)
        if not isinstance(rows, list):
            continue
        applied = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            rank_val = row.get("rank")
            try:
                rank_int = int(rank_val)
            except (TypeError, ValueError):
                log.warning(
                    "row_count_config post-emit: %s row has unparseable rank=%r; "
                    "leaving flag_fields + narrative_doctrine_ref untouched",
                    para_name, rank_val,
                )
                continue
            if not (1 <= rank_int <= len(ranks_cfg)):
                log.warning(
                    "row_count_config post-emit: %s rank=%d outside configured "
                    "1..%d; leaving flag_fields + narrative_doctrine_ref untouched",
                    para_name, rank_int, len(ranks_cfg),
                )
                continue
            rank_entry = ranks_cfg[rank_int - 1] if isinstance(
                ranks_cfg[rank_int - 1], dict) else {}
            on_flags = set(rank_entry.get("flag_fields") or [])
            # Force every configured flag-field to its rank-bound value.
            for flag_name in all_flag_names:
                row[flag_name] = (flag_name in on_flags)
            # Copy narrative_doctrine_ref from the configured rank entry
            # (None when not bound — the schema spec for the field is
            # required=False and accepts None as a legitimate empty).
            row["narrative_doctrine_ref"] = rank_entry.get("narrative_doctrine_ref")
            applied += 1
        if applied:
            stats[para_name] = applied
    return stats


def _build_field_prompt(node: dag_parser.Node, schema: dict, args_context: str,
                        resolved_deps: dict[str, Any],
                        row_ids: list | None = None,
                        bound_coa_id: str = "") -> tuple[str, str]:
    """Build (system_prompt, user_prompt) for a single field node. The
    system prompt fixes role + output contract; the user prompt carries the
    field's doctrine + dep payloads.
    """
    fname = node.metadata.get("field_name", node.id)
    guidance = node.metadata.get("guidance_brief", "") or ""

    def _collapse_nomenclature(name: str) -> str:
        """Strip cougaar's noisy series/model identifiers so doctrinally-
        equivalent variants collapse into ONE family+role string.

        Cougaar's OOB carries multiple model variants per doctrinal role
        ('MTLB 1V13 FDC', 'MTLB 1V16 FDC', 'MTLB 1V13 TRKD BTY FDC' —
        all Fire Direction Centers on an MTLB chassis). Showing all 11
        variants in the enumeration makes the writer pick all 11 and
        the fan-out drowns the artifact in duplicate-role rows. Collapse
        to 'MTLB FDC' so writer picks ONCE and the fan-out aggregates
        every series variant under it.

        Rule: tokenize, drop any pure-numeric / alphanumeric model
        codes (1V13, 9A51, 9A310M1, 30MM, 1RL257), keep alphabetic
        role/family tokens. Falls back to the un-collapsed normalized
        string if collapsing strips everything (defensive).
        """
        import re as _re
        norm = " ".join(
            name.upper()
                .replace("-", " ").replace("_", " ").replace(".", " ")
                .split()
        )
        kept = []
        for tok in norm.split():
            # Drop tokens that contain any digit — these are series /
            # model identifiers (1V13, 9A51, 30MM, 5T, T 90A's '90A',
            # 9A310M1). Pure alphabetic role/family words survive.
            if any(ch.isdigit() for ch in tok):
                continue
            kept.append(tok)
        collapsed = " ".join(kept)
        return collapsed or norm

    def _build_oob_nomenclature_enum(resolved_deps: dict) -> str:
        """Flatten cougaar.get_oob_red.units[*].composition_rollup into a
        sorted list of unique nomenclature strings with their total
        instance counts across the OOB. Writer picks verbatim from this
        list — no fuzzy matching, no hyphen-vs-space drift. The selected
        kg_ids guidance instructs the writer to copy strings EXACTLY
        from this enumeration; downstream expansion is a dict lookup.
        Returns "" when the dep isn't resolvable yet."""
        units = []
        for key in ("cougaar.get_oob_red", "cougaar.get_oob"):
            dep = resolved_deps.get(key) or {}
            if isinstance(dep, dict):
                u = dep.get("units")
                if isinstance(u, list) and u:
                    units = u
                    break
                # Sometimes deps come grouped by category — flatten.
                rbc = dep.get("red_units_by_category") or {}
                if isinstance(rbc, dict):
                    for v in rbc.values():
                        if isinstance(v, list):
                            units.extend(v)
                    if units:
                        break
        if not units:
            return ""
        # Collapse cougaar's series variants into doctrinal families.
        # Multiple model-numbers per role ('MTLB 1V13 FDC' /
        # 'MTLB 1V16 FDC' / 'MTLB 1V13 TRKD BTY FDC' all become
        # 'MTLB FDC') so the writer picks each family+role ONCE.
        counts: dict[str, int] = {}
        for u in units:
            comp = (u or {}).get("composition_rollup") or {}
            if isinstance(comp, dict):
                for nm, c in comp.items():
                    if isinstance(nm, str) and nm:
                        family = _collapse_nomenclature(nm)
                        if not family:
                            continue
                        counts[family] = counts.get(family, 0) + int(c or 0)
        if not counts:
            return ""
        # Saturated nomenclatures (count > 200) are infrastructure
        # scattered across every unit — engagement footprints we
        # cannot saturate against. Writer can't pick what it doesn't
        # see, so they're dropped from the enumeration entirely. The
        # omitted count is reported so the writer knows the universe
        # is filtered.
        SAT_THRESHOLD = 200
        kept    = [(nm, n) for nm, n in counts.items() if n <= SAT_THRESHOLD]
        dropped = sum(1 for _, n in counts.items() if n > SAT_THRESHOLD)
        if not kept:
            return ""
        # Sort ASCENDING by count so the writer reads the actual
        # scarce chokepoints (single-digit counts) FIRST — top-down
        # reading bias picks from the first plausible-looking entries.
        rows = sorted(kept, key=lambda kv: (kv[1], kv[0]))
        lines = ["═════════════════════════════════════════════════════════",
                  "VALID NOMENCLATURES — PICK VERBATIM FROM THIS ENUMERATION",
                  "═════════════════════════════════════════════════════════",
                  "(format: <count instances> <nomenclature> — copy the",
                  " nomenclature string EXACTLY into your output; no",
                  " hyphenation, no doctrinal renaming, no abbreviation.)",
                  "",
                  "Sorted ASCENDING by count. Low-count entries are the",
                  "scarce single-points-of-failure whose loss CASCADES",
                  "across many downstream units. Pick from this list ONLY."]
        if dropped:
            lines.append(
                f"({dropped} saturated nomenclatures with count > "
                f"{SAT_THRESHOLD} OMITTED — distributed infrastructure, "
                "not pickable chokepoints.)"
            )
        lines.append("")
        for nm, n in rows:
            lines.append(f"  {n:>5}× {nm}")
        return "\n".join(lines)
    # The brief in metadata is a 80–120 char preview — pull the FULL guidance
    # from the schema itself.
    full_guidance = ""
    pf_spec: dict = {}
    if node.type == "header_field":
        spec = (schema.get("header_fields") or {}).get(fname) or {}
        full_guidance = spec.get("guidance", "") or guidance
    elif node.type == "paragraph_field":
        for pf in schema.get("paragraph_fields") or []:
            if isinstance(pf, dict) and pf.get("name") == fname:
                pf_spec = pf
                full_guidance = pf.get("guidance", "") or guidance
                shape = pf.get("shape", "string")
                subfields = pf.get("subfields") or []
                item_subfields = pf.get("item_subfields") or []
                break
        else:
            shape = "string"
            subfields = []
            item_subfields = []
        # Filter item_subfields with source: "derived:..." OUT of the
        # prompt the writer sees. Those fields are pipeline-computed
        # post-emit (e.g. MCOO width_km from terrain planner, echelon
        # from ATP 2-01.3 Table 4-2 thresholds) — exposing their guidance
        # to the writer either invites fabrication or wastes its
        # reasoning trying to derive a value it can't measure from
        # qualitative input data. Mirrors the header_field convention
        # where source="tool:..." / "session" / "computed" / "ref:..."
        # short-circuits the writer call entirely.
        item_subfields = [
            sf for sf in item_subfields
            if not (isinstance(sf, dict)
                    and isinstance(sf.get("source"), str)
                    and sf["source"].startswith("derived:"))
        ]
        # PRE-prompt bypass for from:/path: + source_transform subfields.
        # The post-emit resolver pass (~L2480) mechanically fills these
        # from upstream structured_data / source_transform handlers. If
        # the writer ALSO emits values for them, the upstream gets
        # overwritten anyway — and the writer's regurgitation often drifts
        # (paraphrase, synonym substitution, list reordering). Exclude
        # the subfield from the writer's output-keys + field-guidance
        # blocks when an upstream payload is available; the post-emit
        # resolver will fill the slot. Subfields whose upstream is NOT
        # available stay in the prompt so the writer fallback-authors per
        # the per-subfield guidance.
        #
        # Saves prompt-tokens (no per-subfield guidance for the 9 mirrored
        # subfields) and output-tokens (writer emits N items × fewer keys
        # per item) on every Threat COAs / SITEMP / per-COA fanout call.
        def _has_upstream(sf: dict) -> bool:
            from_product = sf.get("from")
            if not from_product:
                return False
            dep = (resolved_deps or {}).get(f"artifact.{from_product}")
            if not isinstance(dep, dict):
                return False
            if dep.get("_empty"):
                return False
            return True
        item_subfields = [
            sf for sf in item_subfields
            if not (isinstance(sf, dict)
                    and (
                        (sf.get("from") and sf.get("path") and _has_upstream(sf))
                        or (sf.get("source_transform") and _has_upstream(sf))
                    ))
        ]
    else:
        full_guidance = guidance

    product_name = schema.get("name", "")
    doctrine = (schema.get("doctrine") or {}).get("purpose", "")
    other_paragraphs = [p.get("name") for p in (schema.get("paragraph_fields") or [])
                        if isinstance(p, dict) and p.get("name")]

    sys_lines = [
        f"You are the {product_name} per-field writer. You write ONE field at a time.",
        f"Doctrinal purpose: {doctrine}",
        "Hard rules:",
        " - Output ONLY the field content, no preamble, no doctrine citation (no ATP/FM/TC/JP refs).",
        " - Never paraphrase a verbatim source (e.g. PIR text from the OPORD — copy exactly).",
        (" - 'No significant activity reported.' / 'NSTR' / 'N/A' is the "
         "correct output ONLY when the UPSTREAM DEPENDENCY PAYLOADS "
         "section below is genuinely empty. If ANY dep payload contains "
         "items (count > 0, non-empty list, etc.), synthesize from them. "
         "When dep items don't fit your field's narrow doctrinal scope "
         "(e.g. writing AIR DEFENSE but deps are all voice C2 intercepts), "
         "describe the absence explicitly AND point to where the activity "
         "DID land — never just emit the bare default token."),
        " - NEVER emit redaction or classification placeholder tokens — no [REDACTED], (REDACTED), --REDACTED--, [CLASSIFIED], [SECRET], [CONFIDENTIAL], XXXXX, asterisk-blocks, █████, or any variant. This exercise is UNCLASSIFIED; every grid, unit ID, target ID, and artifact_id is published in full. Real-world INTSUMs in your training corpus are redacted before release — IBL artifacts are not.",
        " - 'Not reported' is BANNED as a fallback. If the named row-key (HPTL row, AGM row, etc.) doesn't match the bound target by kg_id, DO NOT default to 'Not reported'. Instead: read the ANALYST CONTEXT block + BOUND TARGET KG RECORD + cougaar.get_recent_intel.engagement (RECCEXREP rows) + cougaar.get_recent_intel.imagery (IIR observations). These carry the LIVE-CONFLICT data for this exact target. Synthesize the field's value from those. Only when ALL of those are also empty should you leave the field blank — never write the literal token 'Not reported'.",
        " - DO NOT fabricate identifiers, units, or events that are absent from the dep payload. Synthesize ONLY from what's in the payload + the analyst context.",
        " - You see ONLY this field's dependencies — other paragraphs in this product are written by sibling calls and you must not duplicate their content.",
        " - NEVER append `_provenance`, kbID citations, IDX vertex names (IDXTacElintReport.kbID=...), or any provenance markup to the field's content. The runner auto-stamps `_provenance` as a SEPARATE structured field — your value is the prose / data ONLY. Outputs like 'DOGEARTA _provenance: IDXTacElintReport.kbID=abc' or 'Not reported. IDXTacElintReport.kbID=...' are bugs.",
    ]
    if node.type == "paragraph_field":
        sys_lines.append(f"You are writing field `{fname}` of {product_name}. Sibling fields covered in parallel: {', '.join(p for p in other_paragraphs if p != fname)}.")
        if shape == "string":
            sys_lines.append("Output ONE plain prose paragraph. No JSON, no list, no markdown headers.")
        elif shape == "dict":
            sys_lines.append(
                "Output a JSON object with EXACTLY these keys, verbatim, "
                "in this order: " + ", ".join(subfields) + ". Do NOT "
                "rename, translate, or substitute synonyms. Do NOT add "
                "extra keys. Do NOT omit any key."
            )
            sys_lines.append("Each value is a short prose paragraph (or 'No significant activity reported.' for subfields not exercised this period).")
            sys_lines.append("Return ONLY the JSON object, no prose around it.")
        elif shape == "list_of_dicts":
            keys = [sf.get("name") for sf in item_subfields if isinstance(sf, dict)]
            sys_lines.append(
                f"Output a JSON array of objects, each with EXACTLY "
                f"these top-level keys verbatim (no synonyms, no "
                f"additions, no omissions): {', '.join(keys)}."
            )
            sys_lines.append("Return ONLY the JSON array, no prose around it.")
            # Inject the configured EMIT-EXACTLY-N clause when the
            # paragraph_field declares row_count_source. The writer must
            # emit one object per rank 1..N — count comes from
            # product_config, not from writer discretion. See
            # _resolve_row_count_config above. The flag_fields and
            # narrative_doctrine_ref are overwritten post-emit per the
            # configured rank entry, so the writer's job is to author
            # each rank's analytical content (statement, objectives,
            # functional_allocation, etc.) — not to decide rank->flag
            # binding.
            _row_count_cfg = _resolve_row_count_config(schema, pf_spec)
            _row_count = _row_count_cfg.get("count")
            if isinstance(_row_count, int) and _row_count > 0:
                _ranks_for_prompt = _row_count_cfg.get("ranks") or []
                _role_summary_lines = []
                for r in _ranks_for_prompt:
                    if not isinstance(r, dict):
                        continue
                    _rank_num = r.get("rank")
                    _role = r.get("role", "")
                    _flags = r.get("flag_fields") or []
                    _flag_clause = (
                        f" (flag_fields: {', '.join(_flags)})" if _flags
                        else ""
                    )
                    _role_summary_lines.append(
                        f"   - rank {_rank_num}: role={_role}{_flag_clause}"
                    )
                sys_lines.append(
                    f"EMIT EXACTLY {_row_count} objects, one per rank "
                    f"1..{_row_count}. The runner WILL reject any array "
                    f"with a different length and re-prompt. Per-rank "
                    f"role + flag_fields binding (the writer authors the "
                    f"analytical content; the runner overwrites the "
                    f"flag fields + narrative_doctrine_ref deterministically "
                    f"post-emit):\n" + "\n".join(_role_summary_lines)
                )
    elif node.type == "header_field":
        sys_lines.append(f"You are filling header field `{fname}` of {product_name}.")
        sys_lines.append("Output is a short string — typically 1-5 words. No JSON, no quotes around the value.")
    elif node.type == "row_field":
        # One node fans out across every row in the row_source payload. The
        # LLM emits a JSON object mapping row identifier (target_id, unit_id,
        # line_id — whichever the schema's row_source produces) to the
        # analytical value for THIS column on that row.
        sys_lines.append(f"You are filling column `{fname}` for ALL rows of {product_name}.")
        sys_lines.append("The upstream payload contains the row iteration set. For each row, emit your analytical value for the `" + fname + "` column.")
        sys_lines.append("Use the EXACT row id values from the upstream payload (the `id` field on each row) as the keys of your JSON object. NEVER emit 'N/A' as a key — pick a real id from the payload, or omit that row entirely.")
        # When the field declares an enum, constrain the LLM to those exact
        # tokens. Free-text values break downstream grouping / label maps
        # (HVTL category collapse, CARVER value buckets, etc.).
        row_spec = (schema.get("row_fields") or {}).get(fname) or {}
        enum_values = row_spec.get("enum") or []
        if enum_values:
            sys_lines.append(
                f"The value of `{fname}` MUST be one of the following exact tokens — "
                f"no other strings, no synonyms, no display labels: "
                + ", ".join(repr(v) for v in enum_values) + "."
            )
        sys_lines.append("Output a JSON object mapping each row's identifier (e.g. target_id, unit_id) to its column value. Return ONLY the JSON object, no prose around it.")

    # Universal + per-product hard rules from templates.common
    # HARD_RULES_REGISTRY, appended LAST so they're the freshest thing in
    # the writer's attention. Registry covers ICD-203 (likelihood scale,
    # confidence-vs-likelihood, assumptions vs judgments, analysis of
    # alternatives, clear argumentation) + PROVENANCE_HARD_RULE; the
    # per-product schema can add more via spec["hard_rules"]. See
    # templates.common.HARD_RULES_REGISTRY + hard_rules_for().
    try:
        from templates.common import hard_rules_for as _hard_rules_for
        _registry_rules = _hard_rules_for(product_name, schema.get("hard_rules") or [])
    except Exception:
        _registry_rules = []
    if _registry_rules:
        sys_lines.append("")
        sys_lines.append("---")
        sys_lines.append("HARD RULES — read these LAST, apply to every field you emit:")
        for _rule in _registry_rules:
            sys_lines.append("")
            sys_lines.append(_rule)

    sys_prompt = "\n".join(sys_lines)

    # ----- USER PROMPT ordering for vLLM prefix-cache hits -----
    # vLLM's --enable-prefix-caching matches token-prefix substrings across
    # calls. The longest shared prefix collapses to one prefill instead of
    # N. To maximize the shared prefix WITHIN a product run, blocks are
    # ordered SHARED → PER-FIELD:
    #
    #   1. Scenario fixture block       — identical across all calls (per-DTG cached)
    #   2. Analyst context              — identical within one autodraft run
    #   3. Upstream dependency payloads — varies per field's deps; deterministic ordering
    #                                     so two fields with the same dep set share their prefix
    #   4. Field guidance               — per-field (different copy)
    #   5. Subfield / item-subfield keys — per-field
    #   6. Row-id iteration (dst_dps)   — per-field
    #   7. "WRITE THE VALUE FOR ..."    — per-field tail
    #
    # Audit showed 73k-token prompts on Threat COA Statement + 43k on Threat Overlay row
    # fields. Before this reorder, field guidance (which differs per call)
    # came first and forced every call into a fresh prefill — vLLM logs
    # showed 66% prefix-cache hit rate (decent for the shared system
    # prompt). After: the entire shared block (fixture + analyst context
    # + same-dep payloads) sits at the FRONT of the user prompt and only
    # the field-specific tail re-prefills, pushing the hit rate up.
    user_lines: list[str] = []

    # Block 1+2: SHARED across every call in this product run.
    user_lines.append(_scenario_fixture_block())
    if args_context:
        user_lines.append(f"=== ANALYST CONTEXT ===\n{args_context}\n")

    # Schema-flagged nomenclature enumeration. Fields with
    # `inject_oob_nomenclatures: True` get a flat list of every unique
    # equipment nomenclature in the cougaar OOB (with total instance
    # counts) appended to their user prompt. Writer picks verbatim
    # from this list — exact downstream lookup, no fuzzy matching.
    _field_spec: dict = {}
    if node.type == "header_field":
        _field_spec = (schema.get("header_fields") or {}).get(fname) or {}
    elif node.type == "paragraph_field":
        _field_spec = pf_spec or {}
    if _field_spec.get("inject_oob_nomenclatures"):
        _enum_block = _build_oob_nomenclature_enum(resolved_deps)
        if _enum_block:
            user_lines.append(_enum_block + "\n")

    # Block 3: dep payloads, deterministically ordered so same-dep-set
    # fields share their prefix segment.
    if resolved_deps:
        # Schema-declared `data_source_filter` narrows each dep payload to
        # only the keys this field needs BEFORE the writer ever sees it.
        # Plus per-COA narrowing when bound_coa_id is set (per-COA chains).
        resolved_deps = _apply_field_filters(node, schema, resolved_deps, bound_coa_id)
        # For expand_to_equipment products (HVTL / HPTL), the curated
        # nomenclature enumeration replaces the raw composition_rollup
        # dump across EVERY field prompt — wave 1 selection AND wave
        # 2+ analytical row_field batches (carver_score /
        # enemy_reaction / impact_of_loss / etc.) all reason about
        # equipment types that are already known and bearer-unit data
        # already aggregated by the post-merge fan-out.
        # Verified on HVTL_20260611T100327_840Z: cougaar.get_oob_red
        # was 234K of the 281K total prompt — 84% of every wave.
        # Strip applies whenever the schema declares expand_to_equipment
        # OR the specific field declares inject_oob_nomenclatures.
        _strip_comp = (
            schema.get("expand_to_equipment")
            or _field_spec.get("inject_oob_nomenclatures")
        )
        # Per-unit keep-list. Audited 2026-06-11 against
        # HVTL_20260611T102649_729Z's cougaar.get_oob_red payload (225
        # units × 736 chars/unit = 165K). Only these keys are used by
        # downstream writer fields; the rest are internal markers
        # (_source_*), always-constant (force_type, type_five_o),
        # redundant with the enumeration (composition_by_fm360_category),
        # redundant with location_mgrs (primary_position), or empty
        # (attributed_positions, size_posture_activity). Drops the
        # per-unit avg from 736 → ~150 chars, total cougaar.get_oob_red
        # from 156K → ~35K, total selection prompt from 201K → ~80K.
        _UNIT_KEEP = {
            "id", "name", "category", "echelon", "location_mgrs",
            "parent", "doi", "kbID", "sidc",
        }
        if _strip_comp:
            for k in ("cougaar.get_oob_red", "cougaar.get_oob"):
                dep = resolved_deps.get(k)
                if isinstance(dep, dict) and isinstance(dep.get("units"), list):
                    pruned_units = []
                    for u in dep["units"]:
                        if isinstance(u, dict):
                            slim = {kk: vv for kk, vv in u.items()
                                    if kk in _UNIT_KEEP}
                            pruned_units.append(slim)
                        else:
                            pruned_units.append(u)
                    # Mutate a COPY of the dep dict so other concurrent
                    # field calls don't see the slim view.
                    new_dep = dict(dep)
                    new_dep["units"] = pruned_units
                    new_dep["_units_composition_rollup_stripped"] = (
                        "see VALID NOMENCLATURES enumeration above — "
                        "raw composition_rollup dropped to save ~230K "
                        "tokens; refer to the collapsed enumeration for "
                        "all equipment counts."
                    )
                    resolved_deps[k] = new_dep
        user_lines.append("=== UPSTREAM DEPENDENCY PAYLOADS ===")
        # Sort deps by dep_id so two calls with the same dep set produce
        # the same byte-for-byte prefix.
        ordered_deps = dict(sorted(resolved_deps.items()))
        user_lines.extend(_summarize_all_deps(ordered_deps))

    # Block 4: per-field FIELD GUIDANCE (varies per call — first prefix-cache miss).
    if full_guidance:
        user_lines.append(f"=== FIELD GUIDANCE ({fname}) ===\n{full_guidance}\n")
    if node.type == "paragraph_field" and shape == "dict" and subfields:
        user_lines.append(
            "Required subfield keys — the output JSON object MUST use "
            "EXACTLY these keys, in this order, verbatim. Do NOT rename, "
            "translate, or substitute synonyms (e.g. NEVER emit "
            "'primary' instead of 'immediate', NEVER 'end_state' "
            "instead of 'desired_end_state'). Do NOT add extra keys. Do "
            "NOT omit required keys. The keys are: "
            + ", ".join(subfields)
        )
    elif node.type == "paragraph_field" and shape == "list_of_dicts" and item_subfields:
        for sf in item_subfields:
            if isinstance(sf, dict):
                user_lines.append(f"- `{sf.get('name')}` ({sf.get('render_label','')}, required={sf.get('required', False)}): {sf.get('guidance','')}")
                # If this item_subfield is itself a dict-shape with its
                # own nested `subfields`, enumerate those nested keys
                # verbatim + attach per-nested-key guidance from
                # subfield_guidance. Without this the writer sees only
                # the outer item_subfield name (e.g. "statement") and
                # invents plausible sub-keys (e.g. "primary" instead of
                # "immediate"), leaving the renderer's canonical-key
                # lookups blank on the analyst-facing artifact.
                #
                # `strict_subfields` (default True) controls whether the
                # writer is required to emit EVERY listed key. Some
                # nested dicts (e.g. TCOAS `functional_allocation`) enumerate
                # a UNION of applicable keys across operation types
                # (offense uses action_force/exploitation_force/reserve;
                # defense uses disruption_force/main_defense_force/
                # counterattack_force). Forcing the writer to emit all
                # keys wastes inference on non-applicable role-bins and
                # produces empty rows the renderer then displays as
                # doctrinally-nonsensical `—`. When `strict_subfields:
                # False`, emit the key list as the applicable set the
                # writer picks from per the field guidance (which
                # already encodes the operation-type → role-bin mapping),
                # not as a mandatory-all-keys emission contract.
                _nested_subfields = sf.get("subfields") or []
                _nested_subfield_guidance = sf.get("subfield_guidance") or {}
                _nested_strict = sf.get("strict_subfields", True)
                if _nested_subfields:
                    if _nested_strict:
                        user_lines.append(
                            f"  Inside `{sf.get('name')}`, the value MUST be "
                            f"a JSON object using EXACTLY these keys "
                            f"verbatim (no synonyms, no additions, no "
                            f"omissions): "
                            + ", ".join(_nested_subfields)
                        )
                    else:
                        user_lines.append(
                            f"  Inside `{sf.get('name')}`, the value is a "
                            f"JSON object. The KEY VOCABULARY is a fixed "
                            f"list from which the writer picks the "
                            f"doctrinally-applicable subset per the field "
                            f"guidance above (typically 3-5 of the "
                            f"following depending on operation type). If a "
                            f"key does not apply to this row, OMIT it "
                            f"entirely — do NOT emit it with an empty "
                            f"string. Allowed key vocabulary (use EXACTLY "
                            f"these spellings, no synonyms): "
                            + ", ".join(_nested_subfields)
                        )
                    for _nk in _nested_subfields:
                        _ng = _nested_subfield_guidance.get(_nk, "")
                        if _ng:
                            user_lines.append(f"    - `{_nk}`: {_ng}")

    # Block 5/6: row-id iteration set for derived-from-artifact row_sources.
    if (node.type == "row_field"
            and (schema.get("row_source") or "") == "dst_dps"
            and row_ids):
        user_lines.append(_dst_dps_row_iteration_block(
            row_ids, resolved_deps,
            field_name=node.metadata.get("field_name", "")
        ))

    # Block 7: tail.
    user_lines.append(f"=== WRITE THE VALUE FOR `{fname}` NOW ===")
    return sys_prompt, "\n\n".join(user_lines)


def _build_batched_field_prompt(
    nodes: list,
    schema: dict,
    args_context: str,
    resolved_deps_by_node: dict,
    row_ids: list | None = None,
    bound_coa_id: str = "",
) -> tuple[str, str, list[str]]:
    """Build ONE (sys_prompt, user_prompt) covering N paragraph_field nodes
    that share a batch_group. The combined prompt dumps the UNION of all
    constituent paragraphs' upstream deps ONCE, then iterates per-paragraph
    guidance + shape spec, and asks the writer for a single JSON object
    whose keys are the paragraph field names.

    Returns (sys, user, [field_name_in_emit_order]) — the third value is the
    list of field names the parser will look for in the response, in the
    same order as the prompt presents them.
    """
    product_name = schema.get("name", "")
    doctrine = (schema.get("doctrine") or {}).get("purpose", "")

    # Collect per-field spec for each node, in deterministic order
    # (alphabetical by field_name so the prompt prefix is stable).
    # Supports both paragraph_field nodes (spec lives in
    # schema["paragraph_fields"] LIST keyed by .name) and row_field nodes
    # (spec lives in schema["row_fields"] DICT keyed by name) — same
    # batch_group machinery applies to both for row-shape products like
    # HVTL / HPTL / CARVER that need analytical row_fields amortized
    # across one LLM call.
    pf_specs: list[dict] = []
    row_field_names: set[str] = set()
    row_fields_map = schema.get("row_fields") or {}
    for n in nodes:
        fname = n.metadata.get("field_name", "")
        if n.type == "row_field" and fname in row_fields_map:
            spec = dict(row_fields_map[fname] or {})
            spec.setdefault("name", fname)
            spec.setdefault("shape", "row_value_map")
            pf_specs.append(spec)
            row_field_names.add(fname)
            continue
        for pf in schema.get("paragraph_fields") or []:
            if isinstance(pf, dict) and pf.get("name") == fname:
                pf_specs.append(pf)
                break
    pf_specs.sort(key=lambda p: p.get("name", ""))
    fnames_in_order = [p.get("name") for p in pf_specs]

    # ----- SYSTEM PROMPT -----
    sys_lines = [
        f"You are the {product_name} batched per-field writer. You write "
        f"MULTIPLE fields in ONE call, sharing the same upstream context.",
        f"Doctrinal purpose: {doctrine}",
        "Hard rules:",
        " - Output ONE JSON object with EXACTLY these top-level keys: "
        + ", ".join(f"`{n}`" for n in fnames_in_order)
        + ". Each value follows the shape declared for that field in its "
          "FIELD GUIDANCE section below.",
        " - No prose outside the JSON object. No markdown code fences. "
          "Bare JSON only.",
        " - Synthesize each field from the UPSTREAM DEPENDENCY PAYLOADS "
          "+ scenario fixture only. DO NOT fabricate identifiers, units, "
          "or events absent from the payloads.",
        " - 🔒 UNIT ID FORMAT IS COUGAAR-VERBATIM. Every unit id you write "
          "(parent_unit, parent fields, kg_id references, ANY id-shaped string) "
          "MUST appear VERBATIM in the upstream cougaar.get_oob units list "
          "(red_units_by_category.*[].id or blue_units_by_category.*[].id). "
          "Cougaar emits IDXRedUnit kbID UUIDs as ids — e.g. "
          "'672436e9-3022-4c7a-b42b-fe55f5e6d7a1', NOT 'U-DN-614TKBDE'. "
          "The 'U-DN-<NAME>' / 'U-BL-<NAME>' format is the LEGACY scenario "
          "fixture format — it no longer exists in the data. DO NOT "
          "synthesize ids from unit names — never take '614 TK BDE' and "
          "emit 'U-DN-614TKBDE', that is the most common fabrication and "
          "the validator REJECTS it. If you need to reference a unit by id, "
          "copy the kbID UUID from the payload verbatim. If the id you "
          "want is genuinely not in the payload, fall back to plain English "
          "by unit name: 'the 60 MECH IFV DIV's sustainment brigade'. The "
          "validator REJECTS the entire response on any single fabricated "
          "id — every COA with even one made-up id has to be re-fired.",
        " - NEVER emit redaction or classification placeholders (no "
          "[REDACTED], [CLASSIFIED], XXXXX, etc.). Every grid, unit ID, "
          "and identifier in this exercise is published in full; if a "
          "value is genuinely unknown, write 'Not reported' or leave blank.",
        " - NEVER append `_provenance` or kbID citations to a field's "
          "content. The runner stamps `_provenance` as a separate field.",
        " - Sibling top-level keys in your JSON output are written by THIS "
          "call (above) — do NOT duplicate their content across fields. Keep "
          "each field's content narrowly scoped to its FIELD GUIDANCE.",
    ]
    # Universal + per-product hard rules from templates.common
    # HARD_RULES_REGISTRY, appended LAST (mirrors _build_field_prompt).
    try:
        from templates.common import hard_rules_for as _hard_rules_for
        _registry_rules = _hard_rules_for(product_name, schema.get("hard_rules") or [])
    except Exception:
        _registry_rules = []
    if _registry_rules:
        sys_lines.append("")
        sys_lines.append("---")
        sys_lines.append("HARD RULES — read these LAST, apply to every field you emit:")
        for _rule in _registry_rules:
            sys_lines.append("")
            sys_lines.append(_rule)
    sys_prompt = "\n".join(sys_lines)

    # ----- USER PROMPT -----
    # Union of all deps. Each node may have filtered its deps via
    # _apply_field_filters — apply per-node filter, then union.
    union_deps: dict[str, Any] = {}
    for n in nodes:
        resolved_for_node = resolved_deps_by_node.get(n.id) or {}
        filtered = _apply_field_filters(n, schema, resolved_for_node, bound_coa_id)
        for dep_id, payload in filtered.items():
            if dep_id not in union_deps:
                union_deps[dep_id] = payload

    user_lines: list[str] = []
    user_lines.append(_scenario_fixture_block())
    if args_context:
        user_lines.append(f"=== ANALYST CONTEXT ===\n{args_context}\n")
    # Same composition_rollup strip as _build_field_prompt. expand_to_
    # equipment products (HVTL / HPTL) author per-equipment-type rows
    # and the post-merge fan-out maps to per-bearer instances; no
    # batched-field node needs the 234K per-unit rollup dump. Strip
    # it across the whole union of deps. Mutate a COPY so concurrent
    # waves don't see the slim view.
    _BATCHED_UNIT_KEEP = {
        "id", "name", "category", "echelon", "location_mgrs",
        "parent", "doi", "kbID", "sidc",
    }
    if schema.get("expand_to_equipment"):
        for k in ("cougaar.get_oob_red", "cougaar.get_oob"):
            dep = union_deps.get(k)
            if isinstance(dep, dict) and isinstance(dep.get("units"), list):
                pruned_units = []
                for u in dep["units"]:
                    if isinstance(u, dict):
                        slim = {kk: vv for kk, vv in u.items()
                                if kk in _BATCHED_UNIT_KEEP}
                        pruned_units.append(slim)
                    else:
                        pruned_units.append(u)
                new_dep = dict(dep)
                new_dep["units"] = pruned_units
                new_dep["_units_composition_rollup_stripped"] = (
                    "see VALID NOMENCLATURES enumeration in the "
                    "selection field's prompt — raw composition_rollup "
                    "dropped to save ~230K tokens per batched call."
                )
                union_deps[k] = new_dep
    if union_deps:
        user_lines.append("=== UPSTREAM DEPENDENCY PAYLOADS ===")
        ordered_deps = dict(sorted(union_deps.items()))
        user_lines.extend(_summarize_all_deps(ordered_deps))

    # Per-paragraph guidance sections.
    for pf in pf_specs:
        fname = pf.get("name", "")
        shape = pf.get("shape", "string")
        guidance = pf.get("guidance", "") or ""
        subfields = pf.get("subfields") or []
        item_subfields = pf.get("item_subfields") or []
        section = [f"=== FIELD `{fname}` (shape={shape}) ==="]
        if guidance:
            section.append(guidance)
        if shape == "row_value_map" or fname in row_field_names:
            # Row-shape batched output. The writer emits a JSON object
            # mapping every row id to a value satisfying this field's
            # guidance.
            if row_ids:
                # Writer-selected row-id list (HVTL / any schema declaring
                # row_id_selection_field). Constrain the output to EXACTLY
                # these N ids — no more, no fewer — so the LLM doesn't
                # re-expand back to the full row_source pool.
                _id_list = ", ".join(f"\"{rid}\"" for rid in row_ids)
                section.append(
                    f"Output for `{fname}`: a JSON object with EXACTLY "
                    f"these {len(row_ids)} keys (the analyst-selected "
                    f"row id list, verbatim): {_id_list}. Values are short "
                    f"strings per the guidance above. Do NOT add any "
                    f"other keys; do NOT omit any of these keys. If you "
                    f"have no value for a given row, emit an empty string "
                    f"for that row's key (NOT 'Not reported' or "
                    f"'[ANALYST INPUT REQUIRED]')."
                )
            else:
                section.append(
                    f"Output for `{fname}`: a JSON object whose keys are EVERY "
                    f"row id present in the upstream row_source payload (the "
                    f"OOB units block above; ids are kg-sourced UUIDs / unit "
                    f"designators verbatim), and whose values are short strings "
                    f"per the guidance. Never invent ids — only use ids that "
                    f"appear in the upstream payload. If you have no value for "
                    f"a given row, emit an empty string for that row's key "
                    f"(NOT 'Not reported' or '[ANALYST INPUT REQUIRED]')."
                )
        elif shape == "string":
            section.append(f"Output for `{fname}`: ONE plain prose paragraph "
                            "string. No nested JSON.")
        elif shape == "dict" and subfields:
            section.append(
                f"Output for `{fname}`: a JSON object with EXACTLY these "
                f"keys, in this order, verbatim: {', '.join(subfields)}. "
                "Do NOT rename, translate, or substitute synonyms "
                "(e.g. NEVER emit 'primary' instead of 'immediate', "
                "NEVER 'end_state' instead of 'desired_end_state'). Do "
                "NOT add extra keys. Do NOT omit required keys. Each "
                "value is a short prose string."
            )
        elif shape == "list_of_dicts" and item_subfields:
            keys = [sf.get("name") for sf in item_subfields
                    if isinstance(sf, dict)]
            section.append(
                f"Output for `{fname}`: a JSON array of objects, each "
                f"with EXACTLY these top-level keys verbatim (no "
                f"synonyms, no additions, no omissions): {', '.join(keys)}."
            )
            for sf in item_subfields:
                if isinstance(sf, dict):
                    section.append(
                        f"  - `{sf.get('name')}` "
                        f"({sf.get('render_label','')}, "
                        f"required={sf.get('required', False)}): "
                        f"{sf.get('guidance','')}"
                    )
                    # Nested-dict enforcement: item_subfields whose
                    # value is itself a JSON object with declared
                    # `subfields=[...]` (e.g. `statement`,
                    # `objectives`, `functional_allocation` inside
                    # a COA row) — surface the exact nested keys with
                    # per-key guidance so the writer stops inventing
                    # synonyms ("primary" for "immediate", "outcome"
                    # for skipped "who", etc.) that leave the
                    # renderer looking up canonical keys and finding
                    # nothing.
                    _nested_subfields = sf.get("subfields") or []
                    _nested_subfield_guidance = (
                        sf.get("subfield_guidance") or {}
                    )
                    _nested_strict = sf.get("strict_subfields", True)
                    if _nested_subfields:
                        if _nested_strict:
                            section.append(
                                f"    Inside `{sf.get('name')}`, the value "
                                f"MUST be a JSON object using EXACTLY these "
                                f"keys verbatim (no synonyms, no additions, "
                                f"no omissions): "
                                + ", ".join(_nested_subfields)
                            )
                        else:
                            section.append(
                                f"    Inside `{sf.get('name')}`, the value "
                                f"is a JSON object. The KEY VOCABULARY is a "
                                f"fixed list from which the writer picks the "
                                f"doctrinally-applicable subset per the "
                                f"field guidance above (typically 3-5 of the "
                                f"following depending on operation type). "
                                f"If a key does not apply to this row, OMIT "
                                f"it entirely — do NOT emit it with an "
                                f"empty string. Allowed key vocabulary (use "
                                f"EXACTLY these spellings, no synonyms): "
                                + ", ".join(_nested_subfields)
                            )
                        for _nk in _nested_subfields:
                            _ng = _nested_subfield_guidance.get(_nk, "")
                            if _ng:
                                section.append(
                                    f"      - `{_nk}`: {_ng}"
                                )
        user_lines.append("\n".join(section))

    user_lines.append(
        "=== WRITE THE BATCHED JSON OBJECT NOW ===\n"
        "Top-level keys: " + ", ".join(f"`{n}`" for n in fnames_in_order)
        + ". Bare JSON object only."
    )
    return sys_prompt, "\n\n".join(user_lines), fnames_in_order


async def _fire_one_batch(
    nodes: list,
    schema: dict,
    args_context: str,
    completed: dict,
    thinking: bool,
    dump_dir: "Path | None",
    product_type: str,
    row_ids: list | None = None,
    bound_coa_id: str = "",
) -> list[tuple[str, Any, str | None]]:
    """Fire ONE LLM call covering N paragraph_field nodes sharing both a
    batch_group AND a thinking-mode. Per llm-03 / user directive, thinking-on
    calls must NEVER be batched with thinking-off calls — _run_batched_field_nodes
    splits the input batch by thinking-mode and calls this helper once per
    partition.

    Returns a list of (node_id, result, err_or_None) tuples, one per node.
    """
    from utils.llm import invoke_vllm_chat_async

    # Per-node deps (each batch member's filtered upstream).
    resolved_deps_by_node = {n.id: _resolve_deps(n, completed) for n in nodes}
    sys_p, user_p, fnames_in_order = _build_batched_field_prompt(
        nodes, schema, args_context, resolved_deps_by_node,
        row_ids=row_ids, bound_coa_id=bound_coa_id,
    )

    # Output budget: sum of per-node max_tokens (so a batch of 4 paragraphs
    # at 3k each gets a 12k output budget instead of 3k). Thinking-on doubles
    # per-node budget to absorb the reasoning trace — matches the singleton
    # path's `max_tokens *= 2` behavior in _run_field_node.
    per_node_max = _MAX_TOKENS.get("paragraph_field", 3000)
    if thinking:
        per_node_max *= 2
    max_tokens = max(per_node_max, per_node_max * len(nodes))

    batch_id = "+".join(sorted(n.metadata.get("field_name", n.id) for n in nodes))
    think_tag = "think" if thinking else "nothink"
    batch_label = f"{product_type}.batch.{think_tag}.{batch_id}"

    # llm_calls dump: pre-write the prompt + payload BEFORE the call.
    _dump_payload = {
        "batch_id": batch_id,
        "node_ids": [n.id for n in nodes],
        "fnames_in_order": fnames_in_order,
        "product_type": product_type,
        "thinking_on": thinking,
        "max_tokens": max_tokens,
        "system_prompt": sys_p,
        "user_prompt": user_p,
        "response_message": None,
        "raw_response": None,
        "error": None,
    }
    _dump_path = None
    if dump_dir is not None:
        try:
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", batch_id)[:200]
            _dump_path = dump_dir / f"batch__{think_tag}__{safe}.json"
            _dump_path.write_text(json.dumps(_dump_payload, indent=2, default=str))
        except Exception as _dump_exc:
            log.debug("DAG llm_calls batch pre-dump skipped: %s", _dump_exc)
            _dump_path = None

    try:
        resp = await invoke_vllm_chat_async(
            messages=[
                {"role": "system", "content": sys_p},
                {"role": "user", "content": user_p},
            ],
            # thinking flag is decided per-partition by the caller — see
            # _run_batched_field_nodes. Thinking-off is the default for
            # paragraph_field batches that contain no _THINKING_FIELDS
            # members (preserves wall-clock for the bulk of fields).
            tools=None, thinking=thinking, timeout=1800,
            max_tokens=max_tokens, temperature=0.2,
            gate_label=batch_label,
        )
    except Exception as _call_exc:
        if _dump_path is not None:
            try:
                _err_body = ""
                _resp_obj = getattr(_call_exc, "response", None)
                if _resp_obj is not None:
                    try:
                        _err_body = _resp_obj.text
                    except Exception:
                        _err_body = ""
                _dump_payload["error"] = {
                    "type": type(_call_exc).__name__,
                    "message": str(_call_exc),
                    "response_body": _err_body,
                }
                _dump_path.write_text(json.dumps(_dump_payload, indent=2, default=str))
            except Exception as _dump_exc:
                log.debug("DAG llm_calls batch error-dump skipped: %s", _dump_exc)
        err = f"{type(_call_exc).__name__}: {_call_exc}"
        return [(n.id, None, err) for n in nodes]

    msg = (resp.get("choices") or [{}])[0].get("message", {}) or {}
    content = (msg.get("content") or "").strip()
    if _dump_path is not None:
        try:
            _dump_payload["response_message"] = msg
            _dump_payload["raw_response"] = resp
            _dump_path.write_text(json.dumps(_dump_payload, indent=2, default=str))
        except Exception as _dump_exc:
            log.debug("DAG llm_calls batch post-dump skipped: %s", _dump_exc)
    parsed = _try_json(content)

    parsed = _try_json(content)
    if not isinstance(parsed, dict):
        log.warning("DAG batch %s returned non-dict response (len=%d). "
                    "Tail: %r", batch_label, len(content), content[-300:])
        return [(n.id, None, "batched_response_not_dict") for n in nodes]

    out: list[tuple[str, Any, str | None]] = []
    for n in nodes:
        fname = n.metadata.get("field_name", "")
        if fname not in parsed:
            out.append((n.id, None,
                         f"batched_response_missing_field:{fname}"))
            continue
        val = parsed[fname]
        # Wrap list_of_dicts shape values that may come back as plain lists
        # (already the right shape — no wrap needed).
        out.append((n.id, val, None))
    return out


async def _run_batched_field_nodes(
    nodes: list,
    schema: dict,
    args_context: str,
    completed: dict,
    dump_dir: "Path | None" = None,
    product_type: str = "",
    row_ids: list | None = None,
    bound_coa_id: str = "",
) -> list[tuple[str, Any, str | None]]:
    """Fire batched LLM calls covering N paragraph_field nodes sharing a
    batch_group. Returns a list of (node_id, result, err_or_None) tuples,
    one per node.

    Thinking-mode split: per user / llm-03, thinking-on calls must NEVER be
    batched with thinking-off calls (different sampling pathway, different
    response shape, different wall-clock budget). If the input batch mixes
    members that need thinking with members that don't, we split into two
    partitions and fire them concurrently — one with thinking=True covering
    the _THINKING_FIELDS members, one with thinking=False covering the rest.
    Results are merged back into the original order.
    """
    # Partition by thinking-mode using the module-level _THINKING_FIELDS set.
    think_nodes: list = []
    no_think_nodes: list = []
    for n in nodes:
        fname = n.metadata.get("field_name", "")
        if (product_type, fname) in _THINKING_FIELDS:
            think_nodes.append(n)
        else:
            no_think_nodes.append(n)

    # Fast path: homogeneous batch — one call, no split.
    if not think_nodes:
        return await _fire_one_batch(
            no_think_nodes, schema, args_context, completed,
            thinking=False, dump_dir=dump_dir, product_type=product_type,
            row_ids=row_ids, bound_coa_id=bound_coa_id,
        )
    if not no_think_nodes:
        return await _fire_one_batch(
            think_nodes, schema, args_context, completed,
            thinking=True, dump_dir=dump_dir, product_type=product_type,
            row_ids=row_ids, bound_coa_id=bound_coa_id,
        )

    # Mixed batch — fire both partitions concurrently so wall-clock matches
    # the slower of the two (typically the thinking-on partition).
    think_results, no_think_results = await asyncio.gather(
        _fire_one_batch(
            think_nodes, schema, args_context, completed,
            thinking=True, dump_dir=dump_dir, product_type=product_type,
            row_ids=row_ids, bound_coa_id=bound_coa_id,
        ),
        _fire_one_batch(
            no_think_nodes, schema, args_context, completed,
            thinking=False, dump_dir=dump_dir, product_type=product_type,
            row_ids=row_ids, bound_coa_id=bound_coa_id,
        ),
    )
    # Reassemble in the original input order so caller's node-id ordering
    # survives the split.
    by_id: dict[str, tuple[str, Any, str | None]] = {}
    for tup in think_results:
        by_id[tup[0]] = tup
    for tup in no_think_results:
        by_id[tup[0]] = tup
    return [by_id[n.id] for n in nodes if n.id in by_id]


# ----------------------------------------------------------------------------
# Scenario fixture block — injected into EVERY per-field writer prompt so the
# writer is anchored in scenario truth (AO, grid zone, current DTG, OOB,
# phase concept, key terrain) instead of guessing from training data.
# ----------------------------------------------------------------------------
# Keyed by (active project, canonical now) — scenario data is per
# project/event, so the rendered fixture block is too.
_SCENARIO_FIXTURE_CACHE: dict = {}


def _scenario_fixture_block() -> str:
    """Anchor the per-field writer in scenario truth. Returns a single text
    block prepended to every field prompt.

    Cached per-DTG: the scenario module is mostly static within a process,
    but canonical_now_dtg() advances. Re-render when the DTG changes.
    """
    from utils import scenario
    from handlers import intel_agent as ia

    from utils.project_context import active_project

    try:
        now_dtg = ia.canonical_now_dtg() or ""
    except Exception:
        now_dtg = ""
    cache_key = (active_project(), now_dtg)
    if cache_key in _SCENARIO_FIXTURE_CACHE:
        return _SCENARIO_FIXTURE_CACHE[cache_key]

    terrain = scenario.TERRAIN or {}
    kg = scenario.KG or {}
    project_docs = getattr(scenario, "PROJECT_DOCS", {}) or {}

    aoi = (terrain.get("aoi") or "AO BALTIC RESTORE").strip()
    summary = (terrain.get("summary") or "").strip()
    # Canonical IBL AO bbox — matches handlers/intel_agent.py fallback at
    # tool_render_map. South Estonia, grid zones 34V/35V.
    from utils import scenario as _scenario_for_bbox
    bbox = list(_scenario_for_bbox.AO_BBOX)
    grid_zone = "34V / 35V (southern Estonia)"

    # OPORD body — inlined in scenario.PROJECT_DOCS, so available from cold
    # start (no dependency on parse_and_apply_opord).
    # Contains the phase concept (Phase I-IV), scheme of maneuver, commander's
    # intent, task organization. Writer extracts phase IDs / DTGs / objectives
    # directly from this text rather than waiting for the Gemma4 parser.
    opord_body = ((project_docs.get("OPORD") or {}).get("body") or "").strip()

    # Parsed phase concept — only populated AFTER analyst manually fires
    # scenario_ingest.parse_and_apply_opord (~60s Gemma4 extraction). Empty
    # at cold start; the OPORD body excerpt above is the authoritative
    # fallback. When parsed phases ARE present, they're richer (decisive
    # points + per-phase objectives) so we include both.
    phases = list(kg.get("opord_derived_phases") or [])
    phase_lines: list[str] = []
    for p in phases[:8]:
        if not isinstance(p, dict):
            continue
        pid = p.get("id") or p.get("number") or "?"
        pname = p.get("name") or ""
        s_dtg = p.get("start_dtg") or ""
        e_dtg = p.get("end_dtg") or ""
        dp = p.get("decisive_point") or ""
        phase_lines.append(
            f"  - Phase {pid} {pname} ({s_dtg}—{e_dtg}) decisive point: {dp}"
        )
    phase_block = ("\n".join(phase_lines) if phase_lines
                   else "  (parsed phase slot empty — extract phase IDs / DTGs / objectives from the OPORD body §3.b above)")

    # NAI catalog — analyst-named NAIs in TERRAIN. SITEMP's
    # named_areas_of_interest references these verbatim; downstream
    # consumers (ET, DST, ICM, PIR) inherit the same IDs. Without this,
    # writers invented generic NAI-01/02/03 placeholders.
    nai_catalog = list(terrain.get("nai_catalog") or [])
    nai_lines = [
        f"  - {n.get('id','?')}: {n.get('name','?')} mgrs={n.get('mgrs','?')} "
        f"(tied_to={n.get('tied_to_aa','?')}, branch={n.get('threat_branch','?')}) "
        f"— watching for: {n.get('watching_for','')}"
        for n in nai_catalog if isinstance(n, dict)
    ]

    # DP catalog — FRIENDLY commander decision points implied by the OPORD
    # scheme of maneuver. Doctrinally identified at MDMP Step 3, NOT
    # derived from intel products. Event Template's `associated_dp`
    # column and DST's `decision_points` field both reference these IDs
    # verbatim. Without this, writers invented enemy decisions into
    # DST's decision_text — doctrinally wrong (DPs are FRIENDLY
    # decisions triggered BY enemy activity).
    dp_catalog = list(terrain.get("dp_catalog") or [])
    dp_lines = [
        f"  - {p.get('id','?')}: {p.get('name','?')} (phase={p.get('phase','?')}, "
        f"LTIOV={p.get('ltiov','?')}, owner={p.get('owning_commander','?')}, "
        f"linked_nais={p.get('linked_nais','?')})\n"
        f"      friendly_decision: {p.get('friendly_decision','')}\n"
        f"      triggering_indicator: {p.get('triggering_indicator','')}\n"
        f"      options: {p.get('options','')}"
        for p in dp_catalog if isinstance(p, dict)
    ]

    def _top_units(units: list, echelons: tuple, limit: int) -> list[str]:
        out = []
        for u in units or []:
            if not isinstance(u, dict):
                continue
            if u.get("echelon") not in echelons:
                continue
            out.append(
                f"  - {u.get('name','?')} ({u.get('echelon','?')}) "
                f"{u.get('loc','')} mgrs={u.get('mgrs','')}"
            )
            if len(out) >= limit:
                break
        return out

    f_lines = _top_units(kg.get("unit_blue") or [], ("CORPS", "DIV", "BDE"), 12)
    t_lines = _top_units(kg.get("unit_red") or [],  ("DIV", "BDE"), 14)

    aas = (terrain.get("avenues_of_approach") or [])[:6]
    aa_lines = [
        f"  - {a.get('id','?')}: {a.get('name','?')} "
        f"(width {a.get('width_km','?')}km, route: {a.get('route','')})"
        for a in aas if isinstance(a, dict)
    ]

    keys = (terrain.get("key_terrain") or [])[:8]
    key_lines = [
        f"  - {k.get('name','?')} mgrs={k.get('mgrs','?')}"
        f"{' (decisive)' if k.get('decisive') else ''}: {k.get('value','')}"
        for k in keys if isinstance(k, dict)
    ]

    obs = (terrain.get("obstacles") or [])[:6]
    obs_lines = [
        f"  - {o.get('name','?')} mgrs={o.get('mgrs','?')} "
        f"type={o.get('type','')} effect={o.get('tactical_effect','')}"
        for o in obs if isinstance(o, dict)
    ]

    # OPORD body cap — full body is ~8KB. Cache is per-DTG so we pay this
    # once per scenario-clock tick. vLLM context is 16-32K so even with deps
    # this fits. If a field's prompt overflows, we trim deps not the OPORD.
    opord_excerpt = opord_body[:7000] if opord_body else "(no OPORD loaded — scenario.PROJECT_DOCS['OPORD'] missing)"

    block = (
        "=== SCENARIO FIXTURE (authoritative ground truth — use these values verbatim, do NOT substitute training-data values) ===\n"
        f"Area of Operations: {aoi}\n"
        f"Summary: {summary}\n"
        f"BBox (W,S,E,N): {bbox}\n"
        f"MGRS grid zone for any MGRS coords you emit: {grid_zone}. "
        f"NEVER emit 38S (Iran/Pakistan), 31U (UK), or any other zone — every MGRS in this AO starts with 34V or 35V.\n"
        f"Current DTG for any 'now' / latest-time fields: {now_dtg}. "
        f"NEVER emit 2023 dates (e.g. 20231027T1400Z); the scenario clock is the current DTG above.\n"
        "\n=== OPORD body (verbatim — extract phase concept §3.b, scheme of maneuver §3.c, intent §3.a, friendly OOB §1.b, mission §2 from here) ===\n"
        f"{opord_excerpt}\n"
        "\n=== Parsed phase concept (richer per-phase decisive points / DTGs / objectives — only present when analyst has fired scenario_ingest) ===\n"
        f"{phase_block}\n"
        "\n=== Named Areas of Interest (analyst NAI catalog — use these IDs verbatim, NEVER invent NAI-01/02/03 placeholders) ===\n"
        + ("\n".join(nai_lines) if nai_lines else "  (NAI catalog not seeded — SITEMP should name NAIs by terrain feature)") + "\n"
        "\n=== Friendly Commander Decision Points (DP catalog — use these IDs verbatim; DPs are FRIENDLY commander decisions triggered BY enemy activity, NEVER enemy decisions) ===\n"
        + ("\n".join(dp_lines) if dp_lines else "  (DP catalog not seeded)") + "\n"
        "\n=== Friendly task organization (top-level — use these names verbatim, do not invent 'Mechanized Reserve' / generic 'reserve') ===\n"
        + ("\n".join(f_lines) if f_lines else "  (no friendly OOB seeded)") + "\n"
        "\n=== Threat task organization (top-level — Donovian / OPFOR — use these names verbatim) ===\n"
        + ("\n".join(t_lines) if t_lines else "  (no threat OOB seeded)") + "\n"
        "\n=== Avenues of approach (named, friendly into Donovian band) ===\n"
        + ("\n".join(aa_lines) if aa_lines else "  (none seeded)") + "\n"
        "\n=== Key terrain ===\n"
        + ("\n".join(key_lines) if key_lines else "  (none seeded)") + "\n"
        "\n=== Known obstacles ===\n"
        + ("\n".join(obs_lines) if obs_lines else "  (none seeded)") + "\n"
        "\nIf a field needs an NAI / DP / TPL / target ID that is NOT in the NAI catalog above and NOT in the UPSTREAM DEPENDENCY PAYLOADS below, "
        "DO NOT invent generic placeholders (NAI-01, DP-1, T-001). Leave the field blank or write 'Not reported' "
        "so the gap is explicit and the analyst can fix it.\n"
    )
    _SCENARIO_FIXTURE_CACHE[cache_key] = block
    return block


def _dst_dps_row_iteration_block(row_ids: list, resolved_deps: dict[str, Any],
                                  field_name: str = "") -> str:
    """For `row_source: dst_dps` schemas (PIR today), explicitly enumerate
    each row_id and the matched upstream Decision Support Template
    decision_point's salient fields. Without this the per-column writer
    invents row keys (PIR-01..PIR-N) instead of using PIR-DP-N, and
    column values for question/justification/linked_dp_ids/ltiov come
    back empty because the writer can't tell which DP corresponds to
    which row.

    Pull the DST decision_points list out of resolved_deps['artifact.
    Decision Support Template'].structured_data.decision_points. If
    the dep isn't present, fall back to a row-id-only listing (the
    writer can still emit shape-correct rows; they'll just lack
    DP-anchored content).
    """
    dst_dps: list[dict] = []
    for dep_id, payload in (resolved_deps or {}).items():
        if not isinstance(payload, dict):
            continue
        if dep_id != "artifact.Decision Support Template":
            continue
        sd = payload.get("structured_data") or {}
        dst_dps = list(sd.get("decision_points") or [])
        break
    dp_by_id: dict[str, dict] = {(dp.get("id") or "").strip(): dp
                                  for dp in dst_dps if isinstance(dp, dict)}
    lines = [
        "=== ROW ITERATION SET (use these EXACT row_ids as JSON keys in your emit — do NOT invent PIR-01/02/etc) ==="
    ]
    for rid in row_ids:
        # row_id format is "PIR-<DP-ID>"; recover the DP-ID and look it up.
        dp_id = rid.replace("PIR-", "", 1)
        dp = dp_by_id.get(dp_id) or {}
        if dp:
            lines.append(
                f"  - row_id \"{rid}\"  →  matches DST {dp_id}: "
                f"trigger_NAI={dp.get('trigger_NAI','?')}, "
                f"location={dp.get('location_mgrs','?')}, "
                f"trigger_event={(dp.get('trigger_event','') or '')[:140]!r}, "
                f"decision={(dp.get('decision_text','') or '')[:140]!r}, "
                f"latest_time={dp.get('latest_time','?')}, "
                f"assets_employed={dp.get('assets_employed','?')}, "
                f"effect_on_friendly_COA={(dp.get('effect_on_friendly_COA','') or '')[:140]!r}"
            )
        else:
            lines.append(
                f"  - row_id \"{rid}\"  →  DST {dp_id} (no matching DP "
                f"found in dep payload; emit best-effort from scenario fixture)"
            )
    lines.append("")
    lines.append("Your JSON emit MUST be shaped EXACTLY like this — keys are the row_ids verbatim (PIR-DP-N), values are the column content:")
    lines.append("  {")
    for rid in row_ids:
        lines.append(f"    {rid!r}: \"<your value for this row's `{field_name or '?'}` column>\",")
    lines.append("  }")
    lines.append("")
    lines.append(
        f"DO NOT use DP-1 / NAI-VALGA / any other identifier as a key. "
        f"The {len(row_ids)} row_id keys above are the ONLY legal keys."
    )
    return "\n".join(lines)


def _missing_precursor_names(resolved_deps: dict[str, Any]) -> list[str]:
    """Identify artifact dependencies pre-seeded as empty (no prior emit
    in this session). Returns the doctrinal product name(s) the field
    depends on that weren't found — used to short-circuit the per-field
    LLM call and emit an explicit gap notice naming the missing precursor.
    Subfield-level emptiness on a PRESENT precursor is NOT this case —
    that one legitimately renders as 'No significant activity reported.'
    """
    missing: list[str] = []
    for dep_id, payload in (resolved_deps or {}).items():
        if isinstance(payload, dict) and payload.get("_empty") is True:
            art_name = payload.get("artifact_name") or dep_id
            if art_name and art_name not in missing:
                missing.append(art_name)
    return missing


def _gap_notice_for_field(node: dag_parser.Node, schema: dict,
                            missing_precursors: list[str]) -> Any:
    """Build a shape-correct gap notice for a field whose precursor
    artifacts are missing. The string variant is used as the value for
    every subfield of a dict-shaped paragraph (so the analyst sees the
    same explicit gap in every OE-domain subfield instead of a wall of
    'No significant activity reported.' that hides the real problem).
    """
    fname = node.metadata.get("field_name", "")
    notice = (
        f"[MISSING PRECURSOR: {', '.join(missing_precursors)} — "
        f"generate the upstream artifact{'s' if len(missing_precursors) > 1 else ''} "
        f"before this field can be authored.]"
    )
    if node.type == "header_field":
        return notice
    if node.type == "row_field":
        return notice
    # paragraph_field — emit shape-correct value.
    shape = "string"
    subfields: list[str] = []
    item_subfields: list[dict] = []
    for pf in schema.get("paragraph_fields") or []:
        if isinstance(pf, dict) and pf.get("name") == fname:
            shape = pf.get("shape", "string")
            subfields = list(pf.get("subfields") or [])
            item_subfields = list(pf.get("item_subfields") or [])
            break
    if shape == "dict" and subfields:
        return {sf: notice for sf in subfields}
    if shape == "list_of_dicts" and item_subfields:
        first = {}
        for sf in item_subfields:
            if isinstance(sf, dict):
                first[sf.get("name", "")] = notice
        return [first] if first else []
    return notice


_ROW_SOURCE_TO_PAYLOAD_KEY = {
    "kg_targets_active": "targets",
    "kg_units_active":   "units",
    "kg_red_oob_active": "units",
    "kg_target_single":  "targets",
}


# Mechanical derivations for row_field columns the writer should NEVER
# author. Each token in `derive:*` resolves to a per-row {row_id: value}
# map built from the cougaar payload's existing attributes — no LLM call,
# no prompt construction, no token cost.
#
# Added per the audit finding that 4 Threat Overlay row_field columns
# (weapon_range_kilometers, area_of_operations_radius_meters,
# observation_source, strength) and 1 Threat Description Table column
# (last_observed_dtg) were being LLM-emitted at ~70-85 s of decode per
# column despite being deterministic lookups. Threat Description Table's
# last_observed_dtg also hit max_tokens=6000 ceiling because the LLM
# tried to verbosely justify each row.

# Echelon -> AOR radius in meters. Doctrinal nominal sizes per
# FM 3-90 / FM 3-21.10 (planning factors).
_AOR_RADIUS_BY_ECHELON_M = {
    "DIV": 15000, "DIVISION": 15000,
    "BDE": 5000,  "BRIGADE": 5000, "REGT": 5000, "REGIMENT": 5000,
    "BN": 1500,   "BATTALION": 1500, "SQUADRON": 1500,
    "CO": 500,    "COMPANY": 500, "TROOP": 500, "BTRY": 500, "BATTERY": 500,
    "PLT": 150,   "PLATOON": 150,
}

# Unit class -> nominal weapon range km. Tuned against the scenario
# fixture's `class` field tokens (see scenario.KG.unit_red entries).
_WEAPON_RANGE_KM_BY_CLASS = {
    "arty": 30, "artillery": 30, "fa": 30, "mlrs": 70, "ssm": 300,
    "sam_short": 15, "sam_med": 40, "sam_long": 80, "ada": 15,
    "at": 8, "atgm": 8,
    "mech_inf": 4, "motorized_inf": 4, "inf": 1, "tank": 5, "mech": 4,
    "recon": 2, "cav": 4,
    "engineer": 1, "sustainment": 0, "spt": 0, "signal": 0, "sig": 0,
    "div_hq": 0, "hq": 0, "bde_hq": 0,
    "avn": 200, "aviation": 200, "rotary": 200,
}


def _derive_row_field_value(src: str, schema: dict,
                              resolved_deps: dict[str, Any]) -> dict | None:
    """Resolve a `derive:<token>` row_field source to a {row_id: value}
    map by walking the cougaar payload row attributes — no LLM call.

    Tokens:
      - derive:aor_radius_m       echelon -> _AOR_RADIUS_BY_ECHELON_M
      - derive:weapon_range_km    class   -> _WEAPON_RANGE_KM_BY_CLASS
      - derive:observation_source 'OOB / IDXEnemyOrderOfBattleReport' for
        rows carrying a Cougaar kbID, else 'OOB / scenario fixture' for
        fixture-derived rows. Pre-fix this was a hard constant which
        mis-attributed every fixture row to the Cougaar vertex type.
    """
    token = src.split(":", 1)[1] if ":" in src else ""
    row_source = schema.get("row_source")
    payload_key = _ROW_SOURCE_TO_PAYLOAD_KEY.get(row_source or "")
    rows: list = []
    for _dep_id, payload in (resolved_deps or {}).items():
        if not isinstance(payload, dict):
            continue
        items = payload.get(payload_key) if payload_key else None
        if isinstance(items, list) and items:
            rows = items
            break
    if not rows:
        return {}

    out: dict = {}
    if token == "aor_radius_m":
        for r in rows:
            rid = r.get("id")
            if not rid:
                continue
            ech = (r.get("echelon") or "").strip().upper()
            v = _AOR_RADIUS_BY_ECHELON_M.get(ech)
            if v is not None:
                out[rid] = v
    elif token == "weapon_range_km":
        for r in rows:
            rid = r.get("id")
            if not rid:
                continue
            cls = (r.get("class") or r.get("category") or "").strip().lower()
            v = _WEAPON_RANGE_KM_BY_CLASS.get(cls)
            if v is None:
                # Best-effort substring match — class field has values
                # like 'div_hq' / 'mech_inf' / 'sam_short' that don't
                # match the lookup keys exactly. Walk the lookup table
                # and use the first key that's a substring of cls.
                for k, val in _WEAPON_RANGE_KM_BY_CLASS.items():
                    if k in cls:
                        v = val
                        break
            if v is not None:
                out[rid] = v
    elif token == "observation_source":
        for r in rows:
            rid = r.get("id")
            if not rid:
                continue
            # Cougaar-resolved OOB rows carry the source vertex's kbID
            # (set in cougaar_adapters._fuse_oob -> raw_by_kbid[r["uuid"]]).
            # Fixture-derived rows (scenario.KG.unit_red / unit_blue, used
            # when COUGAAR_BASE_URL is unset OR PYTHO_DISABLE_SCENARIO_FIXTURES
            # is off and cougaar returns nothing) carry no kbID. Emit the
            # accurate source label per row so rendered artifacts don't
            # mis-attribute fixture data to the Cougaar vertex type.
            if r.get("kbID") or r.get("_kbID"):
                out[rid] = "OOB / IDXEnemyOrderOfBattleReport"
            else:
                out[rid] = "OOB / scenario fixture"
    elif token == "placeholder":
        # Skip-LLM placeholder for fields populated by a later runner pass
        # (e.g. HVTL's bearer_unit / count, written by _build_skeleton's
        # expand_to_equipment fan-out). Returns "" per row — non-empty
        # map so _kg_row_field_value's `if not out: return None` (LLM
        # fallback) doesn't fire, but no real value. The expansion pass
        # overwrites with the actual per-equipment value.
        for r in rows:
            rid = r.get("id")
            if rid:
                out[rid] = ""
    else:
        return None
    return out


def _kg_row_field_value(node: dag_parser.Node, schema: dict,
                          resolved_deps: dict[str, Any]) -> dict | None:
    """Transpose a kg-sourced row_field column directly out of the cougaar
    row_source payload. Returns {row_id: value} (the same shape the row_field
    LLM emit would have produced) for source tokens `kg_id` or `kg:<field>`.
    Returns None for analytical / ref / header_alias fields so the caller
    falls through to the normal LLM path.

    The row identifier is the row_source payload row's `id` (this is what
    _derive_row_ids picked up to build the skeleton, so the LLM-emitted
    row_field maps for analytical columns key off the same value).
    """
    spec = (schema.get("row_fields") or {}).get(node.metadata.get("field_name", "")) or {}
    src = spec.get("source", "") or ""
    # Mechanical row fields that derive from per-unit attributes WITHOUT
    # the LLM. Audit found analytical columns like weapon_range_kilometers
    # / area_of_operations_radius_meters / observation_source were
    # LLM-emitted (45-85 s decode per column) when they're pure data
    # lookups. Switching to derive:* short-circuits the LLM call.
    if src.startswith("derive:"):
        return _derive_row_field_value(src, schema, resolved_deps)
    if src != "kg_id" and not src.startswith("kg:"):
        return None
    row_source = schema.get("row_source")
    payload_key = _ROW_SOURCE_TO_PAYLOAD_KEY.get(row_source or "")
    if not payload_key:
        return None
    # Pull the cougaar retrieval payload out of resolved_deps. The dep id
    # is `cougaar.<tool>` per dag_parser; we don't know the tool name here
    # without recomputing the parser's ROW_SOURCE_TO_COUGAAR map, so accept
    # whichever dep payload carries the expected list under payload_key.
    rows: list = []
    for dep_id, payload in (resolved_deps or {}).items():
        if not isinstance(payload, dict):
            continue
        items = payload.get(payload_key)
        if isinstance(items, list) and items:
            rows = items
            break
    if not rows:
        return {}
    if src == "kg_id":
        return {r.get("id"): r.get("id") for r in rows if r.get("id")}
    # kg:<field>
    kg_field = src.split(":", 1)[1]
    # Aliases — schema uses analyst-facing column names; cougaar payload
    # uses _t1_unit's own field names. Without these aliases the kg-source
    # short-circuit returns None for the affected columns, falls through
    # to the LLM, and the writer emits empty strings (the symptom the
    # audit caught on Threat Description Table where 0/222 of these
    # mechanical columns were populated despite the OOB carrying every
    # value verbatim).
    _KG_FIELD_ALIASES = {
        "unit_designation": "name",
        "parent_unit": "parent",
        "posture": "size_posture_activity",
    }
    payload_key_for_field = _KG_FIELD_ALIASES.get(kg_field, kg_field)
    out = {r.get("id"): (r.get(kg_field) if r.get(kg_field) not in (None, "")
                          else r.get(payload_key_for_field))
            for r in rows
            if r.get("id") and (
                r.get(kg_field) not in (None, "")
                or r.get(payload_key_for_field) not in (None, "")
            )}
    # When the cougaar payload carries no value for this kg field across
    # ALL rows (real-world gap — e.g. live KG doesn't populate
    # target.category yet, only the fixture does), fall through to the
    # LLM so the writer can classify from name/description against the
    # field's enum. Returning {} here would lock every row to an empty
    # bucket; returning None lets _run_field_node take the LLM path.
    if not out:
        return None
    return out


def _resolve_lookup_source(src: str, schema: dict,
                            resolved_deps: dict[str, Any],
                            fname: str) -> str:
    """Mechanical resolver for `lookup:<Artifact>.<key>[target_id].<field>`
    sources.

    Resolves the lookup against the upstream artifact_dep payload keyed
    by THIS artifact's target_id (for header_only products like TIDAT).
    Returns '' (empty string) when the matching row is absent — the
    writer LLM is NEVER invoked for these fields. Closes TIDAT's per-
    target AGM echo + HPTL target_number lookup + TMP jdpi_id /
    parent_etf_eid lookup paths.

    Examples:
        lookup:HPTL.rows[target_id].id
        lookup:AGM.agm_rows[target_id].how_engage
        lookup:AGM.agm_rows[target_id].effects_plus_remarks
        lookup:TMP.jdpi_id
        lookup:TMP.parent_etf_eid
    """
    body = src[len("lookup:"):].strip()
    # Parse: <Artifact>.<key>[target_id].<field>  OR  <Artifact>.<field>
    artifact_name = body.split(".", 1)[0]
    rest = body[len(artifact_name) + 1:] if "." in body else ""
    dep_payload = (resolved_deps or {}).get(f"artifact.{artifact_name}")
    if not isinstance(dep_payload, dict):
        return ""
    sd = dep_payload.get("structured_data") or dep_payload
    # Identify the bound target_id from the header/args_context the
    # caller threaded in. Header_only artifacts carry target_id as a
    # header_field; row-shape artifacts use row_ids. We accept either
    # via resolved_deps["bound_target_id"] if the dispatcher seeded it.
    target_id = (resolved_deps or {}).get("bound_target_id") or ""
    if not target_id:
        bound = (resolved_deps or {}).get("bound_target_kg_record")
        if isinstance(bound, dict):
            target_id = bound.get("id") or bound.get("kbID") or ""
    # Pattern 1: <key>[target_id].<field>
    if "[target_id]" in rest:
        key, tail = rest.split("[target_id]", 1)
        field = tail.lstrip(".").strip()
        rows = sd.get(key)
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                rid = row.get("id") or row.get("target_id") or row.get("kbID")
                if rid == target_id:
                    if field == "effects_plus_remarks":
                        eff = row.get("effects") or ""
                        rem = row.get("remarks") or ""
                        return (eff + " " + rem).strip()
                    val = row.get(field)
                    return val if val is not None else ""
        elif isinstance(rows, dict):
            # agm_rows may be dict-keyed by target_id directly
            row = rows.get(target_id)
            if isinstance(row, dict):
                if field == "effects_plus_remarks":
                    eff = row.get("effects") or ""
                    rem = row.get("remarks") or ""
                    return (eff + " " + rem).strip()
                val = row.get(field)
                return val if val is not None else ""
        return ""
    # Pattern 2: <field> directly on the upstream artifact's
    # structured_data root or header (TMP.jdpi_id pattern).
    field_path = rest.strip()
    if field_path:
        # Try root, then header.
        val = sd.get(field_path)
        if val is None and isinstance(sd.get("header"), dict):
            val = sd["header"].get(field_path)
        return val if val is not None else ""
    return ""


# Module-level thinking-on opt-in set. Both the singleton path
# (_run_field_node) and the batched path (_run_batched_field_nodes) consult
# this to decide thinking=True vs thinking=False per field. Membership check
# is (product_type, field_name). Keep this set small — every entry doubles
# wall-clock for that field. A batch that mixes thinking-on and thinking-off
# members is split into two passes by _run_batched_field_nodes; per user,
# thinking-on calls must NEVER be batched with thinking-off calls.
_THINKING_FIELDS: set[tuple[str, str]] = {
    ("MCOO", "avenues_of_approach"),
}


async def _run_field_node(node: dag_parser.Node, schema: dict, args_context: str,
                          resolved_deps: dict[str, Any],
                          row_ids: list | None = None,
                          dump_dir: "Path | None" = None,
                          product_type: str = "",
                          bound_coa_id: str = "") -> Any:
    """Build the prompt, call vLLM async, parse the response into a value
    appropriate for the field's shape.

    Short-circuits BEFORE the LLM call when any precursor artifact this
    field depends on is missing (pre-seeded `_empty: True` by the runner).
    Returning a "No significant activity reported." paragraph in that case
    masks an integration error (the analyst didn't generate the upstream
    product yet) as if it were a legitimate empty period. Instead we emit
    an explicit "[MISSING PRECURSOR: <name>]" gap notice naming every
    missing artifact, so the analyst sees the broken dependency at a glance.
    Shape-correct: string fields get the gap notice as the value; dict
    fields get the gap notice as the value of every required subfield.

    Row_field nodes with `source: kg_id` or `source: kg:<field>` skip the
    LLM entirely — they just transpose the corresponding column out of the
    row_source cougaar payload that already loaded in an earlier wave. This
    prevents the writer from hallucinating identifiers / category enums /
    locations that the upstream KG already provides verbatim.
    """
    missing_precursors = _missing_precursor_names(resolved_deps)
    if missing_precursors:
        return _gap_notice_for_field(node, schema, missing_precursors)

    # Runner-populated header fields. spatial_overlay_url/map_id/bbox
    # (HEADER_SPATIAL_OVERLAY_*) declare `source: "tool:render_map"` because
    # the geo-agent render PNG URL + map UUID + bbox are intel_agent
    # _build_skeleton's responsibility, not the writer's. session-scoped
    # fields (area_of_operations, classification) are scenario constants
    # the runner pulls from the OPORD/fixture. session.user is the analyst
    # identity. None of these can be authored by the writer LLM; skip the
    # dispatch entirely and let the runner-side fill in _build_skeleton
    # win unopposed. Earlier the LLM produced "Not reported" for AO every
    # generation because the writer has no scenario-context handle.
    if node.type == "header_field":
        fname = node.metadata.get("field_name", "")
        src = (schema.get("header_fields") or {}).get(fname, {}).get("source", "")
        if isinstance(src, str) and (
                src.startswith("tool:")
                or src == "session"
                or src == "session.user"
                or src == "computed"):
            return None
        # ref:<Name> header sources — runner-resolve from the artifact_dep
        # payload instead of dispatching to the LLM. Without this the
        # writer was inventing values (Event Template's sitemp_artifact_ids
        # got filled with NAI IDs because "SITEMP" was in scope but the
        # writer doesn't reliably extract artifact_id from the dep payload
        # vs picking up surface tokens). The artifact_dep resolver already
        # populated resolved_deps["artifact.<Name>"] with the upstream
        # artifact_id (or PROJECT_DOC:<tag> from the PROJECT_DOCS fallback)
        # — copy that verbatim into the header cell.
        if isinstance(src, str) and src.startswith("ref:"):
            ref_name = src[len("ref:"):].strip()
            dep_payload = (resolved_deps or {}).get(f"artifact.{ref_name}")
            if isinstance(dep_payload, dict):
                aid = dep_payload.get("artifact_id")
                if aid:
                    return aid
            return None
        # kg:<field> / kg_id header sources for header_only artifacts —
        # resolve from the BOUND TARGET KG RECORD payload threaded into
        # the args_context block at dispatch time. Closes TIDAT's 7
        # kg:* header_fields emitting [ANALYST INPUT REQUIRED]. Mirror
        # the row-field alias map at _kg_row_field_value._KG_FIELD_ALIASES
        # so `posture` -> `size_posture_activity` etc.
        if isinstance(src, str) and (src == "kg_id" or src.startswith("kg:")):
            bound = (resolved_deps or {}).get("bound_target_kg_record")
            if not isinstance(bound, dict):
                bound = (resolved_deps or {}).get("cougaar.get_target")
                if isinstance(bound, dict):
                    bound = bound.get("bound_target") or bound
            if isinstance(bound, dict):
                if src == "kg_id":
                    return bound.get("id") or bound.get("kbID")
                kg_field = src.split(":", 1)[1]
                _HEADER_KG_ALIASES = {
                    "posture": "size_posture_activity",
                    "unit_designation": "name",
                    "parent_unit": "parent",
                }
                payload_key = _HEADER_KG_ALIASES.get(kg_field, kg_field)
                val = bound.get(kg_field)
                if val in (None, "") and payload_key != kg_field:
                    val = bound.get(payload_key)
                # For category specifically, fall through to LLM when the
                # KG entity field is blank — matches the row-field
                # `if not out: return None` pattern.
                if val in (None, "") and kg_field == "category":
                    return None
                if val not in (None, ""):
                    return val
                # No bound record or no value — let LLM author as fallback
                # ONLY when the kg field is non-identity (identity fields
                # must NEVER be invented).
                if kg_field in {"name", "id", "location_mgrs", "target_number"}:
                    return None
                return None
        # lookup:<artifact>.<key>[target_id].<field> header sources —
        # mechanical row lookup against an upstream artifact's row list
        # keyed by this artifact's target_id. Returns empty string when
        # the matching row is absent (writer never invents). Wires
        # TIDAT method_of_engagement / method_of_fire_and_control /
        # target_number / jdpi_id / parent_etf_eid as mechanical echoes.
        if isinstance(src, str) and src.startswith("lookup:"):
            return _resolve_lookup_source(
                src, schema, resolved_deps, fname,
            )

    # Map-shape paragraphs are rendered by intel_agent._build_skeleton's
    # render_map loop (PNG written to uploads/<uid>/<aid>/<field_name>.png
    # plus map_overlays[field_name] metadata). The writer LLM has no way to
    # know the geo-agent map id / bbox; returning prose here yields the
    # "Not reported" caption + figure-with-broken-img symptom we see in
    # MCOO. Skip the LLM call; the runner-driven render fills the slot.
    if node.type == "paragraph_field":
        fname = node.metadata.get("field_name", "")
        for pf in (schema.get("paragraph_fields") or []):
            if isinstance(pf, dict) and pf.get("name") == fname:
                if pf.get("shape") == "map":
                    return None
                # producer: "auto" — paragraph is produced mechanically by a
                # post-merge enrichment step (e.g. SITEMP.templated_units lifted
                # from cougaar.get_oob). No writer LLM call: it would burn the
                # 3k-token output budget trying to re-emit hundreds of OOB rows
                # in JSON and truncate mid-stream. The post-merge backfill is
                # the canonical producer; this short-circuit makes that explicit.
                if pf.get("producer") == "auto":
                    return None
                break

    # Short-circuit kg-sourced row_fields: pull from cougaar payload directly.
    if node.type == "row_field":
        kg_value = _kg_row_field_value(node, schema, resolved_deps)
        if kg_value is not None:
            return kg_value
        # dst_dps + source:"kg_id" — transpose row_id directly. No cougaar
        # payload to pull from, but the row_ids are the IDs (PIR-DP-N).
        fname = node.metadata.get("field_name", "")
        spec = (schema.get("row_fields") or {}).get(fname) or {}
        if ((schema.get("row_source") or "") == "dst_dps"
                and spec.get("source") == "kg_id"
                and row_ids):
            return {rid: rid for rid in row_ids}
        # dst_dps + source:"row_id_strip_pir" — return row_id with the
        # "PIR-" prefix stripped (e.g. "PIR-DP-3" → "DP-3"). Lets the
        # linked_dp_ids column populate mechanically instead of asking
        # the LLM to do simple string slicing — the writer kept emitting
        # {} as the value because per-column LLM calls are bad at
        # narrowly-scoped string ops.
        if ((schema.get("row_source") or "") == "dst_dps"
                and spec.get("source") == "row_id_strip_pir"
                and row_ids):
            return {rid: rid.replace("PIR-", "", 1) for rid in row_ids}
        # dst_dps + source:"derive_from_dp_catalog:<field>" — look up the
        # DP from row_id (PIR-DP-N → DP-N), find the matching entry in
        # scenario.TERRAIN.dp_catalog, return dp_catalog[dp_id].<field>
        # for each row. Bypasses LLM for mechanically-derivable PIR
        # columns (linked_nai_ids, ltiov, phase) so the writer's
        # per-column call can't key by NAI-VALGA or other natural-key
        # confusion that creates phantom rows.
        src = spec.get("source") or ""
        if (src.startswith("derive_from_dp_catalog:")
                and (schema.get("row_source") or "") == "dst_dps"
                and row_ids):
            catalog_field = src.split(":", 1)[1]
            from utils import scenario as _scenario
            dp_by_id = {(p.get("id") or "").strip(): p
                        for p in (_scenario.TERRAIN.get("dp_catalog") or [])
                        if isinstance(p, dict)}
            out = {}
            for rid in row_ids:
                dp_id = rid.replace("PIR-", "", 1)
                dp = dp_by_id.get(dp_id) or {}
                val = dp.get(catalog_field, "")
                if val:
                    out[rid] = val
            return out

    from utils.llm import invoke_vllm_chat_async
    sys_p, user_p = _build_field_prompt(node, schema, args_context, resolved_deps,
                                         row_ids=row_ids, bound_coa_id=bound_coa_id)
    # Per-field max_tokens override: schemas may declare `max_tokens` on
    # a paragraph_field / row_field / header_field spec to size the writer
    # call to actual output expectations (PIR's multi-paragraph cells need
    # more headroom; HVTL's terse one-liners need less). Fallback chain:
    #   1. schema's per-field max_tokens (if declared)
    #   2. _MAX_TOKENS[node.type] (type-level default)
    #   3. 400 (final safety net)
    _field_spec = None
    _fname_lookup = node.metadata.get("field_name", "") if node.type in (
        "paragraph_field", "header_field", "row_field"
    ) else ""
    if _fname_lookup:
        if node.type == "paragraph_field":
            for _pf in (schema.get("paragraph_fields") or []):
                if isinstance(_pf, dict) and _pf.get("name") == _fname_lookup:
                    _field_spec = _pf
                    break
        elif node.type == "row_field":
            _field_spec = (schema.get("row_fields") or {}).get(_fname_lookup)
        elif node.type == "header_field":
            _field_spec = (schema.get("header_fields") or {}).get(_fname_lookup)
    _schema_max_tokens = (_field_spec or {}).get("max_tokens") if isinstance(_field_spec, dict) else None
    max_tokens = (
        int(_schema_max_tokens) if isinstance(_schema_max_tokens, int) and _schema_max_tokens > 0
        else _MAX_TOKENS.get(node.type, 400)
    )
    # Per-field thinking opt-in (module-level _THINKING_FIELDS). Default off
    # (preserves existing wall-clock for all fields). Specific paragraphs
    # that need doctrinal reasoning get thinking=True + a fatter max_tokens
    # budget to absorb the reasoning trace. Keep this set small — every
    # entry doubles wall-clock for that field. Membership check is
    # (product_type, field_name).
    field_name = node.metadata.get("field_name", "") if node.type in (
        "paragraph_field", "header_field", "row_field"
    ) else ""
    thinking_on = (product_type, field_name) in _THINKING_FIELDS
    if thinking_on:
        max_tokens *= 2
    # Required row-count for list_of_dicts paragraph_fields that bind to a
    # product_config block (currently: Threat COA Statement coas via
    # product_config.coa_set). The runner re-prompts the writer up to
    # _ROW_COUNT_RETRY_MAX times when len(parsed) != required_row_count.
    # Each re-prompt feeds back "you emitted N; required M, one object per
    # rank 1..M". After the retry budget is exhausted we accept whatever
    # the writer produced (truncated or padded by the post-emit pass) so
    # the dispatch still lands an artifact — better partial than nothing.
    required_row_count: int | None = None
    if node.type == "paragraph_field" and isinstance(_field_spec, dict):
        _rcc = _resolve_row_count_config(schema, _field_spec)
        _rc = _rcc.get("count")
        if isinstance(_rc, int) and _rc > 0 and _field_spec.get("shape") == "list_of_dicts":
            required_row_count = _rc

    _ROW_COUNT_RETRY_MAX = 2
    retry_messages: list[dict] = []
    msg: dict = {}
    content: str = ""
    resp: dict = {}
    attempt = 0
    while True:
        _dump_payload = {
            "node_id": node.id,
            "node_type": node.type,
            "field_name": field_name,
            "product_type": product_type,
            "thinking_on": thinking_on,
            "max_tokens": max_tokens,
            "attempt": attempt,
            "system_prompt": sys_p,
            "user_prompt": user_p,
            "retry_messages": retry_messages,
            "response_message": None,
            "raw_response": None,
            "error": None,
        }
        _dump_path = None
        if dump_dir is not None:
            try:
                safe_id = re.sub(r"[^A-Za-z0-9._-]+", "_", node.id)[:200]
                _suffix = f"__attempt{attempt}" if attempt > 0 else ""
                _dump_path = dump_dir / f"{safe_id}{_suffix}.json"
                _dump_path.write_text(json.dumps(_dump_payload, indent=2, default=str))
            except Exception as _dump_exc:
                log.debug("DAG llm_calls pre-dump skipped for %s: %s", node.id, _dump_exc)
                _dump_path = None
        try:
            resp = await invoke_vllm_chat_async(
                messages=(
                    [{"role": "system", "content": sys_p},
                     {"role": "user", "content": user_p}]
                    + retry_messages
                ),
                tools=None, thinking=thinking_on, timeout=1800,
                max_tokens=max_tokens, temperature=0.2,
                gate_label=node.id,
            )
        except Exception as _call_exc:
            if _dump_path is not None:
                try:
                    _err_body = ""
                    _resp_obj = getattr(_call_exc, "response", None)
                    if _resp_obj is not None:
                        try:
                            _err_body = _resp_obj.text
                        except Exception:
                            _err_body = ""
                    _dump_payload["error"] = {
                        "type": type(_call_exc).__name__,
                        "message": str(_call_exc),
                        "response_body": _err_body,
                    }
                    _dump_path.write_text(json.dumps(_dump_payload, indent=2, default=str))
                except Exception as _dump_exc:
                    log.debug("DAG llm_calls error-dump skipped for %s: %s", node.id, _dump_exc)
            raise
        msg = (resp.get("choices") or [{}])[0].get("message", {}) or {}
        content = (msg.get("content") or "").strip()
        if _dump_path is not None:
            try:
                _dump_payload["response_message"] = msg
                _dump_payload["raw_response"] = resp
                _dump_path.write_text(json.dumps(_dump_payload, indent=2, default=str))
            except Exception as _dump_exc:
                log.debug("DAG llm_calls dump skipped for %s: %s", node.id, _dump_exc)
        if required_row_count is None:
            break
        # Probe the parsed length without committing to a final value yet.
        # _try_json is the same parser the post-loop shape-parse uses.
        _probe = _try_json(content)
        if isinstance(_probe, list) and len(_probe) == required_row_count:
            break
        if attempt >= _ROW_COUNT_RETRY_MAX:
            log.warning(
                "DAG row_count retry exhausted for %s.%s: required=%d got=%s "
                "after %d attempt(s); accepting writer output as-is.",
                product_type, field_name, required_row_count,
                len(_probe) if isinstance(_probe, list) else type(_probe).__name__,
                attempt + 1,
            )
            break
        _got = len(_probe) if isinstance(_probe, list) else "(not a JSON array)"
        log.info(
            "DAG row_count mismatch for %s.%s attempt %d: required=%d got=%s; re-prompting.",
            product_type, field_name, attempt, required_row_count, _got,
        )
        # Carry the prior assistant turn + a user-role feedback frame so the
        # writer sees its own emit AND the explicit count delta. Vllm chat
        # API expects alternating user/assistant; the assistant turn here
        # mirrors what the model just returned.
        retry_messages.append({"role": "assistant", "content": content})
        retry_messages.append({
            "role": "user",
            "content": (
                f"[RUNNER REJECT row_count] You emitted "
                f"{_got} object(s); the schema requires EXACTLY "
                f"{required_row_count}, one per rank 1..{required_row_count}. "
                "Re-emit the JSON array with the correct number of objects. "
                "Author rank-distinct content for each — do not duplicate "
                "an existing COA to pad the count. Return ONLY the JSON "
                "array, no prose around it."
            ),
        })
        attempt += 1
    # NB: don't strip-before-parse for dict/list shapes — the regex could
    # break JSON syntax if it lands mid-value. We strip after parse below
    # (recursively across dict/list leaves) and only strip the raw string
    # for paragraph_field shape=string + header_field + plain content.

    # Parse per the expected shape.
    if node.type == "paragraph_field":
        fname = node.metadata.get("field_name", "")
        shape = "string"
        for pf in schema.get("paragraph_fields") or []:
            if isinstance(pf, dict) and pf.get("name") == fname:
                shape = pf.get("shape", "string")
                break
        if shape == "dict" or shape == "list_of_dicts":
            parsed = _try_json(content)
            if parsed is not None:
                return _map_banned_literals(_strip_inline_provenance(parsed))
            # Malformed JSON for a structured shape. Returning the raw string would
            # be iterated character-by-character downstream (the templated_units
            # "1696 backticks" symptom). Surface the failure with the truncation
            # tail in the log, then return the doctrinally-correct empty container
            # so downstream rendering shows an empty section instead of garbage.
            log.warning(
                "DAG paragraph %s shape=%s: malformed JSON (%d chars). Tail: %r. "
                "Returning empty %s. Bump _MAX_TOKENS or split the field if this recurs.",
                fname, shape, len(content), content[-200:],
                "list" if shape == "list_of_dicts" else "dict",
            )
            return [] if shape == "list_of_dicts" else {}
        return _map_banned_literals(_strip_inline_provenance(content))
    if node.type == "header_field":
        # Strip surrounding quotes if the model emitted them.
        v = content.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        return _map_banned_literals(_strip_inline_provenance(v))
    if node.type == "row_field":
        parsed = _try_json(content)
        if parsed is not None:
            return _map_banned_literals(_strip_inline_provenance(parsed))
        return _map_banned_literals(_strip_inline_provenance(content))
    return _map_banned_literals(_strip_inline_provenance(content))


# Banned-literal filler set — emitted by the writer when no upstream data
# is available. The analyst context block already lists these as BANNED
# but enforcement was prompt-only; the runner accepted any of them. Map
# every banned literal to the '[ANALYST INPUT REQUIRED]' sentinel so the
# gap surfaces in the artifact's gaps[] downstream rather than letting
# filler boilerplate ship silently. 'No significant activity reported'
# is the writer's most common workaround and is included.
_BANNED_OUTPUT_LITERALS_LOWER = {
    "not reported",
    "tbd",
    "unknown",
    "n/a",
    "nil",
    "-",
    "—",
    "no significant activity reported",
    "no significant activity reported.",
}
_ANALYST_INPUT_REQUIRED_SENTINEL = "[ANALYST INPUT REQUIRED]"


def _map_banned_literals(value: Any) -> Any:
    """Map any banned-filler literal in the writer output to the analyst-
    input sentinel. Recurses into dict / list leaves. The sentinel-runner
    already understands '[ANALYST INPUT REQUIRED]' as a gap signal."""
    if isinstance(value, str):
        s = value.strip()
        if s.lower() in _BANNED_OUTPUT_LITERALS_LOWER:
            return _ANALYST_INPUT_REQUIRED_SENTINEL
        return value
    if isinstance(value, list):
        return [_map_banned_literals(v) for v in value]
    if isinstance(value, dict):
        return {k: _map_banned_literals(v) for k, v in value.items()}
    return value


# ----------------------------------------------------------------------------
# Dep resolution helpers
# ----------------------------------------------------------------------------
def _resolve_deps(node: dag_parser.Node, completed: dict[str, Any]) -> dict[str, Any]:
    """Return {dep_id: payload} for every dep that has a value in `completed`.
    Sentinel deps (`__header_alias__:...`, `__unknown_source__:...`) are
    skipped — they were resolved at parse time or are non-fatal warnings.
    Gap-marker deps (RDE-3 — optional upstream that failed) are dropped:
    the field prompt should reflect "no upstream data" rather than feeding
    the writer a __gap__ payload it would try to summarize as if it were
    real evidence."""
    out: dict[str, Any] = {}
    for d in node.depends_on:
        if d.startswith("__"):
            continue
        if d in completed:
            v = completed[d]
            if _is_gap_marker(v):
                continue
            out[d] = v
    return out


# ----------------------------------------------------------------------------
# The main runner
# ----------------------------------------------------------------------------
async def run_dag(product_type: str, args: dict, state, on_event: Callable) -> dict:
    """Generate a doctrinal product via parallel-DAG execution.

    Returns:
        {
          "artifact_id": str,
          "structured_data": dict,   # merged + validated
          "gaps": list[str],
          "turn_total_ms": int,
          "rendered": dict | None,   # {htmlLink, docxLink, pptxLink} or None
          "waves": list[list[str]],  # the wave decomposition (debug)
        }
    Or an {"error": ..., "details": ...} dict on hard failure.
    """
    from templates import PRODUCT_SCHEMAS, PRODUCTS
    from handlers import intel_agent as ia
    from handlers import product as product_handler
    from utils import database

    if on_event is None:
        on_event = lambda e: None  # noqa: E731

    t_start = time.monotonic()
    schema = PRODUCT_SCHEMAS.get(product_type)
    if not schema:
        return {"error": f"unknown_product_type: {product_type}"}

    args_context = (args.get("context") or "").strip()

    # Per-COA narrowing key. When the chain dispatches via a fanout
    # (SITEMP fanout, per-COA Threat COA Statement fanout, per-COA Event
    # Template fanout, ...) state.coa_focus.coa_id is the COA this chain
    # is writing FOR. Threaded down into every field-node prompt build so
    # _apply_field_filters can narrow upstream artifact_dep payloads'
    # coas[] / coa_slides[] arrays to JUST this coa's entry — instead of
    # surfacing every OTHER coa's mission_statement /
    # scheme_of_maneuver / functional_allocation / decisive_engagement /
    # branches_and_sequels / continuing_operations / phased_actions
    # narrative in the prompt. Pre-fix the SITEMP per-COA writer carried
    # all 4 COAs' full narratives on every paragraph_field call, blowing
    # axis_role_assignments alone to 79k tokens for what doctrinally
    # needs ~6k.
    _coa_focus = (getattr(state, "coa_focus", None) or {}) if state is not None else {}
    _bound_coa_id = (_coa_focus.get("coa_id") or "").strip() if isinstance(_coa_focus, dict) else ""

    # 1. Parse the DAG.
    nodes = dag_parser.parse_dag_from_schema(product_type, schema,
                                              known_products=set(PRODUCTS))
    warnings = dag_parser.validate_dag(nodes)
    if warnings:
        # Cycles are a HARD ERROR (RDE-4). A cyclic DAG cannot be
        # topologically sorted, every downstream consumer of compute_waves
        # silently truncates, and the analyst sees a partial product with
        # no indication that the schema is structurally broken. Raise so
        # the dispatcher logs the cycle and rejects the run instead of
        # producing garbage.
        _cycle_msgs = [w for w in warnings if w.startswith("cycle ")]
        if _cycle_msgs:
            raise RuntimeError(
                f"DAG cycle detected in {product_type} schema: "
                + "; ".join(_cycle_msgs)
                + f" (full warnings: {warnings})"
            )
        log.info("DAG warnings for %s: %s", product_type, warnings)
    waves = dag_parser.compute_waves(nodes)
    on_event({"type": "dag_start", "product_type": product_type,
              "node_count": len(nodes), "wave_count": len(waves),
              "waves": waves})

    by_id = {n.id: n for n in nodes}
    completed: dict[str, Any] = {}
    failed: dict[str, str] = {}

    # 2. Pre-seed artifact-dep nodes from the existing state.registry. INTSUM
    #    consumes 8 upstream artifacts; in the bare-minimum path these are
    #    optional — if absent, we mark the dep empty and continue.
    #
    # Most artifacts are rollup-shaped (latest is meaningful: INTSUM, BDA,
    # SITEMP, Threat Overlay etc. — each new emission supersedes the last).
    # KLIEGLIGHT is event-driven: one per perishable SIGINT signal in the
    # current window. Consumers (SITREP Line 11, INTSUM intel block) need
    # ALL fresh KLIEGLIGHTs, not just the latest one. Special-case to pass
    # the full list to event-driven atomics.
    # BDA also flows as ALL_IN_WINDOW for Combat Assessment so the CA
    # writer receives the FULL BDA pool inside the reporting window
    # rather than just the latest one. selected_bda_artifact_ids
    # singleton header_field then enumerates engagements over this set.
    _ALL_IN_WINDOW = {"KLIEGLIGHT", "BDA"}
    reg = state.registry_dict() if state is not None else {}
    # For event-driven atomics (KLIEGLIGHT) the consumer-product session
    # registry is empty — the artifacts live in the `_autodraft` user's
    # pool (autofire writes them there, not into walker's session). When a
    # consumer like SIGSUM/INTSUM/SITREP runs as walker@..., the cross-
    # session pool query is the only source. Resolve it once here.
    _kl_pool: list[dict] = []
    _bda_pool: list[dict] = []
    if state is not None and any(
        n.type == "artifact_dep" and n.metadata.get("artifact_name") in _ALL_IN_WINDOW
        for n in nodes
    ):
        try:
            from handlers.intel_agent import _klieglights_for_user
            user_id = getattr(state, "user_id", "") or ""
            project_id = getattr(state, "project_id", "") or args.get("project_id") or "default"
            _kl_pool = _klieglights_for_user(user_id, project_id)
        except Exception as e:
            log.warning("_ALL_IN_WINDOW cross-session pool lookup failed: %s", e)
        # BDA pool for CA — every BDA artifact inside the reporting
        # window, filtered by header.engagement_dtg if the consumer
        # supplies window bounds via args. Without this CA can only
        # see the latest BDA and selected_bda_artifact_ids has no
        # candidate pool to enumerate.
        try:
            from handlers.intel_agent import _bda_pool_for_user
            user_id = getattr(state, "user_id", "") or ""
            project_id = getattr(state, "project_id", "") or args.get("project_id") or "default"
            window_start = args.get("reporting_period_start") or ""
            window_end = args.get("reporting_period_end") or ""
            _bda_pool = _bda_pool_for_user(
                user_id, project_id, window_start, window_end,
            )
        except Exception as e:
            log.warning("_ALL_IN_WINDOW BDA pool lookup failed: %s", e)

    # Build a name-keyed sidecar of artifact_dep payloads for the from:/path:
    # resolver. dag_executor's `completed` dict is node-id-keyed; resolver
    # reads by upstream product name. This is a side-effect-free index.
    artifact_dep_by_name: dict[str, dict] = {}
    for n in nodes:
        if n.type != "artifact_dep":
            continue
        art_name = n.metadata.get("artifact_name", "")
        items = reg.get(art_name) or ()
        # KLIEGLIGHT (and any future _ALL_IN_WINDOW atomic) fold the cross-
        # session pool into the dep payload when the session registry is
        # empty — autofire stashes them in _autodraft, consumers run as a
        # named analyst, so the registry-only lookup misses them.
        if art_name in _ALL_IN_WINDOW and not items and _kl_pool and art_name == "KLIEGLIGHT":
            # Canonical-on-disk: reload each pool entry's structured_data
            # from the on-disk schema_state.json so analyst-edited KLIEGLIGHTs
            # flow into the SIGSUM / INTSUM consumers.
            items = tuple({
                "artifact_id": p["artifact_id"],
                "structured_data": _load_upstream_structured_data(
                    p.get("artifact_id"),
                    p.get("owner_user_id") or "",
                    p["structured_data"],
                ),
                "dtg": p.get("dtg"),
                "summary": p.get("summary", ""),
            } for p in _kl_pool)
        if art_name == "BDA" and not items and _bda_pool:
            # Fold the in-window BDA pool the same way KLIEGLIGHT does —
            # CA needs the full set of engagements in [start, end], not
            # just the latest one.
            items = tuple({
                "artifact_id": p["artifact_id"],
                "structured_data": _load_upstream_structured_data(
                    p.get("artifact_id"),
                    p.get("owner_user_id") or "",
                    p.get("structured_data") or {},
                ),
                "dtg": p.get("dtg"),
                "summary": p.get("summary", ""),
            } for p in _bda_pool)
        if items and art_name in _ALL_IN_WINDOW:
            # Pre-digest each item into a writer-readable summary line so
            # downstream prompts see signal, not 40 full structured_data
            # blobs that get truncated to 4000 chars by the payload
            # summarizer. Per-KLIEGLIGHT summary surfaces the doctrinal
            # discriminators (emitter / signal_class / freq / modulation /
            # MGRS / intercept time) the SIGSUM writer needs to roll up.
            digested_items = []
            for it in items:
                sd = it.get("structured_data") or {}
                hdr = sd.get("header") or {}
                p1 = sd.get("paragraph_1_emitter_target_identification") or ""
                p2 = sd.get("paragraph_2_time_of_intercept") or ""
                p3 = sd.get("paragraph_3_frequency_and_parameters") or {}
                p4 = sd.get("paragraph_4_geolocation_mgrs") or {}
                freq = (p3.get("frequency") if isinstance(p3, dict) else "") or ""
                modulation = (p3.get("modulation") if isinstance(p3, dict) else "") or ""
                prf = (p3.get("pulse_repetition_frequency") if isinstance(p3, dict) else "") or ""
                mgrs = (p4.get("mgrs") if isinstance(p4, dict) else "") or ""
                summary_line = (
                    f"{it.get('artifact_id', '?')} | emitter={p1[:60]} | "
                    f"TOI={p2[:30]} | freq={freq[:30]} mod={modulation[:20]} "
                    f"PRF={prf[:30]} | mgrs={mgrs[:30]} | "
                    f"klieg#={hdr.get('kliegl_number', '')}"
                )
                digested_items.append({
                    "artifact_id": it.get("artifact_id"),
                    "summary_line": summary_line,
                    "dtg": it.get("dtg"),
                    # Keep structured_data for any consumer that needs it,
                    # but the summary_line is what the writer reads.
                    "structured_data": sd,
                })
            payload = {
                "artifact_name": art_name,
                "count": len(digested_items),
                "summary_lines": [d["summary_line"] for d in digested_items],
                "items": digested_items,
            }
            completed[n.id] = payload
            summary = f"preseeded {len(items)} {art_name} (all-in-window)"
        elif items:
            last = items[-1]
            # Canonical-on-disk: route through _load_upstream_structured_data
            # so analyst edits to the rendered file (persisted to
            # schema_state.json by the finalize trigger) flow into the
            # resolver. Falls back to the writer's emitted structured_data
            # when no on-disk file exists yet.
            _owner = getattr(state, "user_id", "") or ""
            _fresh_sd = _load_upstream_structured_data(
                last.get("artifact_id"), _owner, last.get("structured_data"),
            )
            payload = {
                "artifact_id": last.get("artifact_id"),
                "structured_data": _fresh_sd,
                "dtg": last.get("dtg"),
                "summary": last.get("summary", ""),
            }
            completed[n.id] = payload
            summary = "preseeded"
        else:
            # Current session has no artifact of this type — fall back to
            # the cross-session pool (autodraft + sibling project sessions)
            # for the latest. product_autodraft.ensure_pool_populated
            # seeds every product into the `_autodraft` pool at startup, so
            # this lookup makes the autodraft visible to a fresh MCOO/SITREP
            # generation that would otherwise gap-notice every upstream.
            pool_hit = None
            if state is not None:
                try:
                    from handlers.intel_agent import _latest_artifact_for_user_by_type
                    user_id = getattr(state, "user_id", "") or ""
                    project_id = (getattr(state, "project_id", "")
                                   or args.get("project_id") or "default")
                    pool_hit = _latest_artifact_for_user_by_type(
                        user_id, project_id, art_name,
                    )
                except Exception as e:
                    log.warning(
                        "artifact_dep cross-session pool lookup failed for %s: %s",
                        art_name, e,
                    )
            if pool_hit:
                # Canonical-on-disk: route through
                # _load_upstream_structured_data — pool entries store the
                # writer's emitted structured_data, but the on-disk
                # schema_state.json may carry analyst edits (post-finalize).
                _pool_owner = pool_hit.get("owner_user_id") or ""
                _fresh_pool_sd = _load_upstream_structured_data(
                    pool_hit.get("artifact_id"),
                    _pool_owner,
                    pool_hit.get("structured_data"),
                )
                completed[n.id] = {
                    "artifact_id": pool_hit.get("artifact_id"),
                    "structured_data": _fresh_pool_sd,
                    "dtg": pool_hit.get("dtg"),
                    "summary": pool_hit.get("summary", ""),
                    "_source": "autodraft_pool",
                }
                summary = "preseeded (pool)"
            else:
                # PROJECT_DOCS fallback — OPORD / FRAGO / ANNEX_L_INTEL /
                # RoadToWar / etc. aren't generated artifacts (no autodraft
                # entry), but they ARE available as static scenario docs
                # inlined in scenario.PROJECT_DOCS. If the dep name matches
                # a PROJECT_DOCS tag, populate from there so consumers
                # (ICM's opord_artifact_id, IRE's annex_l_artifact_id, etc.)
                # resolve cleanly instead of surfacing a [MISSING PRECURSOR]
                # gap notice for content that is in fact present.
                doc_hit = None
                try:
                    from utils import scenario as _scenario
                    pd = (getattr(_scenario, "PROJECT_DOCS", None) or {}).get(art_name)
                    if isinstance(pd, dict) and pd.get("body"):
                        doc_hit = pd
                except Exception as e:
                    log.warning(
                        "artifact_dep PROJECT_DOCS fallback failed for %s: %s",
                        art_name, e,
                    )
                if doc_hit:
                    completed[n.id] = {
                        "artifact_id": f"PROJECT_DOC:{art_name}",
                        "structured_data": {
                            "title": doc_hit.get("title", ""),
                            "type":  doc_hit.get("type", ""),
                            "body":  doc_hit.get("body", ""),
                        },
                        "dtg": "scenario-static",
                        "summary": doc_hit.get("title", ""),
                        "_source": "project_docs",
                    }
                    summary = f"preseeded (project_docs:{art_name})"
                else:
                    completed[n.id] = {"_empty": True, "artifact_name": art_name,
                                        "_note": f"no prior {art_name} artifact in session or pool or PROJECT_DOCS"}
                    summary = "empty"
        # Sidecar: populate name-keyed index for the from:/path: resolver.
        if art_name and n.id in completed:
            _pl = completed[n.id]
            if isinstance(_pl, dict):
                artifact_dep_by_name[f"artifact.{art_name}"] = _pl
        on_event({"type": "dag_node_done", "node_id": n.id, "node_type": n.type,
                  "wave": 0, "ok": True, "dur_ms": 0,
                  "summary": summary})

    # Per-call LLM I/O dump directory. Mirrors the tool_results dump
    # convention used later in this function — under
    # uploads/<uid>/<aid>/llm_calls/. Captures system prompt, user prompt,
    # full raw response (so reasoning_content / thinking blocks survive
    # for inspection), and the parsed value. Skipped (None) if
    # pending_artifact_id isn't set yet (some legacy harness paths).
    _llm_calls_dir = None
    try:
        from pathlib import Path as _Path
        from handlers.product import UPLOAD_DIR as _UPLOAD_DIR
        _aid = (getattr(state, "pending_artifact_id", "") or "").strip()
        _uid = (getattr(state, "user_id", "") or "anonymous").strip() or "anonymous"
        if _aid:
            _llm_calls_dir = _Path(_UPLOAD_DIR) / _uid / _aid / "llm_calls"
            _llm_calls_dir.mkdir(parents=True, exist_ok=True)
    except Exception as _e:
        log.debug("DAG llm_calls dump dir setup skipped: %s", _e)

    # For row_source = "dst_dps" the row_ids derive from the upstream
    # Decision Support Template autodraft (independent of wave-computed
    # intel_agent_data), so we can compute them BEFORE the wave loop and
    # feed them into per-field LLM closures via _run_one. Other row_sources
    # (kg_targets_active etc.) need cougaar tool returns and stay late-bound.
    row_ids: list = []
    if (schema.get("row_source") or "") == "dst_dps":
        row_ids = ia._derive_row_ids(product_type, {}, args_context)

    # Writer-controlled row-id selection. When the schema declares
    # `row_id_selection_field: "<header_field_name>"`, the writer emits
    # that header field as a list of kg_ids — and the DAG runner uses
    # THAT list as the row-iteration pool instead of the full row_source
    # pool. HVTL uses this so the published list is SELECTIVE (6-12
    # doctrinally-derived HVTs out of a ~200-unit OOB pool) instead of
    # one row per OOB entry. Resolved at the end of each wave: as soon
    # as the selection field's header_field node completes, subsequent
    # row_field waves iterate over the writer's picks.
    _row_id_selection_field = (schema.get("row_id_selection_field") or "").strip()

    # 3. Execute waves.
    log.info("DAG STEP %s: starting wave execution total_s=%.1f wave_count=%d",
              product_type, time.monotonic() - t_start, len(waves))
    for wave_idx, wave_ids in enumerate(waves):
        # Filter out already-completed nodes (artifact_deps pre-seeded above)
        # and the terminal submit (handled separately).
        pending = [nid for nid in wave_ids
                   if nid not in completed and not nid.endswith(".submit")]
        if not pending:
            continue
        _t_wave = time.monotonic()
        log.info("DAG STEP %s: wave %d START total_s=%.1f pending=%d",
                  product_type, wave_idx, time.monotonic() - t_start, len(pending))

        # Build coroutines for this wave.
        async def _run_one(nid: str):
            n = by_id[nid]
            t0 = time.monotonic()
            on_event({"type": "dag_node_start", "node_id": nid,
                      "node_type": n.type, "wave": wave_idx})
            try:
                if n.type == "cougaar_retrieval":
                    tool = n.metadata.get("tool", "")
                    # Single canonical async-native tool stack — every
                    # DAG cougaar_retrieval calls into handlers.intel_agent
                    # directly. No parallel implementation, no wrappers.
                    # See scratch/ibl/reports/COUGAAR_TOOL_DIVERGENCE_2026-05-26.md
                    # for the 5-day-regression case study that drove the
                    # consolidation. Sync tool_* path was the canonical
                    # design; the prior _call_cougaar shortcut drifted.
                    # Wave-level parallelism already fans every distinct
                    # cougaar_retrieval node out via asyncio.gather below;
                    # the work here is to (a) parallelize the RED/BLUE
                    # sub-fetch inside get_oob (RDE-5) and (b) push the
                    # synchronous tool_* fetchers off the event loop via
                    # asyncio.to_thread so they don't block sibling waves
                    # that are still doing real I/O (RDE-6). The sync
                    # tool_get_terrain / _civil / _environmental /
                    # _spatial_overlay / _list_project_docs / _get_project_doc
                    # are read-only fixture+filter reads with no shared
                    # mutable state between them, so to_thread is safe.
                    from handlers import intel_agent as ia
                    tool_args: dict = {}
                    if tool == "get_oob":
                        # Fire BOTH RED and BLUE IN PARALLEL; merge into one
                        # result. Writer prompts reference both sides (e.g.
                        # Threat COA Statement's functional_allocation cites
                        # friendly formations the threat positions itself
                        # against); seen_ids harvest must cover both sides
                        # too, otherwise any U-BL- reference the writer
                        # emits gets flagged FABRICATED_IDENTIFIERS and the
                        # paragraph fails validation. Per
                        # cougaar_adapters.py:13-15 BOTH RED and BLUE are
                        # first-class consumers across products
                        # (HVTL/CARVER/HPTL/SITEMP/Threat COA Statement/
                        # Threat Overlay consume RED; WEM/SITREP/CCM consume
                        # BLUE); firing both at this single cougaar_retrieval
                        # node keeps schemas from having to declare two
                        # deps. RDE-5: gather instead of sequential await.
                        red, blue = await asyncio.gather(
                            ia.tool_get_oob({"force_type": "RED"}, state),
                            ia.tool_get_oob({"force_type": "BLUE"}, state),
                        )
                        # Merge: concat unit lists, prefer RED's _source +
                        # error fields when both present (RED is the primary
                        # tool consumer; BLUE is always fixture).
                        red_units = (red or {}).get("units") or []
                        blue_units = (blue or {}).get("units") or []
                        result = {
                            **(red or {}),
                            "units": list(red_units) + list(blue_units),
                            "red_count": len(red_units),
                            "blue_count": len(blue_units),
                        }
                    elif tool == "get_oob_red":
                        # RED-only OOB for products whose row pool is enemy units exclusively
                        # (HVTL). Do NOT merge BLUE — including U-BL-* in the HVT candidate
                        # set would put friendly units in the threat row pool. The seen_ids
                        # harvest concern that motivated the merged get_oob branch does not
                        # apply here: HVTL rows are RED units only by definition; any U-BL-
                        # reference the writer emits in a justification cell is harvested
                        # through the artifact_dep on Threat COA Statement (which itself uses
                        # the merged get_oob), not through this retrieval node.
                        result = await ia.tool_get_oob({"force_type": "RED"}, state)
                    elif tool == "list_targets":
                        result = await ia.tool_list_targets(tool_args, state)
                    elif tool == "get_target":
                        result = await ia.tool_get_target(tool_args, state)
                    elif tool == "get_recent_intel":
                        result = await ia.tool_get_recent_intel(tool_args, state)
                    elif tool == "get_environmental":
                        # RDE-6: sync fetcher — offload to default executor.
                        result = await asyncio.to_thread(
                            ia.tool_get_environmental, tool_args, state,
                        )
                    elif tool == "get_terrain":
                        result = await asyncio.to_thread(
                            ia.tool_get_terrain, tool_args, state,
                        )
                    elif tool == "get_civil":
                        result = await asyncio.to_thread(
                            ia.tool_get_civil, tool_args, state,
                        )
                    elif tool == "get_spatial_overlay":
                        result = await asyncio.to_thread(
                            ia.tool_get_spatial_overlay, tool_args, state,
                        )
                    elif tool == "list_project_docs":
                        result = await asyncio.to_thread(
                            ia.tool_list_project_docs, tool_args, state,
                        )
                    elif tool == "get_project_doc":
                        result = await asyncio.to_thread(
                            ia.tool_get_project_doc, tool_args, state,
                        )
                    elif tool == "render_map":
                        # DELIBERATE no-op stub. Several products declare
                        # depends_on:['cougaar.render_map'] on header
                        # spatial_overlay_* fields (see
                        # common.HEADER_SPATIAL_OVERLAY_URL); that dep
                        # exists to wire DAG wave ordering, NOT to fire
                        # an actual render at retrieval time. The real
                        # PNG/JPG render is the post-merge map loop at
                        # `_post_merge_map_fields` below (runs ONCE per
                        # paragraph_field with shape='map' AFTER waves
                        # complete + writer emit lands).
                        # Pre-fix this branch errored with
                        # 'unknown_cougaar_tool: render_map' (benign);
                        # firing the real render here would double-render
                        # every map field, saturate the Chromium CDP pool
                        # (RENDER_CONCURRENCY=4), and queue the
                        # legitimate post-merge renders past their 120s
                        # timeout.
                        result = {"skipped": "render_map_is_post_merge_step"}
                    else:
                        # Last-ditch lookup against the public dispatch
                        # registry — covers any tool registered in
                        # INTEL_AGENT_TOOL_DISPATCH that the hardcoded
                        # elif chain above doesn't yet branch for. Sync
                        # callables get offloaded; async callables are
                        # awaited directly. Keeps the DAG dispatcher in
                        # sync with the tools map without requiring a
                        # twin elif edit every time intel_agent.py adds
                        # a new tool.
                        _disp = getattr(ia, "INTEL_AGENT_TOOL_DISPATCH", None)
                        _fn = (_disp or {}).get(tool)
                        if _fn is None:
                            result = {"error": f"unknown_tool: {tool}"}
                        elif asyncio.iscoroutinefunction(_fn):
                            result = await _fn(tool_args, state)
                        else:
                            result = await asyncio.to_thread(
                                _fn, tool_args, state,
                            )
                elif n.type in ("header_field", "row_field", "paragraph_field"):
                    deps = _resolve_deps(n, completed)
                    result = await _run_field_node(n, schema, args_context, deps,
                                                    row_ids=row_ids,
                                                    dump_dir=_llm_calls_dir,
                                                    product_type=product_type,
                                                    bound_coa_id=_bound_coa_id)
                elif n.type == "paragraph_row_field":
                    # Informational only — emitted by dag_parser so the DAG
                    # view shows item_subfield → cougaar tool inheritance.
                    # The parent paragraph_field's LLM dispatch authors the
                    # whole list_of_dicts; this node carries no value of its
                    # own. Returning a sentinel keeps it out of any
                    # downstream merge.
                    result = {"_subfield_info_only": True,
                               "parent_paragraph": n.metadata.get("parent_paragraph"),
                               "field_name": n.metadata.get("field_name")}
                elif n.type == "artifact_dep":
                    # Shouldn't reach here — pre-seeded above.
                    result = completed.get(nid) or {"_empty": True}
                elif n.type == "scenario_lookup":
                    # Walk app.utils.scenario by the declared dotted path.
                    # e.g. "TERRAIN.battle_positions" → scenario.TERRAIN["battle_positions"]
                    # The scenario module is process-local + cached; no I/O,
                    # no LLM call. dict-getitem semantics throughout (the
                    # scenario module exposes module-level dicts, not nested
                    # objects).
                    from utils import scenario as _scenario_mod
                    path = (n.metadata.get("path") or "").strip()
                    val = None
                    if path:
                        parts = path.split(".")
                        cur = getattr(_scenario_mod, parts[0], None)
                        ok = cur is not None
                        for p in parts[1:]:
                            if isinstance(cur, dict) and p in cur:
                                cur = cur[p]
                            else:
                                ok = False
                                break
                        if ok:
                            val = cur
                    if val is None:
                        log.warning("DAG scenario_lookup miss: %s (path=%r)", nid, path)
                        result = {"_empty": True, "_path": path}
                    else:
                        result = val
                else:
                    result = {"_note": f"node type {n.type} not handled in this wave"}
                dur_ms = int((time.monotonic() - t0) * 1000)
                on_event({"type": "dag_node_done", "node_id": nid,
                          "node_type": n.type, "wave": wave_idx,
                          "ok": True, "dur_ms": dur_ms,
                          "summary": _node_result_summary(n, result)})
                return nid, result, None
            except Exception as e:
                dur_ms = int((time.monotonic() - t0) * 1000)
                log.warning("DAG node %s failed: %s", nid, e, exc_info=True)
                on_event({"type": "dag_node_done", "node_id": nid,
                          "node_type": n.type, "wave": wave_idx,
                          "ok": False, "dur_ms": dur_ms,
                          "error": f"{type(e).__name__}: {e}"})
                return nid, None, f"{type(e).__name__}: {e}"

        # Group paragraph_field nodes by batch_group. Same-group paragraphs
        # get co-fired as ONE LLM call (one combined prompt, JSON response
        # with N paragraph_name keys). Saves the upstream-dep dump cost +
        # the N-1 redundant gate acquires.
        #
        # Singleton groups (one paragraph in a batch) fall through to the
        # existing single-node path so the batch machinery only kicks in
        # when it actually saves work.
        from collections import defaultdict
        _batches: dict[str, list[str]] = defaultdict(list)
        _singletons: list[str] = []
        for nid in pending:
            n = by_id[nid]
            # batch_group co-fires multiple sibling fields (paragraph_field
            # or row_field) into ONE LLM call that emits a combined JSON
            # response. Row-shape products like HVTL benefit just as much
            # as paragraph-shape products: row_fields that share upstream
            # deps (DonovianDoctrine / OOB / Threat COA / scenario terrain)
            # would otherwise each pay the full ~25K-token dep dump and
            # run serially in the DAG wave; batching amortizes the dep
            # dump and saves wall-clock equal to (N-1) * dep-dump-time.
            bg = (n.metadata.get("batch_group") or "") if n.type in ("paragraph_field", "row_field") else ""
            if bg:
                _batches[bg].append(nid)
            else:
                _singletons.append(nid)
        # Demote single-member batches to singletons.
        for bg in list(_batches.keys()):
            if len(_batches[bg]) <= 1:
                _singletons.extend(_batches.pop(bg))

        async def _run_batch_wrapper(bg: str, nids: list[str]):
            t0 = time.monotonic()
            for nid in nids:
                on_event({"type": "dag_node_start", "node_id": nid,
                          "node_type": by_id[nid].type, "wave": wave_idx,
                          "batch_group": bg})
            nodes = [by_id[nid] for nid in nids]
            try:
                outs = await _run_batched_field_nodes(
                    nodes, schema, args_context, completed,
                    dump_dir=_llm_calls_dir, product_type=product_type,
                    row_ids=row_ids, bound_coa_id=_bound_coa_id,
                )
            except Exception as e:
                dur_ms = int((time.monotonic() - t0) * 1000)
                err = f"{type(e).__name__}: {e}"
                log.warning("DAG batch %s failed: %s", bg, e, exc_info=True)
                for nid in nids:
                    on_event({"type": "dag_node_done", "node_id": nid,
                              "node_type": by_id[nid].type, "wave": wave_idx,
                              "ok": False, "dur_ms": dur_ms,
                              "batch_group": bg, "error": err})
                return [(nid, None, err) for nid in nids]
            dur_ms = int((time.monotonic() - t0) * 1000)
            log.info("DAG batch %s DONE dur_ms=%d members=%d",
                      bg, dur_ms, len(nids))
            for nid, result, err in outs:
                on_event({"type": "dag_node_done", "node_id": nid,
                          "node_type": by_id[nid].type, "wave": wave_idx,
                          "ok": err is None, "dur_ms": dur_ms,
                          "batch_group": bg,
                          "summary": _node_result_summary(by_id[nid], result) if err is None else None,
                          "error": err})
            return outs

        coros = [_run_one(nid) for nid in _singletons]
        coros.extend(_run_batch_wrapper(bg, nids) for bg, nids in _batches.items())
        # _run_one returns a single tuple; _run_batch_wrapper returns a list.
        # Normalize both into a flat list of tuples.
        gathered = await asyncio.gather(*coros)
        results: list = []
        for r in gathered:
            if isinstance(r, list):
                results.extend(r)
            else:
                results.append(r)
        for nid, result, err in results:
            n = by_id[nid]
            if err is None:
                completed[nid] = result
            else:
                required = bool(n.metadata.get("required", False))
                if required:
                    # Required field failed — hard fail the run.
                    on_event({"type": "error", "where": nid, "error": err})
                    return {"error": "required_field_failed", "node_id": nid,
                            "detail": err}
                # Optional node failed — record a structured gap marker so
                # downstream consumers (citations, field assemblers,
                # provenance) can render "cougaar fetch failed" notices
                # instead of silently rendering "" and blaming the writer.
                # Consumers that previously checked `completed[k] == ""`
                # also handle `_is_gap_marker(v)` — see _is_gap_marker
                # below.
                failed[nid] = err
                completed[nid] = {
                    "__gap__": True,
                    "reason": f"{n.type} failure",
                    "node": nid,
                    "error": err,
                }
        log.info("DAG STEP %s: wave %d DONE dur_s=%.1f total_s=%.1f failed_optional=%d",
                  product_type, wave_idx, time.monotonic() - _t_wave,
                  time.monotonic() - t_start, sum(1 for nid in pending if nid in failed))

        # Surface progress to the dashboard. After each wave the chain
        # has completed (wave_idx + 1) of len(waves); approximate
        # percent + emit via product_records.record_transition. The WS
        # product_record_upsert broadcast carries the updated
        # source_metadata to the dashboard which renders
        # "Generating... XX%" on the row. Errors logged + swallowed —
        # progress is decorative; the chain runs whether the broadcast
        # lands or not.
        try:
            from utils import llm as _llm
            _sess = getattr(state, "session_id", "") or ""
            _record_id = _llm.record_id_for_session(_sess)
            if _record_id:
                # Token-weighted percent when vllm gate populated
                # _CHAIN_PROGRESS, falls back to wave-counted percent.
                _pct_tokens = _llm.chain_progress_pct(_sess)
                _wave_pct = int(99 * (wave_idx + 1) / max(1, len(waves)))
                _pct = max(_pct_tokens, _wave_pct)
                from handlers import product_records as _pr
                await _pr.record_transition(
                    _record_id,
                    progress_pct=_pct,
                    progress_wave=f"{wave_idx + 1}/{len(waves)}",
                )
        except Exception as _e:
            log.debug("DAG progress broadcast failed: %s", _e)

        # Writer-controlled row-id selection — see _row_id_selection_field
        # comment above. As soon as the wave that emits the selection
        # header_field completes, harvest the writer-emitted kg_id list
        # and override row_ids for subsequent waves. The selection field
        # is a header_field node whose result is the JSON-parsed list of
        # kg_ids; if the writer emitted a non-list (string with commas,
        # null, etc.), normalize to a list and de-duplicate.
        if _row_id_selection_field and not row_ids:
            # Header field node id matches dag_parser's construction at
            # dag_parser.py:378: `{product_type}.header.{fname}` — NOT
            # `header_field:{name}` (the prior wrong guess silently
            # missed the completed result and left row_ids empty, so
            # the row_field batch wave fell back to "every OOB id" and
            # emitted 214 rows instead of the writer's 6-12 selection).
            _sel_node_id = f"{product_type}.header.{_row_id_selection_field}"
            _sel_result = completed.get(_sel_node_id)
            if _sel_result is not None:
                _sel_list: list = []
                if isinstance(_sel_result, list):
                    _sel_list = _sel_result
                elif isinstance(_sel_result, str):
                    s = _sel_result.strip()
                    if s.startswith("["):
                        try:
                            v = json.loads(s)
                            if isinstance(v, list):
                                _sel_list = v
                        except Exception:
                            pass
                    if not _sel_list and s:
                        _sel_list = [x.strip() for x in s.split(",") if x.strip()]
                # Dedup preserving order.
                _seen: set = set()
                row_ids = []
                for v in _sel_list:
                    sv = str(v).strip()
                    if sv and sv not in _seen:
                        _seen.add(sv)
                        row_ids.append(sv)
                log.info(
                    "DAG STEP %s: row_id_selection_field=%s harvested %d ids "
                    "(subsequent row_field waves iterate over this set, not "
                    "the full row_source pool)",
                    product_type, _row_id_selection_field, len(row_ids),
                )

    # 4. Build intel_agent_data from completed cougaar nodes so _build_skeleton
    #    can reuse the existing skeleton-builder logic (which expects an
    #    intel_agent_data dict keyed by tool name).
    #
    #    Also:
    #    - Harvest seen_ids from every cougaar payload so _validate_emit's
    #      detect_fabricated_ids() doesn't flag legitimate KG IDs (C-001..
    #      C-NNN from get_civil, U-DN-NNN from get_oob, etc.) as fabricated.
    #    - Dump each result to the product folder
    #      <artifact_dir>/tool_results/<tool>.json so the inputs that
    #      produced this artifact are archived alongside it (canonical
    #      store for "what was the data when this product was generated").
    intel_agent_data: dict[str, list] = {}
    harvested_ids: set[str] = set()
    # Resolve artifact_dir once for the tool-result dumps. Fallback to
    # no-op if pending_artifact_id isn't set (legacy harness paths).
    _artifact_dir = None
    try:
        from pathlib import Path as _Path
        from handlers.product import UPLOAD_DIR as _UPLOAD_DIR
        _aid = (getattr(state, "pending_artifact_id", "") or "").strip()
        _uid = (getattr(state, "user_id", "") or "anonymous").strip() or "anonymous"
        if _aid:
            _artifact_dir = _Path(_UPLOAD_DIR) / _uid / _aid / "tool_results"
            _artifact_dir.mkdir(parents=True, exist_ok=True)
    except Exception as _e:
        log.debug("DAG tool_results dump dir setup skipped: %s", _e)
    for nid, result in completed.items():
        n = by_id.get(nid)
        if not n:
            continue
        # Gap marker (RDE-3) — the node failed; nothing to harvest, dump,
        # or filter. The gap is already in `failed` and rendered as a
        # provenance bit + dispatcher notice.
        if _is_gap_marker(result):
            continue
        if n.type == "cougaar_retrieval":
            tool = n.metadata.get("tool")
            if not tool:
                continue
            # Apply the same global pre-filter that field-level LLM context
            # gets (_apply_global_prefilters at line 421). Without this,
            # _derive_row_ids reads the unfiltered cougaar payload — for
            # cougaar.get_oob that means ALL 13,420 sub-BN org-chart rows
            # become row_ids, every row_field LLM dispatch fans out across
            # all 13,420 cells, and the PPTX renderer builds 13,420 rows
            # of slide cells. The doctrinal ORBAT (BN/BDE/DIV/CORPS/ARMY,
            # plus any sub-BN with intel-grade fields) is ~222 rows.
            filtered = _apply_global_prefilters({f"cougaar.{tool}": result})
            intel_agent_data.setdefault(tool, []).append(
                filtered.get(f"cougaar.{tool}", result)
            )
            try:
                harvested_ids.update(ia.harvest_seen_ids(result))
            except Exception:
                log.debug("DAG seen_ids harvest skipped for %s", tool, exc_info=True)
            # Dump to product folder — canonical archive.
            if _artifact_dir is not None:
                try:
                    (_artifact_dir / f"{tool}.json").write_text(
                        json.dumps(result, default=str, indent=2)
                    )
                except Exception as _e:
                    log.debug("DAG tool_results dump skipped for %s: %s", tool, _e)
        elif n.type == "artifact_dep":
            # Upstream artifact payload (e.g. CDE's artifact.NSL dep brings
            # in NSL rows whose C-### civil entity IDs the CDE writer must
            # reference verbatim in nsl_entities_within_radius). Without
            # this harvest the IDs aren't in seen_ids, and the validator
            # flags them as FABRICATED_IDENTIFIERS.
            try:
                _before = len(harvested_ids)
                harvested_ids.update(ia.harvest_seen_ids(result))
                _added = len(harvested_ids) - _before
                _art_name = n.metadata.get("artifact_name") or n.id
                _has_sd = isinstance(result, dict) and bool(result.get("structured_data"))
                _is_empty = isinstance(result, dict) and bool(result.get("_empty"))
                log.info(
                    "DAG artifact_dep harvest: name=%s ids_added=%d "
                    "has_structured_data=%s is_empty=%s result_keys=%s",
                    _art_name, _added, _has_sd, _is_empty,
                    list(result.keys())[:8] if isinstance(result, dict) else type(result).__name__,
                )
            except Exception:
                log.warning("DAG artifact seen_ids harvest failed for %s",
                             n.metadata.get("artifact_name") or n.id, exc_info=True)
    if harvested_ids:
        state = state.add_seen(harvested_ids)
    log.info("DAG STEP %s: harvest done elapsed_s=%.1f seen_ids=%d",
              product_type, time.monotonic() - t_start, len(harvested_ids))

    # 5. Build the writer-shape structured_data emit from completed field
    #    nodes, then run through the existing _build_skeleton + _merge_pw_emit
    #    + _validate_emit pipeline so we get the same shape the legacy writer
    #    would have produced.
    # Derive row_ids from the schema's row_source via the same logic the
    # legacy operations-expert path uses (_derive_row_ids). HVTL/HPTL/CARVER/
    # etc. are row products — without this their skeletons stay empty and
    # the writer fabricates N/A rows. Skip for row_source="dst_dps" which
    # already computed row_ids before the wave loop (closure access).
    if not row_ids:
        row_ids = ia._derive_row_ids(product_type, intel_agent_data, args_context)
    _t = time.monotonic()
    skeleton = ia._build_skeleton(product_type, intel_agent_data, row_ids, state=state,
                                    defer_map_render=True)
    log.info("DAG STEP %s: _build_skeleton done dur_s=%.1f total_s=%.1f",
              product_type, time.monotonic() - _t, time.monotonic() - t_start)

    # Assemble the emit dict from completed field nodes.
    emit: dict[str, Any] = {"header_analysis": {}}
    # Header fields
    _owner_for_resolve = getattr(state, "user_id", "") or ""
    _header_fields_schema = schema.get("header_fields") or {}
    for n in nodes:
        if n.type != "header_field":
            continue
        fname = n.metadata.get("field_name")
        if not fname or n.id not in completed:
            continue
        v = completed[n.id]
        # Canonical-on-disk from:/path: resolver for HEADER fields.
        # Same mechanism as the item_subfield pass below but at the header
        # tier: when a header_field declares `from:` + `path:`, pull the
        # upstream value mechanically and use it AS the header value. This
        # fixes the Threat COAs 422-on-phase-and-threat_force case:
        #   - phase declares from='Threat COA Statement', path='header.phase'
        #   - threat_force declares from='Threat COA Statement',
        #     path='header.threat' (alias — upstream column is `threat`,
        #     downstream column is `threat_force`; same value, different
        #     render_label).
        # Both default to "" via _build_skeleton's setdefault when source
        # is "session" with no scenario-fixture handle. Without this pass
        # the importer treats "" as missing_required_* and returns 422.
        _hspec = _header_fields_schema.get(fname) or {}
        if isinstance(_hspec, dict) and _hspec.get("from") and _hspec.get("path"):
            try:
                _resolved = _resolve_from_path_subfield(
                    _hspec, artifact_dep_by_name,
                    ambient=None,
                    owner_user_id=_owner_for_resolve,
                )
                if _resolved not in (None, ""):
                    v = _resolved
            except Exception as _e:
                log.warning(
                    "from:/path: resolver header pass failed for %s.%s: %s",
                    product_type, fname, _e,
                )
        # Gap-marker check (RDE-3): an upstream failure stores a structured
        # marker in completed[n.id]. Skip the emit slot; the gap is already
        # recorded in `failed` and a notice will surface via the dispatcher.
        if v == "" or v is None or _is_gap_marker(v):
            continue
        emit["header_analysis"][fname] = v
    # Row fields — assemble rows_analysis by merging per-column row_id→value
    # maps. Each row_field node returns a JSON dict {row_id: value, ...};
    # we transpose into the writer-shape list of {id, ...col_values}.
    rows_by_id: dict[str, dict] = {}
    for n in nodes:
        if n.type != "row_field":
            continue
        fname = n.metadata.get("field_name")
        if not fname or n.id not in completed:
            continue
        cells = completed[n.id]
        # Gap-marker (RDE-3): the row_field node failed upstream — the
        # marker is dict-shaped but carries __gap__=True, NOT row_id keys.
        # Skip the whole column rather than turning the marker keys into
        # bogus row entries.
        if _is_gap_marker(cells):
            continue
        if not isinstance(cells, dict):
            continue
        for row_id, cell_val in cells.items():
            if cell_val in (None, "") or _is_gap_marker(cell_val):
                continue
            row = rows_by_id.setdefault(str(row_id), {"id": str(row_id)})
            row[fname] = cell_val
    if rows_by_id:
        emit["rows_analysis"] = list(rows_by_id.values())

    # Paragraph fields
    # Acquisition timestamp once per emit. The DAG executes in a single
    # synchronous burst (per-field LLM calls fan out, but resolution time
    # for the underlying cougaar tool returns is well within seconds of
    # one another), so stamping `As of <ts>` once is accurate enough for
    # the Sources footer's freshness claim. Anchoring to the scenario
    # clock keeps live-event renders reading consistent DTGs even after
    # the wall clock has drifted.
    try:
        _emit_dtg = ia.canonical_now_dtg()
    except Exception:
        _emit_dtg = ""
    for n in nodes:
        if n.type != "paragraph_field":
            continue
        fname = n.metadata.get("field_name")
        if not fname or n.id not in completed:
            continue
        v = completed[n.id]
        # Runner-populated paragraphs (shape:"map") return None from the
        # field node — skip the emit slot so _build_skeleton's render_map
        # loop is authoritative for the value.
        # Gap-marker (RDE-3): an optional node upstream failed; skip the
        # emit slot — the gap is already recorded in `failed` and the
        # dispatcher will surface a "fetch failed" notice instead of
        # blaming the writer.
        if v is None or _is_gap_marker(v):
            continue
        # Canonical-on-disk from:/path: + source_transform resolver — for
        # list_of_dicts paragraph_fields whose item_subfields carry
        # from:/path: declarations (or a source_transform handler),
        # OVERWRITE each item's matching subfield with the mechanically-
        # resolved value. The writer LLM's emit for those subfields is
        # discarded (analyst told us the upstream is the source of truth,
        # not the writer's regurgitation of it). C8 also short-circuits
        # the writer's PROMPT for these subfields — see _build_field_prompt
        # — so the writer never even authored them in the first place; this
        # pass is what fills the slot.
        try:
            pf_spec = None
            for _pf in (schema.get("paragraph_fields") or []):
                if isinstance(_pf, dict) and _pf.get("name") == fname:
                    pf_spec = _pf
                    break
            if (pf_spec
                    and pf_spec.get("shape") == "list_of_dicts"
                    and isinstance(v, list)):
                _item_subfields = pf_spec.get("item_subfields") or []
                # Pre-resolve subfields that share a common upstream so
                # we don't re-read schema_state.json once per (item × sf).
                for i, item in enumerate(v):
                    if not isinstance(item, dict):
                        continue
                    ambient = {"index": i}
                    # Seed ambient with `<this.K>` keys from item values
                    # so cross-subfield equality lookups work.
                    for k, vv in item.items():
                        if isinstance(vv, (str, int, float)):
                            ambient[f"this.{k}"] = str(vv)
                    for sf in _item_subfields:
                        if not isinstance(sf, dict):
                            continue
                        # source_transform takes precedence over plain
                        # from:/path: — the transform handler reads the
                        # from:/path: upstream value AND any sibling
                        # cougaar dep (e.g. task_org_sidebar joins
                        # functional_allocation × cougaar.get_oob) and
                        # projects to the consuming product's shape.
                        if sf.get("source_transform"):
                            try:
                                resolved = _resolve_source_transform_subfield(
                                    sf, artifact_dep_by_name,
                                    completed=completed,
                                    ambient=ambient, item=item,
                                    owner_user_id=_owner_for_resolve,
                                )
                            except Exception as _te:
                                log.warning(
                                    "source_transform=%r failed for %s.%s[%d]: %s",
                                    sf.get("source_transform"),
                                    fname, sf.get("name"), i, _te,
                                )
                                resolved = None
                            if resolved is not None:
                                item[sf.get("name")] = resolved
                            continue
                        if not sf.get("from") or not sf.get("path"):
                            continue
                        resolved = _resolve_from_path_subfield(
                            sf, artifact_dep_by_name,
                            ambient=ambient,
                            owner_user_id=_owner_for_resolve,
                        )
                        if resolved is not None:
                            item[sf.get("name")] = resolved
        except Exception as _e:
            log.warning("from:/path: resolver post-emit pass failed for %s: %s",
                        fname, _e)
        emit[fname] = v
        # Provenance citation builder. Tools are CARRIERS, not sources —
        # we cite the underlying data records the tool returned. For each
        # cougaar dep, we drill into the payload and extract:
        #   - KG vertex types + entity IDs (kbID / id / _source_vertex)
        #   - OPORD-derived doc tags (_source_doc_tag, _source_fragment)
        #   - Scenario fixture keys when the payload came from the
        #     scenario_authored fallback (no vertex on the row)
        #   - Geo-agent overlay map UUID + cougaar source layer for
        #     geo_agent_* siblings on get_terrain
        # Citations get combined with " | " and acquisition DTG appended.
        # FootnoteRegistry then dedupes across paragraphs.
        prov_bits: list[str] = []
        for d in n.depends_on:
            if d.startswith("cougaar."):
                tool = d.split(".", 1)[1]
                upstream = completed.get(d) or {}
                if not isinstance(upstream, dict):
                    continue
                if _is_gap_marker(upstream):
                    # Cougaar fetch failed — record a gap-flavored
                    # provenance bit so the analyst sees WHY the
                    # paragraph's evidence base is incomplete instead
                    # of getting a citation that looks normal but is
                    # silently missing rows.
                    prov_bits.append(
                        f"cougaar.{tool}: fetch failed "
                        f"({upstream.get('error','unknown error')})"
                    )
                    continue
                bits = _citations_from_cougaar_payload(tool, upstream,
                                                         schema, node=n, fname=fname)
                prov_bits.extend(bits)
            elif d.startswith("artifact."):
                art_name = d.split(".", 1)[1]
                upstream = completed.get(d) or {}
                if _is_gap_marker(upstream):
                    prov_bits.append(
                        f"artifact:{art_name}: fetch failed "
                        f"({upstream.get('error','unknown error')})"
                    )
                    continue
                if isinstance(upstream, dict) and upstream.get("artifact_id"):
                    aid = upstream["artifact_id"]
                    dtg = (upstream.get("dtg") or "").strip()
                    is_pool = (upstream.get("_source") == "autodraft_pool")
                    src_label = "Pytho autodraft pool" if is_pool else "Pytho artifact"
                    dtg_tail = f" — DTG {dtg}" if dtg else ""
                    prov_bits.append(f"{src_label}: {art_name} ({aid}){dtg_tail}")
                elif isinstance(upstream, dict) and not upstream.get("_empty"):
                    prov_bits.append(f"Pytho artifact: {art_name}")
                # Transitive provenance: in addition to the one-hop artifact
                # pointer above, inherit the upstream artifact's underlying
                # ground-truth sources so they roll up into this product's
                # footnotes (and keep rolling at the next hop, since we
                # re-stamp them into this emit's _provenance). The upstream's
                # top-level block is already transitive across its own chain,
                # so reading one hop yields the whole lineage.
                if isinstance(upstream, dict):
                    prov_bits.extend(
                        _flatten_upstream_provenance(
                            upstream.get("structured_data")
                        )
                    )
        # Acquisition DTG once at the tail of the combined citation —
        # avoids repeating it per-segment. Order-preserving dedup so a
        # source that's both a direct cougaar dep and an inherited
        # upstream source isn't repeated in this field's citation string
        # (FootnoteRegistry also dedupes globally, but keep the stored
        # string clean for the audit trail and the next hop's flatten).
        if prov_bits:
            _seen_bits: set[str] = set()
            _uniq_bits = [b for b in prov_bits
                          if not (b in _seen_bits or _seen_bits.add(b))]
            cite = " | ".join(_uniq_bits)
            if _emit_dtg:
                cite += f" — As of {_emit_dtg}"
            emit.setdefault("_provenance", {})[fname] = cite

    # Pre-mint artifact_id (state may already carry pending_artifact_id from
    # the websocket dispatch path; fall back to a fresh mint).
    artifact_id = (getattr(state, "pending_artifact_id", "") or "").strip() \
                  or ia._next_artifact_id(product_type)

    # Stash skeleton on state so _validate_emit can find it (mirrors what the
    # writer's dispatch does).
    state = replace(state,
                    product_writer_skeleton_json=json.dumps(skeleton, default=str),
                    product_writer_last_emit_json="",
                    pending_artifact_id=artifact_id)

    _t = time.monotonic()
    validation = ia._validate_emit(product_type, emit, state)
    log.info("DAG STEP %s: _validate_emit done dur_s=%.1f total_s=%.1f error=%s",
              product_type, time.monotonic() - _t, time.monotonic() - t_start,
              validation.get("error"))
    if validation.get("error"):
        # Persist the raw cougaar response that fed this generation
        # alongside the writer's emit + skeleton so the analyst can
        # audit what the writer was seeing vs what it fabricated. Lands
        # in the autodraft artifact dir at debug_failure.json. Best-
        # effort; never raises into the validation-error return path.
        try:
            from pathlib import Path
            import os as _os
            import json as _json
            base = _os.environ.get("PYTHO_UPLOAD_DIR", "/app/uploads")
            artifact_dir = Path(base) / "_autodraft" / (artifact_id or "unknown")
            artifact_dir.mkdir(parents=True, exist_ok=True)
            # Pull the most recent resolved-OOB snapshot from disk if the
            # adapter wrote one — that's the ground truth the writer was
            # supposed to use. The validation error tells us what kg_ids
            # the writer fabricated; the snapshot tells us which kg_ids
            # actually existed.
            # Ground truth = the ACTIVE project's resolved OOB (the
            # writer's kg_id namespace). None when the event runs on
            # the scenario-KG floor without a fused snapshot.
            raw_resolved_oob = None
            try:
                from utils import oob as _oob
                raw_resolved_oob = _oob.get_red_oob_resolved_sync()
            except Exception:
                pass
            debug_payload = {
                "product_type": product_type,
                "validation_error": validation.get("error"),
                "validation_keys": sorted(validation.keys()),
                "writer_emit": emit,
                "skeleton": skeleton,
                "seen_ids_sample": sorted(list(state.seen_ids))[:50] if state.seen_ids else [],
                "seen_ids_count": len(state.seen_ids) if state.seen_ids else 0,
                "raw_resolved_oob": raw_resolved_oob,
            }
            (artifact_dir / "debug_failure.json").write_text(
                _json.dumps(debug_payload, default=str, indent=2)
            )
            log.warning(
                "DAG STEP %s: failure debug written to %s/debug_failure.json "
                "(validation_error captured + raw cougaar snapshot + skeleton + emit)",
                product_type, artifact_dir,
            )
        except Exception as _debug_e:
            log.warning("debug_failure.json write skipped: %s", _debug_e)
        if callable(on_event):
            on_event({"type": "error", "where": "validate_emit",
                      "error": validation["error"]})
        return {"error": "validation_failed", "detail": validation["error"]}

    # Defensive: validation passed but may not carry "merged" (e.g. when
    # a future _validate_emit variant returns a leaner dict). Default to
    # the writer's emit so the post-emit pipeline can still rank/derive.
    merged_sd = validation.get("merged") or emit
    gaps = validation.get("gaps", []) or []
    dtg = validation.get("dtg", "unknown")

    # Deterministic post-emit pass for paragraph_fields that declare
    # row_count_source. Overwrites flag_field values + narrative_doctrine_ref
    # per the configured product_config rank entries. The writer is told
    # via the system prompt to NOT pick rank->flag bindings; this pass
    # enforces it so any rank-shuffled writer output still lands the
    # doctrinal binding (rank=1 -> most_likely, rank=3 -> most_dangerous,
    # rank=4 -> most_deceptive + SunTzuOPFOR in the IDX-26 coa_set).
    try:
        _rcc_stats = _apply_row_count_config_post_emit(schema, merged_sd)
        if _rcc_stats:
            log.info(
                "DAG STEP %s: row_count_config post-emit applied: %s",
                product_type, _rcc_stats,
            )
    except Exception as _rcc_exc:
        log.warning(
            "DAG STEP %s: row_count_config post-emit pass failed: %s",
            product_type, _rcc_exc, exc_info=True,
        )

    # Post-merge pipeline-derived enrichment. Fields the writer cannot
    # author without fabrication (because the input data has no measurement
    # for them) are computed here from the deterministic side of the
    # pipeline. MCOO avenues_of_approach: width_km and echelon are derived
    # from the planner's per-path corridor_width sampling and the ATP
    # 2-01.3 Table 4-2 mobility-corridor thresholds. Runs BEFORE the map
    # re-render so the writer-overlay extractor (which reads attr_subfields
    # off each row) carries the enriched echelon/width attrs onto the
    # painted chevron.
    if product_type == "MCOO":
        _enrich_map_field = next(
            (pf for pf in (schema.get("paragraph_fields") or [])
             if isinstance(pf, dict) and pf.get("shape") == "map"),
            None,
        )
        _enrich_map_id = None
        if _enrich_map_field and state is not None:
            _enrich_map_id = ia.map_uuid_for(
                product_type,
                getattr(state, "pending_artifact_id", "") or artifact_id,
                _enrich_map_field.get("name") or "map",
            )
        _ft = (((merged_sd or {}).get("header") or {}).get("force_type") or "mechanized")
        try:
            _ench_stats = ia._enrich_mcoo_avenues_of_approach(
                merged_sd, map_id=_enrich_map_id, force_type=_ft,
            )
            log.info(
                "MCOO post-merge enrichment: %s map_id=%s force_type=%s",
                _ench_stats, _enrich_map_id, _ft,
            )
        except Exception as _ench_exc:
            log.warning(
                "MCOO post-merge enrichment failed: %s", _ench_exc, exc_info=True,
            )

    # SITEMP post-merge enrichment. C4 audit finding: writer LLM was
    # emitting templated_units=[] despite OOB having 200+ RED units —
    # the 3000-token paragraph_field budget gets truncated mid-emission
    # on a list this large and the parser drops the whole array. The
    # published SITEMP table then renders zero rows.
    #
    # Doctrinal source per ATP 2-01.3 §6 / Fig 6-3: every threat unit
    # in the OOB is templated onto the terrain. The writer prompt asks
    # for one row per IDXEnemyOrderOfBattleReport vertex; if the writer
    # didn't deliver, we backfill mechanically from the same data
    # source the writer was supposed to read (cougaar.get_oob output
    # already carried in intel_agent_data from the DAG cougaar wave).
    #
    # Identity / echelon / location_mgrs come straight from the unit
    # row; doctrinal-template fields (frontage_km / depth_km / TPME) are
    # left blank — they require the DOCTEMP basis the writer would
    # carry but the mechanical fallback can't. The per-COA SITEMP
    # templating block in tool_render_map (intel_agent.py ~line 5685)
    # then operates on the populated templated_units list to apply
    # writer-driven per-COA position overrides on top of this baseline
    # (when the writer DID emit a row, its location_mgrs wins; for the
    # 95%+ of units the writer skipped, the OOB baseline holds).
    if product_type == "SITEMP":
        # Unconditional — templated_units is producer:"auto" on the schema, so
        # the writer node is skipped entirely. The OOB lift is the canonical
        # producer for this paragraph. Previously the backfill was a safety net
        # that only fired when the writer emitted empty; now it's the primary.
        try:
            oob_units: list[dict] = []
            for resp in (intel_agent_data.get("get_oob") or []):
                if isinstance(resp, dict):
                    oob_units.extend(resp.get("units") or [])
            tu_rows: list[dict] = []
            for u in oob_units:
                if not isinstance(u, dict):
                    continue
                identity = (u.get("name")
                             or u.get("unit_short_name")
                             or u.get("unit_long_name")
                             or "").strip()
                if not identity:
                    continue
                row = {
                    "id": u.get("id") or identity,
                    "identity": identity,
                    "echelon": u.get("echelon") or "",
                    "location_mgrs": (u.get("location_mgrs")
                                       or u.get("mgrs")
                                       or ""),
                    "frontage_km": "",
                    "depth_km": "",
                    "task": "",
                    "purpose": "",
                    "method": "",
                    "end_state": "",
                    "weapon_range_km": "",
                }
                src_id = (u.get("id") or "").strip()
                src_vertex = (u.get("_source_vertex") or "").strip()
                # Cougaar-resolved unit rows carry kbID (the IDXEnemyOrderOfBattleReport
                # vertex UUID set in cougaar_adapters._fuse_oob). Fixture-derived
                # rows (scenario.KG.unit_red / unit_blue used when COUGAAR_BASE_URL
                # is unset or cougaar returns nothing) carry only the scenario unit
                # id (e.g. "U-OCC-7CAA-HQ") and no kbID. Emit the accurate provenance
                # prefix per row so SITEMP provenance citations don't mis-attribute
                # fixture rows to the Cougaar vertex type.
                kbid = (u.get("kbID") or u.get("_kbID") or "").strip()
                if kbid:
                    prov_str = f"IDXEnemyOrderOfBattleReport.kbID={kbid}"
                elif src_id:
                    prov_str = f"scenario_fixture.unit_id={src_id}"
                elif src_vertex:
                    prov_str = f"OOB.{src_vertex}"
                else:
                    prov_str = ""
                if prov_str:
                    row["_provenance"] = {
                        "identity": prov_str,
                        "location_mgrs": prov_str,
                    }
                tu_rows.append(row)
            if tu_rows:
                merged_sd["templated_units"] = tu_rows
                log.info(
                    "SITEMP post-merge enrichment: templated_units "
                    "backfilled from OOB rows=%d (producer:auto)",
                    len(tu_rows),
                )
        except Exception as _sitemp_tu_exc:
            log.warning(
                "SITEMP templated_units backfill failed: %s",
                _sitemp_tu_exc, exc_info=True,
            )

    # Post-merge map re-render — push the writer-emitted geometry
    # (per-COA EAs / NAIs / TPLs / threat OBJs / threat AAs per
    # _WRITER_GEOMETRY_MANIFEST) on top of the scenario_terrain + cougaar
    # layers, then re-render the PNG. The first render in _build_skeleton
    # had no writer-emit available (assembled below from completed nodes
    # AFTER skeleton built); this second pass is what makes per-COA SITEMP
    # maps visually distinct. Two renders per artifact — the second
    # overwrites the first PNG.
    _post_merge_map_fields = [
        pf for pf in (schema.get("paragraph_fields") or [])
        if isinstance(pf, dict) and pf.get("shape") == "map"
    ]
    if _post_merge_map_fields and state is not None:
        for _mp in _post_merge_map_fields:
            _field_name = (_mp.get("name") or "").strip()
            if not _field_name:
                continue
            _t = time.monotonic()
            log.info("DAG STEP %s: post-merge render START field=%s total_s=%.1f",
                      product_type, _field_name, time.monotonic() - t_start)
            try:
                _render_args = {
                    "field_name": _field_name,
                    "format": "image",
                    "map_artifact_id": _field_name,
                }
                if _mp.get("bbox"):
                    _render_args["bbox"] = _mp["bbox"]
                ia.tool_render_map(
                    _render_args, state,
                    intel_data=intel_agent_data,
                    current_emit=merged_sd,
                )
            except Exception as _e:
                log.warning("post-merge writer-overlay render failed: field=%s err=%s",
                             _field_name, _e, exc_info=True)
            # C5 audit finding: structured_data[<field_name>] was landing as
            # None despite the .jpg existing on disk + header.spatial_overlay_url
            # being populated. The paragraph_field node returns None for
            # shape="map" (the LLM never authors a map field), and the
            # writer emit doesn't carry the slot — so the top-level
            # structured_data entry was never stamped.
            #
            # The map's published artifact lives at
            # uploads/<owner>/<artifact_id>/<field_name>.jpg (per the
            # tool_render_map jpg path convention at intel_agent.py
            # ~line 5945); downstream consumers (importer's missing-field
            # check, exporter's per-COA map lookup) expect the filename
            # relative to the artifact dir. Stamp it deterministically
            # after the render so the structured_data entry matches the
            # on-disk reality.
            merged_sd[_field_name] = f"{_field_name}.jpg"
            log.info("DAG STEP %s: post-merge render DONE field=%s dur_s=%.1f total_s=%.1f",
                      product_type, _field_name,
                      time.monotonic() - _t, time.monotonic() - t_start)

    # 6. Register the artifact via the same path the writer uses.
    log.info("DAG STEP %s: post-merge render block done, registering artifact total_s=%.1f",
              product_type, time.monotonic() - t_start)
    entry = {
        "artifact_id": artifact_id,
        "structured_data": merged_sd,
        "dtg": dtg,
        "gaps": gaps,
        "summary": "DAG-generated INTSUM",
        "owner_user_id": getattr(state, "user_id", "") or "",
    }
    _t = time.monotonic()
    state = state.with_artifact(product_type, entry)
    log.info("DAG STEP %s: state.with_artifact done dur_s=%.1f total_s=%.1f",
              product_type, time.monotonic() - _t, time.monotonic() - t_start)

    on_event({"type": "artifact_emitted",
              "artifact_id": artifact_id,
              "type_": product_type,
              "htmlLink": None, "docxLink": None, "pptxLink": None})

    # 7. Persist the turn (SessionDetail row) so render_artifact_files can
    #    load_state and find the artifact. Mirrors what
    #    run_operations_expert_loop does at end of turn.
    db = database.SessionLocal()
    try:
        dispatch_record = {
            "artifact_type": product_type,
            "context": args_context,
            "skeleton": skeleton,
            "row_ids": row_ids,
            "messages": [],
            "emit": {
                "artifact_id": artifact_id,
                "structured_data": merged_sd,
                "dtg": dtg,
                "summary": "DAG-generated INTSUM",
                "gaps": gaps,
            },
            "status": "ok",
            "exit_reason": "dag_submit",
            "timing_ms": int((time.monotonic() - t_start) * 1000),
            "iter_timings": [],
        }
        _t = time.monotonic()
        log.info("DAG STEP %s: save_turn START total_s=%.1f", product_type, time.monotonic() - t_start)
        ia.save_turn(
            db,
            session_id=getattr(state, "session_id", ""),
            project_id=args.get("project_id") or args.get("projectId") or "anon",
            user_id=getattr(state, "user_id", "") or "",
            timestamp=int(time.time() * 1000),
            question=args_context or f"Generate {product_type} (DAG path)",
            answer=f"{product_type} artifact registered: {artifact_id}",
            title=f"{product_type} (DAG)",
            state_after=state,
            state_before_oe_msg_count=len(state.operations_expert_messages),
            intel_agent_dispatches=[],
            product_writer_dispatches=[dispatch_record],
            iter_timings=[],
            turn_total_ms=int((time.monotonic() - t_start) * 1000),
        )
        log.info("DAG STEP %s: save_turn DONE dur_s=%.1f total_s=%.1f",
                  product_type, time.monotonic() - _t, time.monotonic() - t_start)
    except Exception as e:
        log.warning("DAG save_turn failed: %s", e, exc_info=True)
    finally:
        db.close()

    # 8. Render the artifact (HTML + DOCX/PPTX). Same code path the WS
    #    handler uses for legacy writer output.
    rendered = None
    _t = time.monotonic()
    log.info("DAG STEP %s: render_artifact_files START total_s=%.1f",
              product_type, time.monotonic() - t_start)
    try:
        rdb = database.SessionLocal()
        try:
            rendered = product_handler.render_artifact_files(
                session_id=getattr(state, "session_id", ""),
                artifact_id=artifact_id,
                artifact_type=product_type,
                gaps=gaps,
                db=rdb,
            )
        finally:
            rdb.close()
        log.info("DAG STEP %s: render_artifact_files DONE dur_s=%.1f total_s=%.1f status=%s",
                  product_type, time.monotonic() - _t, time.monotonic() - t_start,
                  (rendered or {}).get("status") if isinstance(rendered, dict) else type(rendered).__name__)
        if isinstance(rendered, dict) and rendered.get("status") == "ok":
            on_event({"type": "artifact_emitted",
                      "artifact_id": artifact_id,
                      "type_": product_type,
                      "htmlLink": rendered.get("htmlLink"),
                      "docxLink": rendered.get("docxLink"),
                      "pptxLink": rendered.get("pptxLink"),
                      "xlsxLink": rendered.get("xlsxLink")})
    except Exception as e:
        log.warning("DAG render_artifact_files failed: %s", e, exc_info=True)
        rendered = {"status": "error", "error": str(e)}

    turn_total_ms = int((time.monotonic() - t_start) * 1000)
    on_event({"type": "dag_done",
              "product_type": product_type,
              "artifact_id": artifact_id,
              "turn_total_ms": turn_total_ms,
              "node_count": len(nodes),
              "wave_count": len(waves),
              "failed_optional_nodes": list(failed.keys()),
              "gap_count": len(gaps)})

    return {
        "artifact_id": artifact_id,
        "structured_data": merged_sd,
        "gaps": gaps,
        "turn_total_ms": turn_total_ms,
        "rendered": rendered,
        "waves": waves,
        "failed_optional_nodes": list(failed.keys()),
    }


def _node_result_summary(node: dag_parser.Node, result: Any) -> str:
    """One-line summary of a node result for the event stream."""
    if isinstance(result, dict):
        if result.get("error"):
            return f"error: {result['error'][:80]}"
        if "count" in result:
            return f"count={result['count']}"
        if result.get("_empty"):
            return "empty"
        keys = list(result.keys())[:4]
        return "keys=" + ",".join(keys)
    if isinstance(result, list):
        return f"len={len(result)}"
    if isinstance(result, str):
        return f"len={len(result)}"
    return str(type(result).__name__)

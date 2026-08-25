"""Manual-injects folder watcher — overlays the inline scenario.PROJECT_DOCS
catalog with an OS-level docx folder that the analyst can swap at event time.

The watcher walks `MANUAL_INJECTS_DIR` (default `/app/manual_injects`) every
`MANUAL_INJECTS_POLL_SEC` seconds (default 30). Any `.docx` whose mtime has
changed since last scan gets re-extracted, classified by the local vLLM
(Gemma 4) into a doctrinal `tag` / `type`, and merged into
`scenario.PROJECT_DOCS`. The catalog is mutated in place so existing
consumers (`tool_list_project_docs`, `tool_get_project_doc`,
`scenario_ingest`) see the new bodies without restart.

For event-day swap, point `MANUAL_INJECTS_DIR` at an SMB mount or symlink the
local folder there — anything python can `os.scandir()` works.

Classification: Gemma 4 reads the first 4 KB of body + the filename and
returns JSON `{tag, type, title}` mapped onto the canonical doctrinal set
(OPORD, FRAGO_<n>, RoadToWar, ANNEX_L_INTEL, OOB) — or a fresh tag if no
canonical mapping fits. No fuzzy filename matching: filenames are
unreliable (analysts rename, organizers ship with stems like "IDX Order"
that look nothing like "OPORD"). Content classification is robust and the
LLM only runs on mtime changes (cheap amortized).

Each entry that lands in PROJECT_DOCS carries:
  {title, body, type, source_path, source: "manual_injects", mtime_ns}
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

DEFAULT_DIR = "/app/manual_injects"
DEFAULT_POLL_SEC = 30
# Body chars fed to the classifier prompt. 4 KB is enough for a doctrinal
# discriminator: title / first heading / first sentence carry the type.
_CLASSIFIER_BODY_CHARS = 4000
# On-disk classification cache. Keyed by SHA256 of the docx body so identical
# content across renames or restarts skips the vLLM round-trip.
_CACHE_PATH = Path("/app/data/manual_injects_classification_cache.json")

_STATE_LOCK = threading.Lock()
# filename → mtime_ns of last successful extract.
_LAST_MTIME: dict[str, int] = {}
# tag → source path that owns it. When a docx is removed from the folder we
# evict its entry from PROJECT_DOCS so the catalog matches the live folder.
_TAG_OWNER: dict[str, str] = {}
# Loaded classification cache (sha256 → {tag, type, title}). Read on first
# touch, persisted on every successful classification.
_CLASSIFY_CACHE: dict[str, dict] | None = None


# Canonical doctrinal tags the classifier should prefer when content matches.
# OPORD / FRAGO_<n> / RoadToWar / ANNEX_L_INTEL / OOB are the keys downstream
# consumers (scenario_ingest._INGEST_DOC_TYPES, operations-expert lookups,
# writer schema `data_sources: list_project_docs(filter={type:'OPORD'})`)
# expect. Any document outside this set gets its own tag.
_CANONICAL_TAGS = [
    {"tag": "OPORD",         "type": "OPORD",
     "description": "Operations Order — base order issued by higher HQ. Establishes mission, concept of operations, phases, PIRs, command/signal."},
    {"tag": "FRAGO_<n>",     "type": "FRAGO",
     "description": "Fragmentary Order — amendment to a base OPORD. Headed FRAGO <number>. Pick a stable suffix from the order number (e.g. FRAGO_26-04-A). Multiple FRAGOs may coexist as FRAGO_<n1>, FRAGO_<n2>."},
    {"tag": "RoadToWar",     "type": "RoadToWar",
     "description": "Strategic narrative / Road-to-War / Road-to-Crisis. Describes pre-conflict buildup, force movements, diplomatic posture. Establishes scenario context."},
    {"tag": "ANNEX_L_INTEL", "type": "ANNEX_L_INTEL",
     "description": "OPORD Annex L (Intelligence). Specific intelligence support paragraphs (mission, execution, PIRs collection, IPB schedule)."},
    {"tag": "OOB",           "type": "OOB",
     "description": "Order of Battle — friendly or enemy force composition rollup. Lists units, subordinates, equipment, locations."},
]


def _classifier_system_prompt() -> str:
    catalog = "\n".join(
        f"  - tag={t['tag']!r}, type={t['type']!r}: {t['description']}"
        for t in _CANONICAL_TAGS
    )
    return (
        "You classify intelligence-operations documents into doctrinal "
        "tags so downstream tooling can route them by type. You ALWAYS "
        "output a single JSON object — no prose, no markdown fences, no "
        "preamble.\n\n"
        "Canonical tags:\n"
        f"{catalog}\n\n"
        "Rules:\n"
        " - Prefer a canonical tag whenever the content fits. The filename "
        "is unreliable — read the body. \"IDX Order.docx\" is the IDX 26 "
        "exercise's OPORD; \"ROAD TO WAR.docx\" is the RoadToWar narrative; "
        "etc.\n"
        " - For FRAGOs, replace <n> with a stable identifier from the order "
        "(e.g. FRAGO_26-04-A). Never emit literal '<n>'.\n"
        " - If no canonical tag fits, invent a SHORT alphanumeric tag "
        "(letters / digits / underscores, ≤ 32 chars) reflecting the doc's "
        "subject. Set type to the same value as tag in that case.\n"
        " - title = a short human-readable description (≤ 100 chars).\n\n"
        "Output schema:\n"
        '  {"tag": str, "type": str, "title": str}'
    )


def _strip_json_fences(text: str) -> str:
    """Same trick scenario_ingest uses — Gemma 4 occasionally wraps the JSON
    in ```json fences despite the system prompt. Strip them."""
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        if s.endswith("```"):
            s = s[: -3]
    return s.strip()


def _classify_cache_load() -> dict[str, dict]:
    global _CLASSIFY_CACHE
    if _CLASSIFY_CACHE is not None:
        return _CLASSIFY_CACHE
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as fh:
            _CLASSIFY_CACHE = json.load(fh) or {}
    except FileNotFoundError:
        _CLASSIFY_CACHE = {}
    except Exception as e:
        log.warning("manual_injects: classifier cache load failed (%s); starting empty", e)
        _CLASSIFY_CACHE = {}
    return _CLASSIFY_CACHE


def _classify_cache_save() -> None:
    if _CLASSIFY_CACHE is None:
        return
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_CLASSIFY_CACHE, fh, indent=2)
        tmp.replace(_CACHE_PATH)
    except Exception as e:
        log.warning("manual_injects: classifier cache save failed: %s", e)


async def _classify_with_llm(body: str, filename: str) -> dict | None:
    """Run Gemma 4 on the body to get `{tag, type, title}`. Returns None on
    LLM failure (network down, malformed JSON, missing fields) — caller
    skips this docx and retries next mtime change. Cached by body sha256."""
    cache = _classify_cache_load()
    key = hashlib.sha256(body.encode("utf-8")).hexdigest()
    hit = cache.get(key)
    if hit:
        return hit

    try:
        from utils.llm import invoke_vllm_text_async
    except Exception as e:
        log.error("manual_injects: vllm client import failed: %s", e)
        return None

    excerpt = body[:_CLASSIFIER_BODY_CHARS]
    if len(body) > _CLASSIFIER_BODY_CHARS:
        excerpt += f"\n\n[... {len(body) - _CLASSIFIER_BODY_CHARS} more chars elided for classification ...]"

    messages = [
        {"role": "system", "content": _classifier_system_prompt()},
        {"role": "user",
         "content": (
            f"Filename: {filename}\n\n"
            f"Body (first {_CLASSIFIER_BODY_CHARS} chars):\n{excerpt}\n\n"
            "Classify this document. Output ONLY the JSON object — "
            "no prose, no fences."
         )},
    ]
    try:
        raw = await invoke_vllm_text_async(
            messages, max_tokens=200, temperature=0.0, timeout=60,
        )
    except Exception as e:
        log.error("manual_injects: vllm classifier call failed for %s: %s", filename, e)
        return None

    try:
        parsed = json.loads(_strip_json_fences(raw))
    except Exception as e:
        log.error("manual_injects: classifier returned non-JSON for %s: %s | raw=%r", filename, e, raw[:200])
        return None

    if not isinstance(parsed, dict):
        log.error("manual_injects: classifier returned non-object for %s: %r", filename, parsed)
        return None
    tag = (parsed.get("tag") or "").strip()
    doc_type = (parsed.get("type") or "").strip() or tag
    title = (parsed.get("title") or "").strip() or filename
    if not tag:
        log.error("manual_injects: classifier returned empty tag for %s: %r", filename, parsed)
        return None
    # Defensive: clamp tag to safe identifier chars in case the LLM emitted
    # spaces or punctuation despite the system prompt.
    safe_tag = re.sub(r"[^A-Za-z0-9_-]+", "_", tag).strip("_")
    if not safe_tag:
        log.error("manual_injects: classifier tag %r contains no safe chars; rejecting", tag)
        return None

    result = {"tag": safe_tag, "type": doc_type, "title": title}
    cache[key] = result
    _classify_cache_save()
    log.info("manual_injects: classified %s → tag=%s type=%s (LLM)",
             filename, safe_tag, doc_type)
    return result


def _extract_text(path: Path) -> str:
    """Plain-text extraction from a .docx file using docx2txt (already shipped
    in app/requirements.txt). Returns stripped string; empty on extraction
    failure (logged)."""
    try:
        import docx2txt  # type: ignore
    except ImportError:
        log.error("docx2txt not installed; cannot ingest %s", path)
        return ""
    try:
        text = docx2txt.process(str(path)) or ""
    except Exception as e:
        log.warning("manual_injects: extraction failed for %s: %s", path, e)
        return ""
    # docx2txt preserves paragraph breaks via "\n\n"; collapse to single \n
    # for consistency with the inline PROJECT_DOCS stubs.
    return text.strip()


def _enumerate_docx(dirpath: Path) -> Iterable[Path]:
    """Yield every .docx file directly under `dirpath` (non-recursive).
    Skips files starting with '.' or '~$' (Office lock files)."""
    try:
        entries = sorted(os.scandir(dirpath), key=lambda e: e.name)
    except FileNotFoundError:
        return
    for entry in entries:
        if not entry.is_file():
            continue
        name = entry.name
        if name.startswith(".") or name.startswith("~$"):
            continue
        if not name.lower().endswith(".docx"):
            continue
        yield Path(entry.path)


async def scan_once(dirpath: Path | None = None) -> dict[str, str]:
    """One scan pass. Updates `scenario.PROJECT_DOCS` in place, returns a
    dict of `{tag: filename}` for everything currently loaded. Idempotent.

    Async because the classifier is async (vLLM HTTP via httpx.AsyncClient).
    Callers from sync code use `initial_load()` which wraps with
    `asyncio.run` when there's no running loop, or scheduled via
    `asyncio.create_task` in async startup paths."""
    if dirpath is None:
        dirpath = Path(os.environ.get("MANUAL_INJECTS_DIR", DEFAULT_DIR))

    # The overlay applies to EVERY project's PROJECT_DOCS at scenario
    # load (utils.project_scenario) — the injects folder is a venue
    # surface, not a per-event one.
    from utils import project_scenario as _ps

    with _STATE_LOCK:
        seen_tags: dict[str, str] = {}
        mutated = False
        for docx_path in _enumerate_docx(dirpath):
            try:
                stat = docx_path.stat()
            except OSError:
                continue
            mtime_ns = stat.st_mtime_ns
            key = str(docx_path)
            # Skip the LLM call entirely if mtime is unchanged AND we still
            # own a tag pointing at this file — nothing to re-classify.
            existing_tag = next(
                (t for t, owner in _TAG_OWNER.items() if owner == key),
                None,
            )
            if _LAST_MTIME.get(key) == mtime_ns and existing_tag is not None:
                seen_tags[existing_tag] = docx_path.name
                continue
            body = _extract_text(docx_path)
            if not body:
                continue
            classification = await _classify_with_llm(body, docx_path.name)
            if classification is None:
                # vLLM unreachable / classifier failed — skip this docx for
                # now. Next mtime tick (or analyst touching the file)
                # retries. Not a graceful-handling violation: nothing has
                # been merged into PROJECT_DOCS so consumers see no entry,
                # and the startup log line surfaces the failure.
                log.warning("manual_injects: skipping %s (classifier failed; "
                            "will retry on next mtime change)", docx_path.name)
                continue
            tag = classification["tag"]
            doc_type = classification["type"]
            title = classification["title"]
            seen_tags[tag] = docx_path.name
            # If an EARLIER load owned a different tag for this file (e.g.
            # the LLM changed its mind because the file was edited), evict
            # the old tag first.
            if existing_tag is not None and existing_tag != tag:
                _ps.PROJECT_DOCS_OVERLAY.pop(existing_tag, None)
                _TAG_OWNER.pop(existing_tag, None)
            _ps.PROJECT_DOCS_OVERLAY[tag] = {
                "title": title,
                "body":  body,
                "type":  doc_type,
                "source_path": str(docx_path),
                "source": "manual_injects",
                "mtime_ns": mtime_ns,
            }
            _LAST_MTIME[key] = mtime_ns
            _TAG_OWNER[tag] = key
            mutated = True
            log.info("manual_injects: loaded %s → PROJECT_DOCS[%s] "
                     "(%d chars)", docx_path.name, tag, len(body))

        # Evict any previously-loaded tag whose docx is no longer on disk
        # (analyst removed it). Only evict tags we OWN (so stub-only entries
        # in scenario.PROJECT_DOCS are preserved on first run with empty
        # folder).
        for tag, owner_path in list(_TAG_OWNER.items()):
            if tag not in seen_tags:
                _ps.PROJECT_DOCS_OVERLAY.pop(tag, None)
                _LAST_MTIME.pop(owner_path, None)
                _TAG_OWNER.pop(tag, None)
                mutated = True
                log.info("manual_injects: evicted PROJECT_DOCS[%s] "
                         "(source docx removed)", tag)

        # PROJECT_DOCS changed → re-freeze the scenario clock so a fresh
        # OPORD/FRAGO with later DTGs advances "now". Idempotent if nothing
        # carries a newer timestamp.
        if mutated:
            # New/changed docs -> invalidate every project's cached
            # scenario namespace so the overlay lands. Per-project clock
            # refreezes happen lazily on the next now() read inside each
            # project's own context (there is no global clock anymore).
            _ps.bump_docs_overlay_rev()

        return dict(seen_tags)


async def initial_load() -> dict[str, str]:
    """One scan at FastAPI startup so PROJECT_DOCS is populated before the
    first request lands. Equivalent to `scan_once()` plus a log.info
    summary."""
    dirpath = Path(os.environ.get("MANUAL_INJECTS_DIR", DEFAULT_DIR))
    if not dirpath.is_dir():
        log.info("manual_injects: dir %s missing; skipping initial load "
                 "(set MANUAL_INJECTS_DIR or create the folder + mount it)",
                 dirpath)
        return {}
    seen = await scan_once(dirpath)
    log.info("manual_injects: initial load from %s → %d files loaded: %s",
             dirpath, len(seen), sorted(seen.keys()))
    return seen


async def watcher_loop() -> None:
    """Background poll loop. Polls the folder every
    `MANUAL_INJECTS_POLL_SEC` seconds and reloads any docx whose mtime
    changed. Cheap (one stat per docx; LLM classify only on mtime delta)."""
    interval = int(os.environ.get("MANUAL_INJECTS_POLL_SEC", DEFAULT_POLL_SEC))
    log.info("manual_injects: watcher started (interval=%ds, dir=%s)",
             interval, os.environ.get("MANUAL_INJECTS_DIR", DEFAULT_DIR))
    while True:
        try:
            await scan_once()
        except Exception:
            log.exception("manual_injects: watcher tick failed")
        await asyncio.sleep(interval)

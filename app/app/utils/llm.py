import asyncio
import logging
import os
import threading
import time
import warnings
from contextlib import asynccontextmanager, contextmanager

import httpx
import requests
from utils import config


log = logging.getLogger(__name__)


# Token counting for the admit-control gate is delegated to
# utils.tokenizer (single source of truth, locked to o200k_base). The gate
# uses this count to pick a weight; weight errors translate to admit-too-many
# or wait-too-long, not correctness — so the o200k_base BPE (not Gemma's
# native SentencePiece) is fine as an approximation. The shared helper bakes
# tiktoken availability into a process-wide flag so transient unavailability
# doesn't repeatedly re-attempt the import.
from utils.tokenizer import count_tokens as _tokenizer_count


# ============================================================================
# vLLM caller — OpenAI Chat Completions shape, with /v1/models discovery
# ============================================================================

_VLLM_MODEL_CACHE = None


# ============================================================================
# Token-weighted concurrency gate
# ============================================================================
#
# vllm's server-side cap (--max-num-seqs 16) admits 16 sequences regardless of
# size. At 100k context per sequence that's 1.6M tokens of KV demand against a
# pool sized for ~16 × 131k. The continuous-batcher accepts them, but per-
# request decode collapses (perf doc: C=1 @ 100k = 47 tok/s, C=2 @ 100k ≈ 25
# tok/s/req; C=8 thrashes to <5).
#
# This gate caps the SUM of in-flight INPUT tokens across all callers in the
# pytho-app process. Each request weighs its actual prompt-token count
# (measured via vllm's /tokenize — exact Gemma BPE, NOT a heuristic). A 100k
# request takes 100k of the budget; a 5k request takes 5k. The same total
# budget admits one big request OR several small ones — never both in a way
# that thrashes KV.
#
# Budget default = 131,072 Gemma BPE tokens (half of Gemma 4's 262,144 max
# context length — leaves headroom for the model's own generation buffer and
# for the second concurrent admit when both fit). Picks:
#   - One max-size request (131k) fits alone (clamped to budget).
#   - Two ~60-65k SITEMP paragraphs can run concurrently.
#   - Sixteen ~8k requests can overlap.
# Tunable via VLLM_TOKEN_BUDGET env var. The vllm-side max_num_seqs=16 is the
# server-level cap; the gate stays well below it to avoid long-context decode
# thrash.
#
# The gate is strict-FIFO via threading.Condition (loop-agnostic so both
# sync and async callers can share one queue). Per-request weight is clamped
# to <= budget so a single super-large request can't deadlock.
# ============================================================================

_TOKEN_BUDGET = int(os.environ.get("VLLM_TOKEN_BUDGET", "131072"))

# Nonlinear weighting: capacity scales DOWN faster as context grows. Decode
# thrash on Gemma+GB10 is roughly quadratic in concurrent-context-length
# (more in-flight long contexts = more KV bandwidth contention = lower per
# req tok/s). We mirror that:
#
#   effective_weight = tokens * (1 + tokens / SOFT_BREAKPOINT)
#
# With SOFT_BREAKPOINT=20000 and budget=131072:
#   - 5,000-token request  → weight ≈   6,250   → ~20 concurrent (good for short)
#   - 20,000-token request → weight =  40,000   → ~3 concurrent
#   - 50,000-token request → weight = 175,000   → 1 concurrent (clamped to budget alone)
#   - 100,000-token request → weight = 600,000  → 1 concurrent (clamped)
#   - small mix + one heavy: heavy alone already exceeds budget → heavy runs solo,
#     small ones queue behind it (no co-execution that would thrash decode)
#
# Linear was the pre-fix behavior — 5×10k + 1×80k coexisted under a linear
# budget, gave vllm 6 concurrent at long context, dropped per-req decode to
# 7 tok/s. Nonlinear forces the long-context call to crowd out everything
# else so it gets the user's-measured 45 tok/s ceiling.
_SOFT_BREAKPOINT = int(os.environ.get("VLLM_SOFT_BREAKPOINT", "20000"))

# Max seconds a request may sit in the FIFO queue WAITING to be admitted
# (i.e. before it ever reaches vllm). This bounds queue-wait only — once a
# request is admitted, the per-call HTTP timeout (invoke_vllm_chat_async
# timeout=1800 / invoke_vllm_chat timeout=600) governs execution.
#
# Why this exists: the gate wait was previously an unbounded cond.wait(),
# so a wedged head-of-queue holder (e.g. a leaked budget slot, or a request
# parked on a 30-min vllm HTTP timeout) would freeze every request behind
# it FOREVER with no automatic recovery — only a manual cancel or a server
# restart cleared it. A 10-minute deadline turns "stuck in queue forever"
# into a clean gate_timeout failure the retry/triage path can act on.
#
# The clock starts when the request enqueues and is checked on every
# cond wakeup (admit, release, cancel) plus a periodic poll, so expiry is
# detected within ~1s of the deadline regardless of notify traffic.
# Tunable via VLLM_QUEUE_WAIT_TIMEOUT (seconds); 0/negative disables the
# deadline (restores the old unbounded behavior).
_QUEUE_WAIT_TIMEOUT = float(os.environ.get("VLLM_QUEUE_WAIT_TIMEOUT", "600"))


class GateWaitTimeout(TimeoutError):
    """Raised when a request waits in the gate queue past
    _QUEUE_WAIT_TIMEOUT without being admitted. The message contains both
    'gate' and 'timeout' so product_records._classify_error buckets it as
    error_kind='gate_timeout'. Subclasses TimeoutError so generic
    timeout-handling callers also catch it."""


def _effective_weight(raw_tokens: int) -> int:
    """Nonlinear context-aware weight: w = t * (1 + t / SOFT_BREAKPOINT).

    Cheap for short prompts (~linear), super-linear for long. Allows high
    parallelism on short context (where Gemma can sustain it) and forces
    serialization on long context (where Gemma cannot)."""
    t = max(1, int(raw_tokens or 1))
    return int(t * (1.0 + t / _SOFT_BREAKPOINT))


# ─────────────────────────────────────────────────────────────────────
# Per-product chain serialization.
# Only ONE product generation runs at a time across the whole server.
# product_autodraft._autodraft_one acquires this lock before kicking
# the DAG; releases on exit. Other generation paths (sweep, autofire,
# map-driven, dashboard) all flow through _autodraft_one so the same
# lock serializes them all. Sidesteps vllm-bandwidth contention that
# made concurrent BDA/HVTL/HPTL chains each take 50-90s while serial
# is 30-40s.
PRODUCT_GATE: asyncio.Lock = asyncio.Lock()


# ─────────────────────────────────────────────────────────────────────
# Per-chain progress tracking for "Generating... XX%" surfacing.
# Keyed by autodraft session_id. Updated by the DAG executor:
#   - set_chain_total(session_id, expected_tokens) at skeleton build
#   - record_chain_consume(session_id, eff) on each gate ACQUIRE
#   - chain_progress_pct(session_id) returns int 0-99
#   - clear_chain(session_id) on chain complete / fail
# Progress is approximate (token-weighted vs eff budget, not wall clock).
_CHAIN_PROGRESS: dict[str, dict] = {}


def set_chain_total(session_id: str, total_tokens: int) -> None:
    if not session_id: return
    _CHAIN_PROGRESS[session_id] = {"total": max(1, int(total_tokens)),
                                      "consumed": 0}


def record_chain_consume(session_id: str, eff: int) -> None:
    if not session_id: return
    p = _CHAIN_PROGRESS.get(session_id)
    if p is None: return
    p["consumed"] = min(p["total"], p.get("consumed", 0) + int(eff))


def chain_progress_pct(session_id: str) -> int:
    p = _CHAIN_PROGRESS.get(session_id) if session_id else None
    if not p or not p.get("total"):
        return 0
    pct = int(100 * p["consumed"] / p["total"])
    # Cap at 99 until the chain explicitly completes; 100 ⇄ done.
    return min(99, max(0, pct))


def clear_chain(session_id: str) -> None:
    _CHAIN_PROGRESS.pop(session_id or "", None)
    _SESSION_TO_RECORD.pop(session_id or "", None)


# Side-channel session_id → product_records.record_id map. State is
# frozen so we can't stamp the record_id on the running chain's State;
# product_autodraft fills this map when register_and_run mints a
# record, and runtime/dag_executor reads it from the chain's session
# at wave-end so it can broadcast progress_pct via record_transition.
_SESSION_TO_RECORD: dict[str, str] = {}


def register_session_record(session_id: str, record_id: str) -> None:
    if session_id and record_id:
        _SESSION_TO_RECORD[session_id] = record_id


def record_id_for_session(session_id: str) -> str | None:
    return _SESSION_TO_RECORD.get(session_id or "")


# Contextvar holding the currently-running chain's session_id. The DAG
# executor binds this at run_dag start; the vllm gate reads it on each
# acquire and credits the eff weight to that chain's _CHAIN_PROGRESS.
# Defaults to "" so non-chain calls (sync acquires, test harnesses,
# direct LLM helpers) are no-ops in the progress map.
from contextvars import ContextVar
_CURRENT_CHAIN_SESSION: ContextVar[str] = ContextVar(
    "_CURRENT_CHAIN_SESSION", default=""
)


def set_current_chain_session(session_id: str):
    """Bind session_id as the active chain for progress tracking.
    Returns the contextvar token so the caller can reset()."""
    return _CURRENT_CHAIN_SESSION.set(session_id or "")


def reset_current_chain_session(token) -> None:
    try:
        _CURRENT_CHAIN_SESSION.reset(token)
    except Exception:
        pass


class TokenWeightedGate:
    """STRICT-FIFO async semaphore with nonlinear token-weighted budget.

    Total in-flight weight is capped at `budget`. Acquires enqueue in
    arrival order; the gate only admits the head of the queue (the
    longest-waiting request), even if a behind-it smaller request would
    fit under the current headroom. Without this rule a steady arrival
    of small admit-immediately requests would STARVE a heavy waiter:
    observed in production where doctrine_heavy (eff=130k clamped to
    budget) waited 54s while 4 smaller batches admitted ahead of it.

    Weight scales super-linearly with raw token count (see
    _effective_weight) so long-context requests crowd out concurrent
    short ones — preventing the C=N>2 long-context decode thrash that
    drops Gemma+GB10 from ~45 tok/s to ~7 tok/s.

    Per-request weight is clamped to [1, budget] so single-shot oversized
    requests still admit alone.
    """

    def __init__(self, budget: int):
        self._budget = max(1, budget)
        self._in_use = 0
        # Strict-FIFO queue. Each acquire captures its arrival serial via
        # _next_serial; only the lowest-serial unsatisfied waiter can admit.
        self._next_serial = 0
        self._queue: list[int] = []
        # threading primitives — loop-agnostic. The async path bridges via
        # `asyncio.to_thread(cond.wait)` so it doesn't block the running
        # event loop while still sharing one truth-of-source queue with
        # the sync path.
        self._cond = threading.Condition()
        # Optional resolver for cancel_waiter(non-int). Registered by
        # external owners of the identifier↔serial mapping (e.g.
        # product_records.py) via register_serial_resolver. None means
        # the gate has no way to translate identifiers, and string-
        # identifier cancels become no-ops.
        self._serial_resolver = None

    def _wait_for_admit(self, serial: int, eff: int, label: str, raw: int) -> None:
        """Block (in this thread) on self._cond until `serial` is at the
        head of the queue AND `eff` fits under the budget. Caller MUST
        hold self._cond. On any exception (including KeyboardInterrupt
        and CancelledError raised in this thread), removes `serial` from
        the queue and wakes others before re-raising.

        Enforces the queue-wait deadline: if the request sits unadmitted
        for more than _QUEUE_WAIT_TIMEOUT seconds, raises GateWaitTimeout
        (which the BaseException arm cleans up like any other exit). The
        deadline is monotonic (immune to wall-clock jumps). A
        non-positive _QUEUE_WAIT_TIMEOUT disables it (unbounded wait, the
        legacy behavior)."""
        # Monotonic deadline so a wall-clock adjustment can't extend or
        # truncate the wait. None => deadline disabled.
        deadline = (
            time.monotonic() + _QUEUE_WAIT_TIMEOUT
            if _QUEUE_WAIT_TIMEOUT > 0
            else None
        )
        try:
            while True:
                is_head = self._queue and self._queue[0] == serial
                fits = self._in_use + eff <= self._budget
                if is_head and fits:
                    return
                # Check the deadline BEFORE re-waiting so a request that
                # crossed it while parked (e.g. woken by an unrelated
                # notify) fails out promptly rather than blocking again.
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        log.warning(
                            "vllm_gate: QUEUE_TIMEOUT label=%s raw=%d eff=%d "
                            "in_use=%d budget=%d serial=%d waited>=%.0fs "
                            "queue=%s",
                            label or "?", raw, eff, self._in_use,
                            self._budget, serial, _QUEUE_WAIT_TIMEOUT,
                            list(self._queue),
                        )
                        raise GateWaitTimeout(
                            f"vllm gate queue-wait timeout: request "
                            f"(label={label or '?'}, eff={eff}) waited "
                            f"{_QUEUE_WAIT_TIMEOUT:.0f}s without admit "
                            f"(in_use={self._in_use}/{self._budget}, "
                            f"queue_len={len(self._queue)})"
                        )
                else:
                    remaining = None
                log.info(
                    "vllm_gate: WAIT label=%s raw=%d eff=%d in_use=%d "
                    "budget=%d serial=%d queue=%s",
                    label or "?", raw, eff, self._in_use, self._budget,
                    serial, list(self._queue),
                )
                # Cap each wait so the deadline is re-checked at least
                # once a second even with no notify traffic. cond.wait
                # returns False on timeout, True on notify — we don't
                # branch on it; the loop top re-evaluates head/fits and
                # the deadline either way.
                self._cond.wait(
                    1.0 if remaining is None else min(1.0, remaining)
                )
        except BaseException:
            try:
                self._queue.remove(serial)
            except ValueError:
                pass
            self._cond.notify_all()
            raise

    @contextmanager
    def acquire_sync(self, weight: int, label: str = ""):
        """Sync (blocking) acquire. Use from threadpool / non-async code
        paths. Behaves identically to `acquire` but blocks the calling
        thread instead of yielding to an event loop."""
        raw = max(1, int(weight or 1))
        eff = max(1, min(_effective_weight(raw), self._budget))
        with self._cond:
            serial = self._next_serial
            self._next_serial += 1
            self._queue.append(serial)
            self._wait_for_admit(serial, eff, label, raw)
            self._queue.pop(0)
            self._in_use += eff
            log.info(
                "vllm_gate: ACQUIRE label=%s raw=%d eff=%d in_use=%d "
                "budget=%d waiters=%d",
                label or "?", raw, eff, self._in_use, self._budget,
                len(self._queue),
            )
            # Per-chain progress hook — credit this acquire's eff to
            # whichever session_id is currently in the contextvar (set
            # by the DAG entry point per chain). Best-effort; no-op
            # outside chain scope. See _CHAIN_PROGRESS docs above.
            try:
                _sess = _CURRENT_CHAIN_SESSION.get()
                if _sess:
                    record_chain_consume(_sess, eff)
            except Exception:
                pass
        try:
            yield eff
        finally:
            with self._cond:
                self._in_use -= eff
                log.info(
                    "vllm_gate: RELEASE label=%s raw=%d eff=%d in_use=%d "
                    "budget=%d waiters=%d",
                    label or "?", raw, eff, self._in_use, self._budget,
                    len(self._queue),
                )
                self._cond.notify_all()

    @asynccontextmanager
    async def acquire(self, weight: int, label: str = ""):
        """Async acquire. Bridges to the threading.Condition via
        `asyncio.to_thread` so the calling event loop is never blocked
        while the worker thread holds the lock + cond-waits. Multiple
        event loops (e.g. the FastAPI request loop + the DAG runner's
        sub-loop) and sync threadpool callers all share one queue this
        way without loop-affinity errors that would otherwise come from
        an asyncio.Condition."""
        raw = max(1, int(weight or 1))
        eff = max(1, min(_effective_weight(raw), self._budget))

        def _enqueue_and_wait() -> None:
            with self._cond:
                serial_local = self._next_serial
                self._next_serial += 1
                self._queue.append(serial_local)
                self._wait_for_admit(serial_local, eff, label, raw)
                self._queue.pop(0)
                self._in_use += eff
                log.info(
                    "vllm_gate: ACQUIRE label=%s raw=%d eff=%d in_use=%d "
                    "budget=%d waiters=%d",
                    label or "?", raw, eff, self._in_use, self._budget,
                    len(self._queue),
                )

        # to_thread propagates exceptions from the worker thread AFTER
        # _wait_for_admit's BaseException arm has cleaned up the queue +
        # notified others. Nothing more to do here on cancel.
        await asyncio.to_thread(_enqueue_and_wait)
        try:
            yield eff
        finally:
            def _release() -> None:
                with self._cond:
                    self._in_use -= eff
                    log.info(
                        "vllm_gate: RELEASE label=%s raw=%d eff=%d in_use=%d "
                        "budget=%d waiters=%d",
                        label or "?", raw, eff, self._in_use, self._budget,
                        len(self._queue),
                    )
                    self._cond.notify_all()
            # Release on a worker thread to avoid blocking the loop on
            # cond.acquire() contention. The release itself is bounded
            # and short, but bridging through to_thread keeps the
            # semantics uniform.
            await asyncio.to_thread(_release)

    async def cancel_waiter(self, identifier) -> bool:
        """Externally cancel a queued acquire.

        Accepts either:
          - an int serial (as returned in WAIT log lines), in which case
            the gate pops that serial directly, or
          - any other value (typically a record_id str) which is resolved
            to a serial via the registered serial resolver (see
            `register_serial_resolver`). If no resolver is registered, or
            the resolver returns None, this is a no-op that returns False.

        The in-acquire BaseException arm already handles task.cancel() →
        CancelledError → cleanup. This helper is the explicit cooperative
        path for callers that hold an identifier but don't (or can't)
        cancel the awaiting task directly: it pops the slot from the FIFO
        queue and wakes every other waiter so the new head can re-check
        fits.

        After removal, every waiter still in the queue retains its serial
        — serials never shift — but their POSITIONS (1-based index in
        the list) shrink by one if they sat behind the cancelled serial.
        The position lookup in product_records._gate_position walks the
        live list, so the change is reflected on the next poll without
        any explicit re-indexing.

        Returns True if a serial was found and removed; False if no
        waiter matched (already admitted, already cancelled, never
        enqueued, or no resolver registered).

        Async signature is for caller convenience (`await cancel_waiter`)
        — the underlying work is a non-blocking sync critical section.
        """
        if isinstance(identifier, int):
            serial = identifier
        else:
            resolver = self._serial_resolver
            if resolver is None:
                log.warning(
                    "vllm_gate: cancel_waiter called with non-int %r but "
                    "no serial resolver is registered; no-op.",
                    identifier,
                )
                return False
            try:
                resolved = resolver(identifier)
            except Exception:
                log.exception(
                    "vllm_gate: serial resolver raised for identifier=%r",
                    identifier,
                )
                return False
            if resolved is None:
                return False
            serial = int(resolved)
        with self._cond:
            try:
                self._queue.remove(serial)
            except ValueError:
                return False
            self._cond.notify_all()
            log.info(
                "vllm_gate: CANCEL_WAITER serial=%d remaining_waiters=%d "
                "in_use=%d budget=%d",
                serial, len(self._queue), self._in_use, self._budget,
            )
            return True

    def register_serial_resolver(self, fn) -> None:
        """Register a callable `fn(identifier) -> int | None` that maps
        an external identifier (e.g. a ProductRecord.record_id) to the
        gate serial that record's acquire was assigned. Used by
        `cancel_waiter` when the caller passes a non-int identifier.

        Only one resolver is held at a time; calling this again replaces
        the prior resolver."""
        self._serial_resolver = fn

    def stats(self) -> dict:
        return {
            "budget": self._budget,
            "soft_breakpoint": _SOFT_BREAKPOINT,
            "in_use": self._in_use,
            # Waiters = length of the strict-FIFO queue. The queue holds
            # every acquire() that has enqueued but not yet popped (i.e.
            # is still in the `while True` await loop OR is the head that
            # has not yet broken out). _queue is the only source of truth
            # for pending-but-not-acquired entries on this gate.
            "waiters": len(self._queue),
            "headroom": self._budget - self._in_use,
        }


_GATE = TokenWeightedGate(budget=_TOKEN_BUDGET)


def vllm_gate_stats() -> dict:
    """Snapshot of gate state — for /api/diag exposure."""
    return _GATE.stats()


def _vllm_base_url() -> str:
    api = config.Config.VLLM_API()
    return api.split("/v1/")[0] if "/v1/" in api else api.rstrip("/")


def _flatten_chat_text(messages: list) -> str:
    """Concatenate every text-bearing field in a chat-completions messages
    list. Used by the fallback tokenizer paths when vllm /tokenize is
    unreachable."""
    parts: list[str] = []
    for m in messages or []:
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for piece in c:
                if isinstance(piece, dict):
                    t = piece.get("text") or ""
                    parts.append(t)
    return "\n".join(parts)


def _fallback_token_count(messages: list) -> int:
    """Local-only token count (no network). Used when vllm /tokenize is
    unreachable or when the caller is in a sync context that can't await
    an HTTP round-trip. Tiktoken via utils.tokenizer (o200k_base), then
    chars/3 fallback baked into the helper. chars/3 is intentionally
    pessimistic (over-counts) so the gate errs on the side of admitting
    fewer concurrent requests rather than thrashing vllm decode."""
    text = _flatten_chat_text(messages)
    return _tokenizer_count(text, fallback_chars_per_token=3)


async def _count_chat_tokens(messages: list, tools: list | None) -> int:
    """Exact Gemma BPE token count for the chat payload, via vllm /tokenize.

    Falls back to tiktoken's cl100k_base encoder when /tokenize is
    unreachable; falls back to a chars/3 heuristic only if tiktoken is
    also unavailable. Failure should not block the gate (worst case:
    client-side over-admit until vllm queues + back-pressures).
    """
    body = {"model": _vllm_model(), "messages": messages}
    if tools:
        body["tools"] = tools
    base = _vllm_base_url()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{base}/tokenize", json=body)
            resp.raise_for_status()
            data = resp.json()
            n = int(data.get("count") or 0)
            if n > 0:
                return n
    except Exception as exc:
        log.warning("vllm /tokenize failed; falling back to tiktoken: %s", exc)
    return _fallback_token_count(messages)


def _count_chat_tokens_sync(messages: list, tools: list | None) -> int:
    """Sync sibling of `_count_chat_tokens`. Uses `requests` for /tokenize
    so it can run from any thread without an event loop, and shares the
    same tiktoken/char fallback."""
    body = {"model": _vllm_model(), "messages": messages}
    if tools:
        body["tools"] = tools
    base = _vllm_base_url()
    try:
        resp = requests.post(f"{base}/tokenize", json=body, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        n = int(data.get("count") or 0)
        if n > 0:
            return n
    except Exception as exc:
        log.warning("vllm /tokenize failed (sync); falling back to tiktoken: %s", exc)
    return _fallback_token_count(messages)


def _vllm_model() -> str:
    """Discover the served model name once and cache. vLLM serves exactly one
    model (whatever `vllm serve <model>` was launched with); fetch it from
    /v1/models so callers don't have to configure it separately.

    VLLM_MODEL env var overrides discovery. Required when the endpoint is an
    Ollama OpenAI-compat server (laptop dev): Ollama's /v1/models lists EVERY
    pulled model and data[0] is whichever happens to sort first, not the one
    we want."""
    global _VLLM_MODEL_CACHE
    if _VLLM_MODEL_CACHE:
        return _VLLM_MODEL_CACHE
    override = os.getenv("VLLM_MODEL")
    if override:
        _VLLM_MODEL_CACHE = override
        return _VLLM_MODEL_CACHE
    api = config.Config.VLLM_API()
    if not api:
        raise RuntimeError("VLLM_API env var is not set")
    base = api.split("/v1/")[0] if "/v1/" in api else api.rstrip("/")
    resp = requests.get(f"{base}/v1/models", timeout=5).json()
    _VLLM_MODEL_CACHE = resp["data"][0]["id"]
    return _VLLM_MODEL_CACHE


_OLLAMA_COMPAT_CACHE = None


def _is_ollama() -> bool:
    """Detect (once) whether VLLM_API is actually an Ollama OpenAI-compat
    endpoint. Matters because Ollama IGNORES chat_template_kwargs.enable_thinking
    — Gemma 4 then thinks by default, burning the completion budget on a
    `reasoning` field and returning empty content. Ollama's own toggle is the
    `reasoning_effort` request param instead. Detection probes /api/version,
    an Ollama-only route vLLM doesn't serve."""
    global _OLLAMA_COMPAT_CACHE
    if _OLLAMA_COMPAT_CACHE is None:
        try:
            _OLLAMA_COMPAT_CACHE = requests.get(f"{_vllm_base_url()}/api/version", timeout=3).ok
        except Exception:
            _OLLAMA_COMPAT_CACHE = False
    return _OLLAMA_COMPAT_CACHE


def _apply_ollama_thinking(body: dict, thinking: bool) -> None:
    """Enforce the leadership-locked thinking default on Ollama endpoints,
    where chat_template_kwargs is silently ignored. 'none' fully disables the
    reasoning trace; opted-in callers (thinking=True) keep it."""
    if _is_ollama():
        body["reasoning_effort"] = "high" if thinking else "none"


def invoke_vllm_chat(messages, tools=None, thinking=False, timeout=600, max_tokens=8000, temperature=0.2):
    """Call vLLM's OpenAI-compat /v1/chat/completions (SYNC). Returns the
    parsed JSON response (caller inspects `choices[0].message`).

    DEPRECATED: prefer `invoke_vllm_chat_async`. The sync path is retained
    for handlers/aar.py, handlers/chat.py, handlers/agents.py, handlers/
    docgen.py, and handlers/intel_agent.py turn loops that still run in
    FastAPI's sync threadpool. New code MUST use the async entrypoint.

    Gate behavior: this call WEIGHS against the same TokenWeightedGate as
    the async path. The gate is backed by threading.Condition (loop-
    agnostic), so the sync entrypoint blocks the calling thread on the
    same FIFO queue that async callers wait on. This keeps the process-
    wide budget accurate when sync and async callers run side-by-side
    (which they do today in every container).

    THINKING DEFAULT — DO NOT FLIP WITHOUT USER SIGN-OFF:
        Default is `thinking=False`. Leadership benchmarked the system at
        this latency profile; enabling thinking globally roughly doubles
        per-call wall-clock and is unacceptable for the interactive
        turn-loop call sites that dominate this entrypoint. Specific call
        sites that NEED reasoning trace (e.g. DAG planner nodes, certain
        intel-agent dispatches) opt in per-call via `thinking=True`. Do
        NOT change the default to True in a refactor; the regression cost
        is measured in minutes per user interaction.

    Args:
        messages: [{"role": "system"/"user"/"assistant"/"tool", "content": str, ...}, ...]
        tools: OpenAI-style tools schema, or None for plain chat
        thinking: enables Gemma 4's reasoning trace via chat_template_kwargs.
                  Default False (see THINKING DEFAULT block above).
        timeout: HTTP timeout in seconds (raised once per call; no retries —
                 surface the failure to the caller)
        max_tokens: max completion tokens
        temperature: sampling temperature
    """
    warnings.warn(
        "invoke_vllm_chat (sync) is deprecated; migrate call sites to "
        "invoke_vllm_chat_async. The sync wrapper still counts against "
        "TokenWeightedGate via its loop-agnostic threading.Condition.",
        DeprecationWarning,
        stacklevel=2,
    )
    body = {
        "model": _vllm_model(),
        "messages": messages,
        "stream": False,
        "temperature": temperature,
        "max_tokens": max_tokens,
        # 1.15 breaks degenerate repetition loops we've seen on highly-structured
        # emits (rows of nearly-identical dicts) without hurting normal output.
        "repetition_penalty": 1.15,
        # thinking=False is the LEADERSHIP-LOCKED default. See the THINKING
        # DEFAULT block in this function's docstring for the policy
        # rationale; per-call `thinking=True` is the only sanctioned
        # opt-in path.
        "chat_template_kwargs": {"enable_thinking": thinking},
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    _apply_ollama_thinking(body, thinking)
    weight = _count_chat_tokens_sync(messages, tools)
    with _GATE.acquire_sync(weight, label="chat_sync"):
        resp = requests.post(config.Config.VLLM_API(), json=body, timeout=timeout)
        resp.raise_for_status()
        return resp.json()


def invoke_vllm_text(messages, **kwargs) -> str:
    """Convenience wrapper around invoke_vllm_chat that returns just the
    content string. Mirrors the old invoke_ollama_llm bare-string return so
    plain text-in / text-out chat sites are a 1-line migration."""
    resp = invoke_vllm_chat(messages, **kwargs)
    return (resp["choices"][0]["message"].get("content") or "").strip()


async def invoke_vllm_chat_async(messages, tools=None, thinking=False, timeout=1800,
                                  max_tokens=8000, temperature=0.2, gate_label: str = "",
                                  response_format: dict | None = None):
    """Async sibling of invoke_vllm_chat. Mirrors the sync version's request
    shape exactly — same body, same `chat_template_kwargs`, same OpenAI-compat
    /v1/chat/completions endpoint — but issues the POST via httpx.AsyncClient
    so the parallel-DAG runner can fire many concurrent LLM calls under one
    event loop without blocking a thread per call.

    Every call passes through the process-level TokenWeightedGate, which caps
    the SUM of in-flight INPUT tokens across all callers. The per-request
    weight is the EXACT Gemma BPE count from vllm's /tokenize (not a char
    heuristic — the gate uses the same tokenizer the model uses).

    This is the preferred entrypoint. The sync wrapper `invoke_vllm_chat`
    exists only for legacy handlers/* call sites that still run in FastAPI's
    sync threadpool; it shares this gate via the loop-agnostic
    threading.Condition that backs TokenWeightedGate.

    THINKING DEFAULT — DO NOT FLIP WITHOUT USER SIGN-OFF:
        Default is `thinking=False`. Leadership benchmarked the system at
        this latency profile; enabling thinking globally roughly doubles
        per-call wall-clock and is unacceptable for the interactive call
        sites that share this entrypoint. DAG planner nodes and certain
        intel-agent dispatches opt in per-call via `thinking=True`. Do
        NOT change the default to True in a refactor; the regression cost
        is measured in minutes per user interaction.

    Args:
        messages: [{"role": "system"/"user"/"assistant"/"tool", "content": str, ...}, ...]
        tools: OpenAI-style tools schema, or None for plain chat
        thinking: enables Gemma 4's reasoning trace via chat_template_kwargs.
                  Default False (see THINKING DEFAULT block above).
        timeout: HTTP timeout in seconds (raised once per call; no retries —
                 surface the failure to the caller)
        max_tokens: max completion tokens
        temperature: sampling temperature
        gate_label: optional label tag for the gate's acquire/release logs
                    — useful for tracing which paragraph node is in flight.
    """
    body = {
        "model": _vllm_model(),
        "messages": messages,
        "stream": False,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "repetition_penalty": 1.15,
        # thinking=False is the LEADERSHIP-LOCKED default. See the THINKING
        # DEFAULT block in this function's docstring for the policy
        # rationale; per-call `thinking=True` is the only sanctioned
        # opt-in path.
        "chat_template_kwargs": {"enable_thinking": thinking},
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if response_format:
        # vllm OpenAI-compatible structured output. With
        # response_format={"type": "json_object"} vllm grammar-constrains
        # decoding so the LLM PHYSICALLY CANNOT emit invalid JSON — the
        # FSM-based decoder rejects any token that would produce a parse
        # error. Eliminates the "one stray comma broke the whole batch"
        # failure mode that previously killed entire SITEMP / Threat COA
        # fanout chains on a single bad character. Callers that need a
        # specific JSON schema pass {"type": "json_schema", "schema": ...}
        # instead — vllm's outlines integration enforces that too.
        body["response_format"] = response_format
    _apply_ollama_thinking(body, thinking)
    weight = await _count_chat_tokens(messages, tools)
    async with _GATE.acquire(weight, label=gate_label or "chat_async"):
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(config.Config.VLLM_API(), json=body)
            resp.raise_for_status()
            return resp.json()


async def invoke_vllm_text_async(messages, **kwargs) -> str:
    """Async equivalent of invoke_vllm_text. Returns just the content string."""
    resp = await invoke_vllm_chat_async(messages, **kwargs)
    return (resp["choices"][0]["message"].get("content") or "").strip()

# ---- Cloud LLM path (ported from main during the edge+cloud
# unification, 2026-07-07). Consumed by utils/world_state.py and the
# rfis router — cloud-only surfaces. boto3.client() is lazy (no
# network at import), so this is inert on the airgapped edge, which
# never calls fetch_llm_response.
import boto3 as _boto3
from botocore.exceptions import ClientError  # noqa: F401 — fetch_llm_response handler
bedrock_runtime = _boto3.client("bedrock-runtime", region_name="us-east-1")
BEDROCK_MODEL_ID = "us.meta.llama4-maverick-17b-instruct-v1:0"


def fetch_llm_response(prompt: str) -> str:
    """
    Fetch LLM response from AWS Bedrock synchronously.
    
    Args:
        prompt: The prompt text to send to the model
    
    Returns:
        str: The model's response text or an error message
    """
    try:
        request_body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "text": f"""{prompt}"""
                        }
                    ],
                }
            ]
        }

        response = bedrock_runtime.converse(
            modelId=BEDROCK_MODEL_ID,
            messages=request_body["messages"]
        )

        return (response["output"]["message"]["content"][-1]["text"])

    except ClientError as e:
        logging.error(f"LLM request AWS client failure: {e}")
        return "[ERROR] LLM network failure."

    except Exception as e:
        logging.error(f"LLM processing error: {e}")
        traceback.print_exc()
        return "[ERROR] LLM unexpected failure."

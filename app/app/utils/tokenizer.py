"""Single source of truth for tiktoken-based token counting in pytho-app.

Three callsites in the app need an approximate token count for non-decision
purposes (admit-control gating, dep-payload size enforcement, doc generation
budgeting). Historically each callsite imported tiktoken independently and
they drifted apart — some used cl100k_base (GPT-3.5/4 era), others used
o200k_base (GPT-4o / Claude 3.5+ era). The drift caused inconsistent
admit/size decisions for the same input.

This module locks the tokenizer choice to **o200k_base** (modern Claude /
Gemini / GPT-4o era — closer to the actual Gemma-4 SentencePiece BPE count
than cl100k_base, though still an approximation). Every other module in the
app gets its token counter from here.

If a future tokenizer is added or swapped, change it ONCE here. Do not
re-introduce direct `tiktoken.get_encoding(...)` calls in other modules.

The encoding is lazy-loaded on first use to keep import-time cheap.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

_ENCODING_NAME = "o200k_base"
_ENC: Any = None
_TIKTOKEN_FAILED = False


def _get_encoder() -> Any:
    """Return the o200k_base tiktoken encoder, or None if unavailable.

    Caches both the encoder and the unavailable-state so each call after
    the first is constant-time. tiktoken availability is determined ONCE
    at process startup; transient HF / network failures don't repeatedly
    re-attempt the import.
    """
    global _ENC, _TIKTOKEN_FAILED
    if _ENC is not None:
        return _ENC
    if _TIKTOKEN_FAILED:
        return None
    try:
        import tiktoken
        _ENC = tiktoken.get_encoding(_ENCODING_NAME)
        return _ENC
    except Exception:
        _TIKTOKEN_FAILED = True
        log.warning(
            "tiktoken get_encoding(%s) failed; callers will fall back to "
            "char-based heuristics. This will be silent if TIKTOKEN_CACHE_DIR "
            "is set to a directory that has the cached file. In airgapped "
            "deployments the cache is baked into the docker-api image at "
            "/opt/tiktoken-cache.",
            _ENCODING_NAME,
            exc_info=True,
        )
        return None


def count_tokens(text: str, *, fallback_chars_per_token: int = 4) -> int:
    """Estimate the o200k_base token count of `text`.

    Returns at least 1 (callers gate against >= some_min and 0 would
    falsely allow zero-length passes through the gate). When tiktoken is
    unavailable, falls back to `len(text) // fallback_chars_per_token` —
    callers pick the divisor based on whether they want to err on
    over-counting (small divisor like 3, e.g. admit-control) or
    under-counting (larger divisor like 4, e.g. doc generation budgeting).

    Args:
        text: the string to count
        fallback_chars_per_token: divisor for the heuristic when tiktoken
            isn't loadable. Default 4 (OpenAI's general-English rule).
    """
    enc = _get_encoder()
    if enc is not None:
        try:
            return max(1, len(enc.encode(text)))
        except Exception:
            # Binary / exotic UTF-8 / encoding bug — slip to fallback rather
            # than fail the caller, since callers use this number for
            # gating, not correctness.
            pass
    return max(1, len(text) // max(1, fallback_chars_per_token))


def encoding_name() -> str:
    """The tokenizer name in use, for logging / diagnostics."""
    return _ENCODING_NAME

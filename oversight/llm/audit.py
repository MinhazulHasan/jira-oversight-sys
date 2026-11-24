"""LLM audit trail: one llm_calls row per call, holding hashes, counts and outcomes only."""
from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from datetime import datetime

from oversight.llm.provider import Usage
from oversight.store.db import Store
from oversight.timeutil import to_iso, utcnow

# The schema's vocabulary for llm_calls.outcome.
OUTCOMES: tuple[str, ...] = ("ok", "refused", "error")
# An error is a short diagnosis, never a dump of what came back.
_ERROR_MAX_CHARS = 500


def prompt_hash(system: str, user: str) -> str:
    """Fingerprint of a rendered prompt, tying a call to a prompt version without storing the prompt."""
    return hashlib.sha256(f"{system}\x1f{user}".encode("utf-8")).hexdigest()


def record_llm_call(
    store: Store,
    *,
    purpose: str,
    provider: str,
    model: str | None,
    prompt_hash: str,
    usage: Usage | None,
    latency_ms: int | None,
    stop_reason: str | None,
    outcome: str,
    error: str | None = None,
    qa_id: str | None = None,
    now: Callable[[], datetime] = utcnow,
) -> str:
    """Insert one llm_calls row and return its call_id."""
    if outcome not in OUTCOMES:
        raise ValueError(f"llm call outcome must be one of {', '.join(OUTCOMES)}, not {outcome!r}")
    call_id = uuid.uuid4().hex
    store.upsert(
        "llm_calls",
        {
            "call_id": call_id,
            "called_at": to_iso(now()),
            "purpose": purpose,
            "provider": provider,
            "model": model,
            "prompt_hash": prompt_hash,
            "input_tokens": usage.input_tokens if usage is not None else None,
            "output_tokens": usage.output_tokens if usage is not None else None,
            "latency_ms": latency_ms,
            "stop_reason": stop_reason,
            "outcome": outcome,
            "error": error[:_ERROR_MAX_CHARS] if error else None,
            "qa_id": qa_id,
        },
    )
    return call_id

"""Test doubles for the LLM provider and its audit trail."""
from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pytest
from pydantic import BaseModel

from oversight.llm import audit as llm_audit
from oversight.llm.provider import Plan, PlanCall, StructuredResult, TextResult, Usage
from oversight.store.db import Store
from oversight.timeutil import to_iso, utcnow

Scripted = Any


@dataclass
class FakeCall:
    kind: str
    system: str
    user: str
    model: str
    effort: str
    max_tokens: int
    schema: type[BaseModel] | None = None


@dataclass
class FakeProvider:
    plans: list[Scripted] = field(default_factory=list)
    answers: list[Scripted] = field(default_factory=list)
    model: str = "fake-model"
    name: str = "fake"
    calls: list[FakeCall] = field(default_factory=list)

    @property
    def structured_calls(self) -> list[FakeCall]:
        return [call for call in self.calls if call.kind == "structured"]

    @property
    def compose_calls(self) -> list[FakeCall]:
        return [call for call in self.calls if call.kind == "compose"]

    def structured(
        self,
        system: str,
        user: str,
        schema: type[BaseModel],
        *,
        model: str,
        effort: str = "low",
        max_tokens: int = 1024,
    ) -> StructuredResult:
        self.calls.append(FakeCall("structured", system, user, model, effort, max_tokens, schema))
        item = self._next(self.plans, "plan", user)
        if isinstance(item, StructuredResult):
            return item
        if not isinstance(item, schema):
            raise AssertionError(f"FakeProvider: scripted plan is {type(item).__name__}, not {schema.__name__}")
        return StructuredResult(parsed=item, model=self.model, stop_reason="end_turn", usage=Usage(120, 40), latency_ms=1)

    def compose(
        self,
        system: str,
        user: str,
        *,
        model: str,
        effort: str = "medium",
        max_tokens: int = 1024,
    ) -> TextResult:
        self.calls.append(FakeCall("compose", system, user, model, effort, max_tokens))
        item = self._next(self.answers, "answer", user)
        if isinstance(item, TextResult):
            return item
        if not isinstance(item, str):
            raise AssertionError(f"FakeProvider: scripted answer is {type(item).__name__}, not text")
        return TextResult(text=item, model=self.model, stop_reason="end_turn", usage=Usage(300, 80), latency_ms=1)

    @staticmethod
    def _next(script: list[Scripted], what: str, user: str) -> Any:
        if not script:
            raise AssertionError(f"FakeProvider: no scripted {what} left for this call")
        item = script.pop(0)
        if callable(item) and not isinstance(item, (BaseModel, Exception)):
            item = item(user)
        if isinstance(item, Exception):
            raise item
        return item


def plan(*calls: str | tuple[str, dict[str, Any]], understood: bool = True, reason: str | None = None) -> Plan:
    return Plan(
        understood=understood,
        reason=reason,
        calls=[PlanCall(name=c, params={}) if isinstance(c, str) else PlanCall(name=c[0], params=dict(c[1])) for c in calls],
    )


def cut_off(text: str, model: str = "fake-model") -> TextResult:
    return TextResult(text=text, model=model, stop_reason="max_tokens", usage=Usage(300, 600), latency_ms=1)


def fake_prompt_hash(system: str, user: str) -> str:
    return hashlib.sha256((system + "\x1f" + user).encode("utf-8")).hexdigest()


def fake_record_llm_call(
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
            "input_tokens": usage.input_tokens if usage else None,
            "output_tokens": usage.output_tokens if usage else None,
            "latency_ms": latency_ms,
            "stop_reason": stop_reason,
            "outcome": outcome,
            "error": error,
            "qa_id": qa_id,
        },
    )
    return call_id


def _audit_is_stub() -> bool:
    try:
        llm_audit.prompt_hash("probe", "probe")
        with Store.open(":memory:") as probe:
            llm_audit.record_llm_call(
                probe,
                purpose="probe",
                provider="probe",
                model=None,
                prompt_hash="",
                usage=None,
                latency_ms=None,
                stop_reason=None,
                outcome="ok",
            )
    except NotImplementedError:
        return True
    return False


def install_fake_audit(monkeypatch: pytest.MonkeyPatch) -> bool:
    if not _audit_is_stub():
        return False
    monkeypatch.setattr(llm_audit, "prompt_hash", fake_prompt_hash)
    monkeypatch.setattr(llm_audit, "record_llm_call", fake_record_llm_call)
    return True


@pytest.fixture
def audit_shim(monkeypatch: pytest.MonkeyPatch) -> bool:
    return install_fake_audit(monkeypatch)

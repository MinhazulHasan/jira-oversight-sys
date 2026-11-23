"""The Q&A eval (Planning.md section 8): tests/eval/questions.yaml run through
ask(), scored on the plan, the outcome and the numbers.

CONTRACT (stub; the ask build implements it).

questions.yaml entries: {id, question, expected_calls: [tool names in order],
expected_outcome, expected_numbers: [strings that must appear in the answer],
expected_keys: [issue keys that must appear]}. Expected numbers come from
running the catalogue queries directly on the demo data (code, not the model).

run_qa_eval(path, *, store, process, rules, settings, provider, out_path, now, project) -> EvalReport
- For each case: ask(); record plan_ok (names match exactly), outcome_ok,
  numbers_ok (every expected number is in the answer text, or there is none
  expected), keys_ok, latency_s.
- Writes a Markdown report (one row per case, then the rates) to out_path
  (reports/qa_eval.md by default).
- EvalReport.passed is True when the plan rate >= 0.90 and every answered case
  has numbers_ok and keys_ok (the Phase 6 done-when), and median latency <= 5 s.
`python -m oversight eval [--live]` runs it with the scripted provider by
default (replay, offline, in CI) or the configured provider with --live.

The dataset header. Expected values hold only on the data they were computed
on, so the shipped questions.yaml is a mapping whose `dataset` names that data:
{project, seed, issues, now}, the demo seeded with that seed and issue count at
`now`, then derived and rules-run at the same time. load_dataset() reads it and
build_dataset() rebuilds it in any store; the CLI does that in a throwaway
in-memory store and asks every question at `now`, so the eval neither depends
on nor writes to the snapshot. A questions file with no header (a plain list)
runs against the configured database at the real clock, as before.

Implementation notes
- The shipped questions.yaml is a mapping {dataset, cases}; a plain list of
  cases (the form Planning.md shows) is accepted too.
  load_cases validates every entry before any question is asked (ids unique,
  outcomes from the five the service returns, expected_calls from the
  catalogue, keys shaped like issue keys) and raises ConfigError naming the
  case, so a typo fails the run instead of scoring a case wrongly.
- A number or key counts as "in the answer" only as a whole token: "15" is not
  found in "150" or "0.15", "DEMO-3" is not found in "DEMO-30", and "1,150"
  equals "1150". Only the answer text is searched, never the facts table or
  the outcome message: the eval scores what a reader is shown as the answer.
- Each ask runs as user "eval" against `project` (default
  settings.jira_projects[0]), with the clock the caller injects (the dataset's
  `now` when the file has a header, else the real clock). The scripted provider writes fact values only, so
  not_verified never occurs offline; the tests cover it with a fake provider.
- An empty case list cannot pass: every rate is 0 and passed is False.
- The report is Markdown: one row per case, the rates, then one block per case
  that missed something (expected against actual plan, the outcome message,
  the numbers and keys not found), so a nightly run says what to look at.
"""
from __future__ import annotations

import re
import statistics
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from oversight.ask.catalog import CATALOG
from oversight.ask.service import AskResponse, ask
from oversight.config import ProcessConfig, load_yaml
from oversight.errors import ConfigError
from oversight.llm.provider import LLMProvider
from oversight.logs import get_logger
from oversight.rules.catalog import RulesConfig
from oversight.settings import Settings
from oversight.store.db import Store
from oversight.timeutil import parse_ts, utcnow

OUTCOMES: tuple[str, ...] = ("answered", "not_understood", "no_data", "not_measurable", "not_verified")
# The Phase 6 done-when (Planning.md): the plan is right at least 90% of the time, answers take 5 s or less.
PLAN_RATE_TARGET = 0.90
MEDIAN_LATENCY_TARGET_S = 5.0
EVAL_USER = "eval"

_CASE_KEYS = frozenset({"id", "question", "expected_calls", "expected_outcome", "expected_numbers", "expected_keys"})
_DATASET_KEYS = ("project", "seed", "issues", "now")
_REQUIRED_KEYS = ("id", "question", "expected_calls", "expected_outcome")
_ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")
# "1,150" and "1150" are the same number: thousands separators are dropped on both sides.
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}\b)")
# The longest answer excerpt the report quotes.
_EXCERPT = 240

log = get_logger("eval")


@dataclass(frozen=True)
class EvalDataset:
    """The data a questions file's expected values were computed on (its `dataset` header)."""

    project: str
    seed: int
    issues: int
    now: datetime


@dataclass
class EvalCase:
    id: str
    question: str
    expected_calls: list[str]
    expected_outcome: str
    expected_numbers: list[str] = field(default_factory=list)
    expected_keys: list[str] = field(default_factory=list)


@dataclass
class EvalResult:
    id: str
    plan_ok: bool
    outcome_ok: bool
    numbers_ok: bool
    keys_ok: bool
    outcome: str
    latency_s: float
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """Nothing to look at: plan, outcome, numbers and keys all as expected."""
        return self.plan_ok and self.outcome_ok and self.numbers_ok and self.keys_ok


@dataclass
class EvalReport:
    results: list[EvalResult]
    plan_rate: float
    outcome_rate: float
    median_latency_s: float
    passed: bool
    report_path: Path | None = None

    def render(self) -> str:
        """The Markdown report: one row per case, the rates, then what each miss looked like."""
        results = self.results
        n = len(results)
        plans = sum(1 for r in results if r.plan_ok)
        outcomes = sum(1 for r in results if r.outcome_ok)
        answered = [r for r in results if r.outcome == "answered"]
        verified = sum(1 for r in answered if r.numbers_ok and r.keys_ok)
        verdict = "PASSED" if self.passed else "FAILED"
        lines = [
            "# Q&A eval",
            "",
            f"{n} cases, {plans}/{n} plans right ({self.plan_rate:.0%}), {outcomes}/{n} outcomes right "
            f"({self.outcome_rate:.0%}), median latency {self.median_latency_s:.2f} s: **{verdict}**.",
        ]
        first = results[0].detail if results else {}
        if first.get("model") or first.get("asked_at"):
            lines.append(f"Asked at {first.get('asked_at') or '?'} with model {first.get('model') or '?'}.")
        lines += [
            "",
            "| # | Case | Question | Expected | Outcome | Plan | Numbers | Keys | Latency (s) |",
            "|---:|---|---|---|---|:---:|:---:|:---:|---:|",
        ]
        for index, r in enumerate(results, start=1):
            question = _cell(str(r.detail.get("question") or r.id))
            expected = str(r.detail.get("expected_outcome") or "?")
            lines.append(
                f"| {index} | {_cell(r.id)} | {question} | {expected} | {r.outcome} | {_mark(r.plan_ok)} "
                f"| {_mark(r.numbers_ok)} | {_mark(r.keys_ok)} | {r.latency_s:.2f} |"
            )
        lines += [
            "",
            "## Rates",
            "",
            f"- Plan right: {plans}/{n} ({self.plan_rate:.1%}); target {PLAN_RATE_TARGET:.0%}.",
            f"- Outcome right: {outcomes}/{n} ({self.outcome_rate:.1%}).",
            f"- Answered cases with every expected number and key: {verified}/{len(answered)}.",
            f"- Median latency: {self.median_latency_s:.2f} s; target {MEDIAN_LATENCY_TARGET_S:.0f} s.",
            f"- Result: {verdict}" + ("." if self.passed else f" ({_why_failed(self, verified, len(answered))})."),
        ]
        misses = [r for r in results if not r.ok]
        if misses:
            lines += ["", "## Misses", ""]
            for r in misses:
                lines.extend(_miss_lines(r))
                lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def load_cases(path: Path) -> list[EvalCase]:
    """The cases in a questions file, validated; ConfigError names the first problem."""
    data = load_yaml(path)
    if isinstance(data, Mapping) and "cases" in data:
        data = data["cases"]
    name = Path(path).name
    if not isinstance(data, list) or not data:
        raise ConfigError(f"{name}: expected a non-empty list of cases")
    cases: list[EvalCase] = []
    seen: set[str] = set()
    for index, entry in enumerate(data, start=1):
        case = _case(entry, where=f"{name} case {index}")
        if case.id in seen:
            raise ConfigError(f"{name} case {index}: duplicate id {case.id!r}")
        seen.add(case.id)
        cases.append(case)
    return cases


def load_dataset(path: Path) -> EvalDataset | None:
    """The file's `dataset` header, validated; None for a file without one (a plain list of cases)."""
    data = load_yaml(path)
    if not isinstance(data, Mapping) or "dataset" not in data:
        return None
    name = Path(path).name
    header = data["dataset"]
    if not isinstance(header, Mapping):
        raise ConfigError(f"{name}: dataset must be a mapping of {', '.join(_DATASET_KEYS)}")
    missing = [key for key in _DATASET_KEYS if key not in header]
    unknown = sorted(str(key) for key in header if key not in _DATASET_KEYS)
    if missing or unknown:
        raise ConfigError(f"{name}: dataset needs exactly {', '.join(_DATASET_KEYS)}"
                          + (f"; missing {', '.join(missing)}" if missing else "")
                          + (f"; unknown {', '.join(unknown)}" if unknown else ""))
    project = header["project"]
    if not isinstance(project, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,19}", project):
        raise ConfigError(f"{name}: dataset project must be a project key such as DEMO, not {project!r}")
    for key in ("seed", "issues"):
        if isinstance(header[key], bool) or not isinstance(header[key], int) or header[key] < (0 if key == "seed" else 1):
            raise ConfigError(f"{name}: dataset {key} must be a whole number, not {header[key]!r}")
    try:
        # YAML reads an unquoted timestamp as a datetime; parse_ts takes either form.
        moment = parse_ts(header["now"])
    except (TypeError, ValueError):
        moment = None
    if moment is None:
        raise ConfigError(f"{name}: dataset now must be a UTC timestamp such as 2026-11-04T12:00:00Z")
    return EvalDataset(project=project, seed=int(header["seed"]), issues=int(header["issues"]), now=moment)


def build_dataset(store: Store, process: ProcessConfig, rules: RulesConfig, dataset: EvalDataset) -> None:
    """Rebuild the dataset in `store`: seed the demo project, derive and run the rules, all at dataset.now.

    The demo writes its sprints and points under its own field ids, so the rules
    read them through the demo's FieldsConfig, whatever this site's fields.yaml says.
    """
    from oversight.demo import DEMO_FIELDS, seed_demo
    from oversight.derive.run import run_derive
    from oversight.rules.engine import run_rules

    def clock() -> datetime:
        return dataset.now

    seed_demo(store, now=dataset.now, seed=dataset.seed, project=dataset.project, issues=dataset.issues, reset=True)
    run_derive(store, process, projects=[dataset.project], now=clock)
    run_rules(store, process, rules, DEMO_FIELDS, projects=[dataset.project], now=clock)


def run_qa_eval(
    path: Path,
    *,
    store: Store,
    process: ProcessConfig,
    rules: RulesConfig,
    settings: Settings,
    provider: LLMProvider,
    out_path: Path,
    now: Callable[[], datetime] = utcnow,
    project: str | None = None,
) -> EvalReport:
    """Ask every case, score it, write the Markdown report to out_path and return the report."""
    cases = load_cases(path)
    results: list[EvalResult] = []
    for case in cases:
        response = ask(
            case.question,
            store=store,
            process=process,
            rules=rules,
            settings=settings,
            provider=provider,
            user=EVAL_USER,
            project=project,
            now=now,
        )
        results.append(_score(case, response))
    report = _report(results, Path(out_path))
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.render(), encoding="utf-8")
    log.info(
        "qa_eval_finished",
        cases=len(results),
        plan_rate=round(report.plan_rate, 3),
        outcome_rate=round(report.outcome_rate, 3),
        median_latency_s=round(report.median_latency_s, 3),
        passed=report.passed,
        report=str(out),
    )
    return report


# --- cases -----------------------------------------------------------------------------------


def _case(entry: Any, *, where: str) -> EvalCase:
    if not isinstance(entry, Mapping):
        raise ConfigError(f"{where}: each case must be a mapping, not {type(entry).__name__}")
    unknown = sorted(str(key) for key in entry if key not in _CASE_KEYS)
    if unknown:
        raise ConfigError(f"{where}: unknown keys {', '.join(unknown)} (allowed: {', '.join(sorted(_CASE_KEYS))})")
    missing = [key for key in _REQUIRED_KEYS if key not in entry]
    if missing:
        raise ConfigError(f"{where}: missing {', '.join(missing)}")
    case_id = entry["id"]
    if isinstance(case_id, bool) or not isinstance(case_id, (str, int)) or not str(case_id).strip():
        raise ConfigError(f"{where}: id must be a non-empty string")
    question = entry["question"]
    if not isinstance(question, str) or not question.strip():
        raise ConfigError(f"{where}: question must be a non-empty string")
    calls = _strings(entry["expected_calls"], where, "expected_calls")
    unknown_tools = [name for name in calls if name not in CATALOG]
    if unknown_tools:
        raise ConfigError(f"{where}: expected_calls names unknown tools {', '.join(unknown_tools)}")
    outcome = entry["expected_outcome"]
    if outcome not in OUTCOMES:
        raise ConfigError(f"{where}: expected_outcome must be one of {', '.join(OUTCOMES)}, not {outcome!r}")
    numbers = [_number_text(value, where) for value in _list(entry.get("expected_numbers"), where, "expected_numbers")]
    keys = _strings(entry.get("expected_keys"), where, "expected_keys")
    bad_keys = [key for key in keys if not _ISSUE_KEY.match(key)]
    if bad_keys:
        raise ConfigError(f"{where}: expected_keys must be issue keys such as DEMO-12, not {', '.join(bad_keys)}")
    return EvalCase(
        id=str(case_id).strip(),
        question=question.strip(),
        expected_calls=calls,
        expected_outcome=outcome,
        expected_numbers=numbers,
        expected_keys=keys,
    )


def _list(value: Any, where: str, name: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"{where}: {name} must be a list")
    return value


def _strings(value: Any, where: str, name: str) -> list[str]:
    items = _list(value, where, name)
    if not all(isinstance(item, str) and item.strip() for item in items):
        raise ConfigError(f"{where}: {name} must hold non-empty strings")
    return [item.strip() for item in items]


def _number_text(value: Any, where: str) -> str:
    # YAML reads `736` as an int and `1.5` as a float; a quoted "16%" stays text.
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ConfigError(f"{where}: expected_numbers must hold numbers or strings, not {value!r}")
    text = str(value).strip()
    if not text:
        raise ConfigError(f"{where}: expected_numbers must not hold an empty string")
    return text


# --- scoring ----------------------------------------------------------------------------------


def _score(case: EvalCase, response: AskResponse) -> EvalResult:
    text = response.answer or ""
    missing_numbers = [number for number in case.expected_numbers if not _has_number(text, number)]
    missing_keys = [key for key in case.expected_keys if not _has_key(text, key)]
    planned = [call.name for call in response.plan]
    return EvalResult(
        id=case.id,
        plan_ok=planned == case.expected_calls,
        outcome_ok=response.outcome == case.expected_outcome,
        numbers_ok=not missing_numbers,
        keys_ok=not missing_keys,
        outcome=response.outcome,
        latency_s=response.latency_ms / 1000,
        detail={
            "question": case.question,
            "expected_calls": list(case.expected_calls),
            "planned": [{"name": call.name, "params": dict(call.params)} for call in response.plan],
            "expected_outcome": case.expected_outcome,
            "message": response.message,
            "answer": response.answer,
            "missing_numbers": missing_numbers,
            "missing_keys": missing_keys,
            "model": response.model,
            "asked_at": response.asked_at,
            "qa_id": response.qa_id,
        },
    )


def _has_number(text: str, expected: str) -> bool:
    """The number as a whole token: not inside a longer number ("15" in "150", "4.0" in "24.0")."""
    wanted = _THOUSANDS.sub("", expected)
    haystack = _THOUSANDS.sub("", text)
    return re.search(rf"(?<![\d.,]){re.escape(wanted)}(?!\d|[.,]\d)", haystack) is not None


def _has_key(text: str, key: str) -> bool:
    """The key as a whole token: "DEMO-3" is not found in "DEMO-30" or "XDEMO-3"."""
    return re.search(rf"(?<![A-Z0-9_-]){re.escape(key)}(?![\d_-])", text) is not None


def _report(results: list[EvalResult], out_path: Path) -> EvalReport:
    n = len(results)
    plan_rate = sum(1 for r in results if r.plan_ok) / n if n else 0.0
    outcome_rate = sum(1 for r in results if r.outcome_ok) / n if n else 0.0
    median_latency = statistics.median(r.latency_s for r in results) if n else 0.0
    verified = all(r.numbers_ok and r.keys_ok for r in results if r.outcome == "answered")
    passed = n > 0 and plan_rate >= PLAN_RATE_TARGET and verified and median_latency <= MEDIAN_LATENCY_TARGET_S
    return EvalReport(
        results=results,
        plan_rate=plan_rate,
        outcome_rate=outcome_rate,
        median_latency_s=median_latency,
        passed=passed,
        report_path=out_path,
    )


# --- rendering --------------------------------------------------------------------------------


def _mark(ok: bool) -> str:
    return "ok" if ok else "MISS"


def _cell(text: str) -> str:
    """One Markdown table cell: pipes escaped, one line."""
    return " ".join(text.split()).replace("|", "\\|")


def _excerpt(text: str | None) -> str:
    if not text:
        return "(none)"
    flat = _cell(text)
    return flat if len(flat) <= _EXCERPT else flat[: _EXCERPT - 3] + "..."


def _why_failed(report: EvalReport, verified: int, answered: int) -> str:
    reasons = []
    if not report.results:
        reasons.append("no cases")
    if report.plan_rate < PLAN_RATE_TARGET:
        reasons.append(f"plan rate {report.plan_rate:.1%} is below {PLAN_RATE_TARGET:.0%}")
    if verified < answered:
        reasons.append(f"{answered - verified} answered case(s) missed a number or key")
    if report.median_latency_s > MEDIAN_LATENCY_TARGET_S:
        reasons.append(f"median latency {report.median_latency_s:.2f} s is over {MEDIAN_LATENCY_TARGET_S:.0f} s")
    return "; ".join(reasons) or "unknown"


def _miss_lines(r: EvalResult) -> list[str]:
    d = r.detail
    lines = [f"### {r.id}: {_cell(str(d.get('question') or ''))}".rstrip(": ")]
    if not r.plan_ok:
        planned = ", ".join(f"{c['name']}({_params(c.get('params'))})" for c in d.get("planned", [])) or "(no calls)"
        lines.append(f"- Plan: expected {', '.join(d.get('expected_calls') or []) or '(no calls)'}; got {planned}.")
    if not r.outcome_ok:
        lines.append(f"- Outcome: expected {d.get('expected_outcome')}; got {r.outcome}.")
    if d.get("message"):
        lines.append(f"- Message: {_cell(str(d['message']))}")
    if not r.numbers_ok:
        lines.append(f"- Numbers not found: {', '.join(d.get('missing_numbers') or [])}.")
    if not r.keys_ok:
        lines.append(f"- Keys not found: {', '.join(d.get('missing_keys') or [])}.")
    lines.append(f"- Answer: {_excerpt(d.get('answer'))}")
    return lines


def _params(params: Any) -> str:
    if not isinstance(params, Mapping):
        return ""
    return ", ".join(f"{key}={value}" for key, value in params.items())

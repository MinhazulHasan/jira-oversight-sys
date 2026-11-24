"""The rule catalogue: config/rules.yaml as validated models."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, model_validator

from oversight.config import load_yaml
from oversight.errors import ConfigError

Kind = Literal["rule", "warning"]
Severity = Literal["breach", "warning"]
Scope = Literal["issue", "sprint"]


class AppliesTo(BaseModel):
    """Which issues a rule looks at. None means no filter on that attribute."""

    model_config = ConfigDict(extra="forbid")

    status_category: list[Literal["new", "indeterminate", "done"]] | None = None
    # Issue type names (case-insensitive). Naming a type here also overrides defaults.exclude_types.
    issue_type: list[str] | None = None


class RuleDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exclude_types: list[str] = ["Epic"]
    history_days: int = Field(default=180, ge=1)
    min_history: int = Field(default=20, ge=1)
    lookback_days: int | None = Field(default=None, ge=1)


# A rule's id: R (a breach rule) or W (a warning), a word, and two or three digits (R-EFF-01, W-CUS-104).
RULE_ID_PATTERN = r"^[RW]-[A-Z]+-\d{2,3}$"


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=RULE_ID_PATTERN)
    title: str
    description: str = ""
    kind: Kind
    severity: Severity
    # A key of checks.CHECKS (scope issue) or checks.SPRINT_CHECKS (scope sprint).
    check: str
    params: dict[str, Any] = Field(default_factory=dict)
    applies_to: AppliesTo | None = None
    # Evidence names from process.yaml; any that is null there makes the rule "not measurable".
    requires_evidence: list[str] = Field(default_factory=list)
    # Stages the project's workflow must map; otherwise "not measurable".
    requires_stages: list[str] = Field(default_factory=list)
    # For warnings: the breach rule this warning foreshadows ("warned on X, breached on Y").
    escalates_to: str | None = None
    scope: Scope = "issue"
    enabled: bool = True

    _board_version: str | None = PrivateAttr(default=None)
    # The fingerprint of its params on the board (settings_hash); None: they are the catalogue's.
    _settings_hash: str | None = PrivateAttr(default=None)
    # Whether anything of it differs from rules.yaml on the board.
    _customised: bool = PrivateAttr(default=False)
    # Its params in the catalogue, which name its findings (identity_params); None: its own params are the catalogue's.
    _identity_params: dict[str, Any] | None = PrivateAttr(default=None)
    # Whether an admin added it to the board: a custom rule, not one of rules.yaml's.
    _custom: bool = PrivateAttr(default=False)

    @property
    def custom(self) -> bool:
        return self._custom

    @property
    def settings_hash(self) -> str | None:
        return self._settings_hash

    @property
    def customised(self) -> bool:
        """Whether a board's settings change anything of it: on or off, its severity, or a param."""
        return self._customised

    @property
    def identity_params(self) -> dict[str, Any]:
        return self._identity_params if self._identity_params is not None else self.params


class RulesConfig(BaseModel):
    """The whole catalogue, validated."""

    model_config = ConfigDict(extra="forbid")

    version: str
    defaults: RuleDefaults = Field(default_factory=RuleDefaults)
    rules: list[Rule]

    # True for one board's rules (oversight.rules.settings.effective_rules), which are never a catalogue themselves.
    _board_rules: bool = PrivateAttr(default=False)
    # Custom rules the board deleted, by id: their findings keep their titles.
    _retired: dict[str, Rule] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> RulesConfig:
        # Imported here: checks.py imports this module for RulesConfig.
        from oversight.rules.checks import CHECK_NAMES, SPRINT_CHECK_NAMES

        ids = [rule.id for rule in self.rules]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"duplicate rule ids: {', '.join(duplicates)}")
        by_id = {rule.id: rule for rule in self.rules}
        for rule in self.rules:
            registry = SPRINT_CHECK_NAMES if rule.scope == "sprint" else CHECK_NAMES
            if rule.check not in registry:
                raise ValueError(f"{rule.id}: unknown {rule.scope} check {rule.check!r}")
            if rule.escalates_to is not None:
                target = by_id.get(rule.escalates_to)
                if target is None:
                    raise ValueError(f"{rule.id}: escalates_to names unknown rule {rule.escalates_to!r}")
                if target.kind != "rule":
                    raise ValueError(f"{rule.id}: escalates_to must name a rule of kind 'rule'")
                if rule.kind != "warning":
                    raise ValueError(f"{rule.id}: only a warning may escalate to a rule")
        return self

    @classmethod
    def load(cls, path: Path) -> RulesConfig:
        data = load_yaml(path)
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            raise ConfigError(f"{Path(path).name} is invalid:\n{exc}") from exc

    def by_id(self, rule_id: str) -> Rule:
        for rule in self.rules:
            if rule.id == rule_id:
                return rule
        raise ConfigError(f"rule {rule_id!r} is not in the catalogue")

    def find(self, rule_id: str) -> Rule | None:
        found = next((rule for rule in self.rules if rule.id == rule_id), None)
        return found if found is not None else self._retired.get(rule_id)

    def title_of(self, rule_id: str) -> str:
        """The title a finding of the rule shows wherever it is listed."""
        rule = self.find(rule_id)
        return rule.title if rule is not None and rule.title else rule_id

    @property
    def enabled_rules(self) -> list[Rule]:
        return [rule for rule in self.rules if rule.enabled]

    @property
    def known(self) -> list[Rule]:
        return [*self.rules, *self._retired.values()]

    @property
    def is_board_rules(self) -> bool:
        """Whether these are one board's rules, its settings applied, rather than the catalogue itself."""
        return self._board_rules

    def rule_version(self, rule: Rule) -> str:
        return rule._board_version or self.version

    def warnings_for(self, breach_rule_id: str) -> list[Rule]:
        """Warning rules whose escalates_to is this breach rule."""
        return [rule for rule in self.rules if rule.escalates_to == breach_rule_id]

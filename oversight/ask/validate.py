"""Numbers and keys in an answer must exist in the facts (Planning.md principle 3).

The validator also produces the citations: each verified number or key in the
text is bound to the fact row and column it came from, which is what the Ask
page's tick-and-tie lines draw.

validate(text, facts, plan) -> Check
1. Issue keys: KEY = \\b[A-Z][A-Z0-9_]+-\\d+\\b. Every key in the text must appear
   in some fact row (any column of kind issue_key, or the issue_key/epic_key
   columns, or sample_keys lists). Unknown keys are problems ("unknown key X").
2. Dates: DATE = \\b\\d{4}-\\d{2}-\\d{2}\\b, and the spelled forms "4 Nov 2026" /
   "Nov 2026". A date in the text must match a date column value in the facts
   (its ISO form, or its spelled form). Matched dates and keys are removed from
   the text before the number scan.
3. Numbers: NUM = -?(?:\\d{1,3}(?:,\\d{3})+(?:\\.\\d+)?%?|\\d+(?:\\.\\d+)?%?), with a
   (?<![\\w.-]) lookbehind: a leading minus belongs to the number, and no match
   starts inside another number or word. Commas are dropped before comparing. A number in the
   text is allowed when its normalised form is in the display forms of any
   number in the facts or in the plan parameters, or equals a fact's row
   count. Otherwise "unverified number X".
   A number written in words (SPELLED: "two" to "nineteen", the tens, their
   compounds such as "twenty-one", and hundreds and thousands of them, in any
   case) is checked the same way, as its value, and cited as written
   ("Seventeen"): 18 Nov 2026, a poem wrote its five counts in words and none
   was checked or cited. "One" on its own is left alone ("the one in progress"
   is no count), and so are ordinals ("second") and words such as "dozen",
   "half" and "twice". Code-written text quotable as it is allows the numbers
   it writes in words too ("Phase Two").
4. display_forms(value) -> the strings a number may legitimately be written
   as: the raw repr, rounded to 0, 1 and 2 decimals, with thousands separators,
   and for a share (0..1) also its whole-percent and one-decimal percent forms
   ("16%", "15.8%"); hours and ratios never gain a percent form. Column kinds
   decide which forms apply (share -> percent forms; others not).
5. Bindings: for each verified key/number occurrence (in text order) a
   Binding(marker, text, fact_ref, row_index, column); numbers from plan
   parameters or row counts get fact_ref of the fact and column "params" /
   "row_count" with row_index -1. Markers start at 1. A key is bound to the
   first place that holds it. A number or date that only one place holds is
   bound there; when several places hold its value (a 3 in three people's
   rows), it is bound to the one its context points at (the tick-and-tie line
   must reach the row the figure is about, never merely the first 3):
   - `hints` first: an answer written in code (scorecards.code_built) maps the
     start of each figure it wrote to its row and column, and that is final
     (a hint binds only a number the facts allow anyway);
   - otherwise the places are narrowed, each step kept only when some place
     survives it: (a) to the rows of the figure's entities: in a Markdown table
     the entities named anywhere in the figure's table row, in prose those
     named before it in the same sentence (a sentence ends at . ; ! or ?
     followed by a space, or at a line break; a dot inside a name ends
     nothing), people and issue keys first, then a fact's other row labels,
     each kind nearest first; (b) to the fact of the figures already bound in
     the same sentence, or above it in the same table column; (c) in a table,
     to the fact columns whose label or key matches the column's header; (d)
     in a table, to the fact of the figures already bound in the same table;
     then (e) the first place left, in the facts' order, as before.
   An entity is a text cell of a fact row as the answer writes it: a person's
   name (person_id/name, assignee_id/assignee, or `people`, so a name also
   reaches the rows that hold only the person's id), an issue or epic key, or
   another label such as a status group. A name of two words or more is found
   whole, in any case and with or without its accents (people.fold); a one-word
   label only as the data writes it ("will" is not Will), as people.named_in
   finds names. `context`: the answer as written, when `text` has names masked
   (people level): entities are read there, figures in `text`.
Check.ok is True when there are no problems. Pure function: no I/O.

Where numbers may come from, exactly: columns of a number kind (number, count,
hours, share, ratio); text columns that code wrote (a finding's message, a
group label such as "Sprint 4427", a note), quoted as they are; the fact's
`as_of` and note; the plan's parameters; each fact's row count. Ticket text
(the summary and epic_title columns) never supplies a number: anyone who can
edit a ticket can write one there (tests/test_injection.py). Nor does a rule's
title or description (the rule_title, title and description columns, and
wherever code quotes one in its own text, a note or a candidate's title: the
board's rules, `rules`, say which they are): an admin writes a custom rule's
(JO-20), the AI drafts it, and "Bugs untouched for 14 days" is no count of
anything. Dates come from date columns, as_of and code-written text, and a
verified date also allows its year as a bare number. Rule ids such as R-EST-01
are vocabulary, not data, and are ignored; so is a rule's title the answer
quotes whole ("'Bugs untouched for 14 days' has 4 open findings": the 4 is
checked, the 14 is the rule's name). Verified dates are bound like keys and
numbers.
"""
from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from oversight.api.people import PERSON_ID, fold
from oversight.ask.catalog import Facts
from oversight.llm.provider import Plan
from oversight.rules.catalog import RulesConfig

KEY = re.compile(r"\b[A-Z][A-Z0-9_]+-\d+\b")
DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
# The sign belongs to the number: "-4.5" is one token, so a real negative value
# verifies whole and an invented "-40" is checked instead of skipped. "." in the
# lookbehind keeps a match from starting mid-number (the "5" of "-4.5").
NUM = re.compile(r"(?<![\w.-])-?(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?%?|\d+(?:\.\d+)?%?)")

# Numbers written in words (the module docstring, 3). Longest first in each alternation, so "nineteen" is never read
# as "nine" and the rest.
_UNIT_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}
_TEEN_WORDS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
               "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TEN_WORDS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
              "ninety": 90}
_NUMBER_WORDS = {**_UNIT_WORDS, **_TEEN_WORDS, **_TEN_WORDS, "hundred": 100, "thousand": 1000}


def _either(words: Mapping[str, int]) -> str:
    return "|".join(sorted(words, key=len, reverse=True))


_UNDER_HUNDRED = (rf"(?:(?:{_either(_TEN_WORDS)})(?:[\s-]+(?:{_either(_UNIT_WORDS)}))?"
                  rf"|{_either(_TEEN_WORDS)}|{_either(_UNIT_WORDS)})")
_UNDER_THOUSAND = rf"(?:(?:a|{_either(_UNIT_WORDS)})\s+hundred(?:\s+(?:and\s+)?{_UNDER_HUNDRED})?|{_UNDER_HUNDRED})"
SPELLED = re.compile(
    rf"\b(?:(?:a|{_UNDER_THOUSAND})\s+thousand(?:,?\s+(?:and\s+)?{_UNDER_THOUSAND})?|{_UNDER_THOUSAND})\b",
    re.IGNORECASE,
)

# Column kinds whose values are numbers the answer may quote.
NUMBER_KINDS = frozenset({"number", "count", "hours", "share", "ratio"})
# Text columns that hold ticket text: untrusted, never a source of numbers or dates, nor a label a figure is placed by.
TICKET_TEXT_COLUMNS = frozenset({"summary", "epic_title"})
# Text columns that are never a source of numbers or dates: ticket text, and a rule's title and description (an admin
# writes a custom rule's: JO-20). A rule's title still labels its row, so a figure beside it is cited there.
UNTRUSTED_TEXT_COLUMNS = TICKET_TEXT_COLUMNS | {"rule_title", "title", "description"}

_RULE_ID = re.compile(r"\b[RW]-[A-Z]+-\d{2,3}\b")
_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}")
# An ISO date, optionally with the time part the storage format carries, so a
# timestamp quoted whole is checked by its date and leaves no stray numbers.
_ISO_DATETIME = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:?\d{2})?)?")
_MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?"
    r"|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
_SPELLED_DAY = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH})\.?\s+(\d{{4}})\b", re.IGNORECASE)
_SPELLED_MONTH = re.compile(rf"\b({_MONTH})\.?\s+(\d{{4}})\b", re.IGNORECASE)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


@dataclass(frozen=True)
class Binding:
    marker: int
    text: str
    fact_ref: str
    row_index: int
    column: str


@dataclass
class Check:
    ok: bool
    problems: list[str] = field(default_factory=list)
    bindings: list[Binding] = field(default_factory=list)


@dataclass(frozen=True)
class _Source:
    """Where a verified value came from (what a Binding points at)."""

    fact_ref: str
    row_index: int
    column: str


def display_forms(value: object, kind: str = "number") -> set[str]:
    number = _number(value)
    if number is None:
        return set()
    forms: set[str] = set()
    if isinstance(value, int) and not isinstance(value, bool):
        forms.add(str(value))
    elif isinstance(value, float):
        forms.add(repr(value))
    elif isinstance(value, str):
        forms.add(value.strip().replace(",", ""))
    for decimals in (0, 1, 2):
        for rounded in _rounded(number, decimals):
            forms.update((rounded, _with_commas(rounded), _trim(rounded), _trim(_with_commas(rounded))))
    if kind == "share" and 0 <= number <= 1:
        # 0.158 may be written as 16%, 15.8%, or "16 per cent".
        for decimals in (0, 1):
            for rounded in _rounded(number * 100, decimals):
                forms.update((rounded + "%", _trim(rounded) + "%", rounded, _trim(rounded)))
    return forms


def _rounded(number: float, decimals: int) -> set[str]:
    """The number at `decimals` places, rounded half to even (Python's default) and half up (a reader's)."""
    half_up = Decimal(repr(number)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    return {f"{number:.{decimals}f}", f"{half_up:.{decimals}f}"}


def _with_commas(form: str) -> str:
    whole, _, fraction = form.partition(".")
    grouped = f"{int(whole):,}" if whole.lstrip("-").isdigit() else whole
    return f"{grouped}.{fraction}" if fraction else grouped


def validate(text: str, facts: list[Facts], plan: Plan | None,
             hints: Mapping[int, tuple[str, int, str]] | None = None, *, context: str | None = None,
             people: Sequence[tuple[str, str]] = (), rules: RulesConfig | None = None) -> Check:
    """hints: where each figure an answer written in code starts -> (fact ref, row index, column) it was read from.
    context: the answer as written, when `text` is that answer with names masked (just as long): the entities that
    place a figure are read there. people: (person id, display name) of everyone the answer may name, so a name also
    reaches the rows that hold only that person's id. rules: the board's rules the facts were read with, whose titles
    and descriptions are words, never figures, wherever the facts or the answer quote them (see the module
    docstring)."""
    titles, words = _rule_words(rules)
    keys, dates, numbers = _allowed(facts, plan, words)
    found: list[_Found] = []
    problems: list[str] = []
    # A rule's title quoted whole is vocabulary, not data (masked first: a title may name another rule's id); so are
    # rule ids, masked so "FLOW-02" is never read as a key.
    work = titles.sub(_blank, text) if titles is not None else text
    work = _RULE_ID.sub(_blank, work)

    for match in KEY.finditer(work):
        _record(match.start(), match.group(), keys.get(match.group()), "key", found, problems, "unknown key")
    work = KEY.sub(_blank, work)

    for match in _ISO_DATETIME.finditer(work):
        _record(match.start(), match.group(), dates.get(match.group()[:10]), "date", found, problems, "unverified date")
    work = _ISO_DATETIME.sub(_blank, work)
    for match in _SPELLED_DAY.finditer(work):
        day = _spelled_day(match.group(1), match.group(2), match.group(3))
        _record(match.start(), match.group(), dates.get(day or ""), "date", found, problems, "unverified date")
    work = _SPELLED_DAY.sub(_blank, work)
    for match in _SPELLED_MONTH.finditer(work):
        prefix = _spelled_month(match.group(1), match.group(2))
        month = [source for day, held in dates.items() if prefix and day.startswith(prefix) for source in held]
        _record(match.start(), match.group(), month, "date", found, problems, "unverified date")
    work = _SPELLED_MONTH.sub(_blank, work)

    for match in _spelled_numbers(work):
        _record(match.start(), match.group(), numbers.get(str(spelled_value(match.group()))), "number", found,
                problems, "unverified number")
    work = SPELLED.sub(_blank, work)

    for match in NUM.finditer(work):
        token = match.group()
        _record(match.start(), token, numbers.get(token.replace(",", "")), "number", found, problems,
                "unverified number")

    found.sort(key=lambda item: item.start)
    written = context if context is not None and len(context) == len(text) else text
    places = _bind(found, written, facts, hints or {}, people)
    bindings = [
        Binding(marker=marker, text=item.token, fact_ref=source.fact_ref, row_index=source.row_index,
                column=source.column)
        for marker, (item, source) in enumerate(zip(found, places, strict=True), start=1)
    ]
    unique = list(dict.fromkeys(problems))
    return Check(ok=not unique, problems=unique, bindings=bindings)


# --- where a figure came from: the place its context points at --------------------------------

# A sentence ends at . ; ! or ? before a space, or at a line break (a Markdown table row is a line of its own).
_SENTENCE_END = re.compile(r"[.;!?](?=\s)|\n")
# A Markdown table's delimiter row: "|---|:--:|", the pipes at either end optional.
_TABLE_DELIMITER = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")
_CELL_PIPE = re.compile(r"(?<!\\)\|")
_WORD = re.compile(r"\w+")
_HEADER_WORD = re.compile(r"[^\W_]+")
# The columns that say whose a row is, each with the column that names that person beside it.
_PERSON_COLUMNS = (("person_id", "name"), ("assignee_id", "assignee"))
# A text cell longer than this is a sentence (a message, a note), not a row's label.
_LABEL_MAX = 120
# An entity's kind, the strongest first: a person or an issue key; a label only one row of its column holds (a status
# group, a sprint); any other label (a measure's name, a status many tickets share).
_IDENTITY, _ROW_LABEL, _LABEL = 0, 1, 2


@dataclass(frozen=True)
class _Found:
    """A verified key, date or number in the text, and every place in the facts that holds its value."""

    start: int
    token: str
    sources: tuple[_Source, ...]  # in the facts' order: the first is where a value with no context is bound
    kind: str  # "key", "date" or "number"


@dataclass(frozen=True)
class _Phrase:
    """A text cell as an answer may write it: folded (people.fold) when it has two words or more, else as written."""

    text: str
    folded: bool


@dataclass(frozen=True)
class _Entity:
    """A person, an issue key or a row label the answer names, and the fact rows ((ref, index)) it stands for."""

    start: int
    end: int
    tier: int
    rows: frozenset[tuple[str, int]]


@dataclass(frozen=True)
class _TableRow:
    """A body row of a Markdown table in the answer: its line, its cells, and the header row's cell texts."""

    table: int
    start: int
    end: int
    cells: tuple[tuple[int, int], ...]
    header: tuple[str, ...]

    def column(self, at: int) -> int:
        return next((index for index, (start, end) in enumerate(self.cells) if start <= at < end), -1)


def _bind(found: list[_Found], text: str, facts: Sequence[Facts], hints: Mapping[int, tuple[str, int, str]],
          people: Sequence[tuple[str, str]]) -> list[_Source]:
    """The place each verified value is bound to, in text order (the module docstring, 5): a hint; a key's first
    place; the only place that holds a value; else the place the value's context points at."""
    reading: _Reading | None = None
    chosen: list[tuple[int, _Source]] = []
    for item in found:
        hinted = hints.get(item.start) if item.kind == "number" else None
        if hinted is not None:
            source = _Source(*hinted)
        elif item.kind == "key" or len(item.sources) == 1:
            source = item.sources[0]
        else:
            reading = reading or _Reading(text, facts, people)
            source = reading.place(item.start, item.sources, chosen)
        chosen.append((item.start, source))
    return [source for _start, source in chosen]


class _Reading:
    """What an answer says around its figures: the entities it names, where its sentences end, its table rows."""

    def __init__(self, text: str, facts: Sequence[Facts], people: Sequence[tuple[str, str]]) -> None:
        self.entities = _entities(text, _Index(facts, people))
        # A dot inside a name ("Jo B. Smith", "Ada.Brooks") ends no sentence: names are blanked before ends are found.
        masked = list(text)
        for entity in self.entities:
            masked[entity.start:entity.end] = " " * (entity.end - entity.start)
        self.ends = [match.start() for match in _SENTENCE_END.finditer("".join(masked))]
        self.rows = _table_rows(text)
        self.row_starts = [row.start for row in self.rows]
        self.labels: dict[str, dict[str, str]] = {}
        for fact in facts:
            self.labels.setdefault(fact.ref, {column.key: column.label for column in fact.columns})

    def sentence(self, at: int) -> int:
        return bisect_left(self.ends, at)

    def row(self, at: int) -> _TableRow | None:
        index = bisect_right(self.row_starts, at) - 1
        return self.rows[index] if index >= 0 and at < self.rows[index].end else None

    def place(self, start: int, sources: Sequence[_Source], chosen: Sequence[tuple[int, _Source]]) -> _Source:
        row = self.row(start)
        if row is None:
            # Prose: the entities named before the figure in its sentence, then the figures bound there before it.
            sentence = self.sentence(start)
            named = [entity for entity in self.entities
                     if entity.start <= start and self.row(entity.start) is None
                     and self.sentence(entity.start) == sentence]
            candidates = _by_entities(sources, sorted(named, key=lambda entity: (entity.tier, start - entity.start)))
            together = [source for at, source in reversed(chosen)
                        if self.row(at) is None and self.sentence(at) == sentence]
            return _by_fact(candidates, together)[0]
        # A table row: the entities named anywhere in it (those before the figure first), then the figures bound
        # above it in its column, then its column's header, then the figures bound anywhere in the table.
        named = [entity for entity in self.entities if row.start <= entity.start < row.end]
        candidates = _by_entities(sources, sorted(
            named, key=lambda entity: (entity.tier, entity.start > start, abs(start - entity.start))))
        column = row.column(start)
        in_table = [(other.column(at), source) for at, source in reversed(chosen)
                    if (other := self.row(at)) is not None and other.table == row.table]
        candidates = _by_fact(candidates, [source for at_column, source in in_table if at_column == column])
        candidates = self._by_header(candidates, row.header[column] if 0 <= column < len(row.header) else "")
        return _by_fact(candidates, [source for _column, source in in_table])[0]

    def _by_header(self, candidates: list[_Source], header: str) -> list[_Source]:
        """The places whose fact column's label or key matches the table column's header, the closest match only."""
        wanted = _header_words(header)
        if not wanted:
            return candidates
        scores = [self._likeness(source, wanted) for source in candidates]
        best = max(scores)
        if not best:
            return candidates
        return [source for source, score in zip(candidates, scores, strict=True) if score == best]

    def _likeness(self, source: _Source, wanted: frozenset[str]) -> int:
        """2 when the column's label or key says the header's words, 1 when one holds the other's words, else 0."""
        if source.row_index < 0:
            return 0
        label = self.labels.get(source.fact_ref, {}).get(source.column, "")
        best = 0
        for words in (_header_words(label), _header_words(source.column)):
            if words and words == wanted:
                return 2
            if words and (words <= wanted or wanted <= words):
                best = 1
        return best


def _by_entities(sources: Sequence[_Source], entities: Sequence[_Entity]) -> list[_Source]:
    """The places in the rows of each entity in turn, each step kept only when some place is left."""
    candidates = list(sources)
    for entity in entities:
        kept = [source for source in candidates if (source.fact_ref, source.row_index) in entity.rows]
        candidates = kept or candidates
    return candidates


def _by_fact(candidates: list[_Source], bound: Sequence[_Source]) -> list[_Source]:
    """The places in the fact of the nearest bound figure that has one among them (bound: nearest first)."""
    for other in bound:
        kept = [source for source in candidates if source.fact_ref == other.fact_ref]
        if kept:
            return kept
    return candidates


class _Index:
    """The entities the facts hold: issue keys, people's names and row labels, each with the rows it stands for."""

    def __init__(self, facts: Sequence[Facts], people: Sequence[tuple[str, str]]) -> None:
        self.keys: dict[str, set[tuple[str, int]]] = {}
        self.phrases: dict[_Phrase, set[tuple[str, int]]] = {}
        holders: dict[str, set[tuple[str, int]]] = {}  # person id -> the rows that are theirs
        names: dict[_Phrase, set[str]] = {}  # a person's name -> their person ids
        counts: Counter[tuple[str, str, _Phrase]] = Counter()
        for fact in facts:
            kinds = {column.key: column.kind for column in fact.columns}
            for index, row in enumerate(fact.rows):
                place = (fact.ref, index)
                for column, value in row.items():
                    if not isinstance(value, str):
                        continue
                    kind = kinds.get(column, "text")
                    if kind == "issue_key" or column in ("issue_key", "epic_key"):
                        self.keys.setdefault(value, set()).add(place)
                    elif column == "sample_keys":
                        for key in KEY.findall(value):
                            self.keys.setdefault(key, set()).add(place)
                    elif kind == "text" and column not in TICKET_TEXT_COLUMNS and (phrase := _phrase(value)):
                        self.phrases.setdefault(phrase, set()).add(place)
                        counts[(fact.ref, column, phrase)] += 1
                for id_column, name_column in _PERSON_COLUMNS:
                    person = row.get(id_column)
                    if isinstance(person, str) and person:
                        holders.setdefault(person, set()).add(place)
                        name = row.get(name_column)
                        if isinstance(name, str) and (phrase := _phrase(name)):
                            names.setdefault(phrase, set()).add(person)
        for person, name in people:
            if phrase := _phrase(name):
                names.setdefault(phrase, set()).add(person)
        self.tiers = {phrase: _LABEL for phrase in self.phrases}
        for (_ref, _column, phrase), count in counts.items():
            if count == 1:
                self.tiers[phrase] = _ROW_LABEL
        for phrase, persons in names.items():
            rows = self.phrases.get(phrase, set()).union(*(holders.get(person, set()) for person in persons))
            if rows:
                self.phrases[phrase] = rows
                self.tiers[phrase] = _IDENTITY


def _phrase(value: str) -> _Phrase | None:
    """How a text cell is looked for in an answer; None for one that labels nothing (an id, a date, a sentence)."""
    text = " ".join(value.split())
    if (not 2 <= len(text) <= _LABEL_MAX or not any(char.isalpha() for char in text)
            or PERSON_ID.fullmatch(text) or _ISO_DAY.match(text)):
        return None
    if len(_WORD.findall(text)) > 1:
        return _Phrase(" ".join(fold(text).split()), folded=True)
    return _Phrase(text, folded=False)


def _entities(text: str, index: _Index) -> list[_Entity]:
    """The entities the text names, in text order; where two would overlap, the longer ("Ada Brooks", never also
    "Ada")."""
    spans: list[tuple[int, int, int, set[tuple[str, int]]]] = []
    for match in KEY.finditer(_RULE_ID.sub(_blank, text)):
        if rows := index.keys.get(match.group()):
            spans.append((match.start(), match.end(), _IDENTITY, rows))
    folded, places = _folded(text)
    for phrase, rows in index.phrases.items():
        words = phrase.text.split()
        within = folded if phrase.folded else text
        if not all(word in within for word in words):
            continue
        pattern = re.compile(r"(?<!\w)" + r"\s+".join(re.escape(word) for word in words) + r"(?!\w)")
        for match in pattern.finditer(within):
            start, end = (places[match.start()], places[match.end() - 1] + 1) if phrase.folded else match.span()
            spans.append((start, end, index.tiers[phrase], rows))
    taken = bytearray(len(text))
    entities: list[_Entity] = []
    for start, end, tier, rows in sorted(spans, key=lambda span: (span[0] - span[1], span[0], span[2])):
        if any(taken[start:end]):
            continue
        taken[start:end] = b"\x01" * (end - start)
        entities.append(_Entity(start, end, tier, frozenset(rows)))
    return sorted(entities, key=lambda entity: entity.start)


def _folded(text: str) -> tuple[str, list[int]]:
    """The text folded a character at a time (people.fold), and where in the text each folded character stood."""
    pieces: list[str] = []
    places: list[int] = []
    for index, char in enumerate(text):
        piece = fold(char)
        pieces.append(piece)
        places.extend([index] * len(piece))
    return "".join(pieces), places


def _table_rows(text: str) -> list[_TableRow]:
    """The body rows of every Markdown table in the text: a header row, a delimiter row, then the lines with a pipe."""
    lines: list[tuple[int, str]] = []
    at = 0
    for line in text.split("\n"):
        lines.append((at, line))
        at += len(line) + 1
    rows: list[_TableRow] = []
    tables = 0
    index = 0
    while index + 1 < len(lines):
        head = lines[index][1]
        delimiter = lines[index + 1][1]
        if "|" not in head or "|" not in delimiter or not _TABLE_DELIMITER.match(delimiter):
            index += 1
            continue
        header = tuple(head[start:end].strip() for start, end in _cells(head))
        index += 2
        while index < len(lines) and "|" in lines[index][1] and lines[index][1].strip():
            start, line = lines[index]
            cells = tuple((start + left, start + right) for left, right in _cells(line))
            rows.append(_TableRow(tables, start, start + len(line), cells, header))
            index += 1
        tables += 1
    return rows


def _cells(line: str) -> list[tuple[int, int]]:
    """Each cell's span in a table line; a pipe that opens or closes the line bounds no cell of its own."""
    pipes = [match.start() for match in _CELL_PIPE.finditer(line)]
    bounds = [-1, *pipes, len(line)]
    cells = [(bounds[at] + 1, bounds[at + 1]) for at in range(len(bounds) - 1)]
    edge = line.strip()
    if cells and edge.startswith("|"):
        cells = cells[1:]
    if cells and edge.endswith("|") and not edge.endswith("\\|"):
        cells = cells[:-1]
    return cells


def _header_words(text: str) -> frozenset[str]:
    """A header's or a column's words, folded, a plural's s taken off: "Tickets reopened" ~ "tickets_reopened"."""
    return frozenset(word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word
                     for word in _HEADER_WORD.findall(fold(text)))


# --- what the facts allow ------------------------------------------------------------------

# A value -> every place that holds it, in the facts' order (a dict kept as an ordered set): the first place is where
# a value with no context is bound.
_Places = dict[str, dict[_Source, None]]


def _put(places: _Places, value: str, source: _Source) -> None:
    places.setdefault(value, {}).setdefault(source, None)


def _rule_words(rules: RulesConfig | None) -> tuple[re.Pattern[str] | None, re.Pattern[str] | None]:
    """The board's rules' words as two patterns: their titles (masked in the answer: a title quoted whole is a name),
    and their titles and descriptions (masked in the facts' own text: never a figure there). Each matched whole, in
    any case, any run of spaces between its words; only words that hold a figure (a digit, or a number in words), as
    no other changes what is read. None when there are none."""
    if rules is None:
        return None, None
    titles: set[str] = set()
    texts: set[str] = set()
    for rule in rules.known:
        for value, into in ((rule.title, (titles, texts)), (rule.description, (texts,))):
            phrase = " ".join(value.split()) if isinstance(value, str) else ""
            if phrase and (_DIGIT_ANYWHERE.search(phrase) or SPELLED.search(phrase)):
                for found in into:
                    found.add(phrase)
    return _phrases(titles), _phrases(texts)


def _phrases(phrases: set[str]) -> re.Pattern[str] | None:
    """One pattern for every phrase, the longest first (so a title that holds another is masked whole)."""
    if not phrases:
        return None
    either = "|".join(r"\s+".join(re.escape(word) for word in phrase.split())
                      for phrase in sorted(phrases, key=lambda phrase: (-len(phrase), phrase)))
    return re.compile(rf"(?<!\w)(?:{either})(?!\w)", re.IGNORECASE)


_DIGIT_ANYWHERE = re.compile(r"\d")


def _allowed(facts: list[Facts], plan: Plan | None,
             rule_words: re.Pattern[str] | None = None) -> tuple[_Places, _Places, _Places]:
    """Allowed keys, dates (ISO day) and number forms, each mapped to every place it came from, in order. rule_words:
    the board's rules' titles and descriptions, masked in any text the facts hold before it is read."""
    keys: _Places = {}
    dates: _Places = {}
    numbers: _Places = {}

    def trusted(text: str, source: _Source) -> None:
        _add_trusted_text(rule_words.sub(_blank, text) if rule_words is not None else text, source, dates, numbers)

    for fact in facts:
        kinds = {column.key: column.kind for column in fact.columns}
        for index, row in enumerate(fact.rows):
            for column, value in row.items():
                source = _Source(fact.ref, index, column)
                kind = kinds.get(column, "text")
                if kind == "issue_key" or column in ("issue_key", "epic_key"):
                    if isinstance(value, str):
                        _put(keys, value, source)
                elif column == "sample_keys" and isinstance(value, str):
                    for key in KEY.findall(value):
                        _put(keys, key, source)
                elif kind in NUMBER_KINDS:
                    for form in display_forms(value, kind):
                        _put(numbers, form, source)
                elif kind == "date":
                    _add_date(value, source, dates, numbers)
                elif isinstance(value, str) and column not in UNTRUSTED_TEXT_COLUMNS:
                    trusted(value, source)
        if fact.as_of:
            _add_date(fact.as_of, _Source(fact.ref, -1, "as_of"), dates, numbers)
        if fact.note:
            trusted(fact.note, _Source(fact.ref, -1, "note"))
    if plan is not None:
        for call, fact in zip(plan.calls, facts, strict=False):
            source = _Source(fact.ref, -1, "params")
            for value in call.params.values():
                for form in _param_forms(value):
                    _put(numbers, form, source)
    for fact in facts:
        source = _Source(fact.ref, -1, "row_count")
        for form in display_forms(len(fact.rows), "count"):
            _put(numbers, form, source)
    return keys, dates, numbers


def _param_forms(value: object) -> set[str]:
    """A plan parameter's number forms: the value itself, or the digits inside a string such as "90d"."""
    forms = display_forms(value)
    if forms or not isinstance(value, str):
        return forms
    for token in NUM.findall(KEY.sub(_blank, value)):
        forms |= display_forms(token)
    return forms


def _add_date(value: object, source: _Source, dates: _Places, numbers: _Places) -> None:
    # A cell holds "2026-10-03" or the storage form "2026-10-03T16:00:00.000Z"; the day is what matters.
    if isinstance(value, str) and _ISO_DAY.match(value):
        day = value[:10]
        _put(dates, day, source)
        _put(numbers, day[:4], source)  # "in 2026" is the date's year, not an invented number


def _add_trusted_text(text: str, source: _Source, dates: _Places, numbers: _Places) -> None:
    """Numbers and dates in text that code wrote (a message, a label, a note), quotable as they are."""
    for match in _ISO_DATETIME.finditer(text):
        _add_date(match.group(), source, dates, numbers)
    work = _ISO_DATETIME.sub(_blank, text)
    for match in _SPELLED_DAY.finditer(work):
        day = _spelled_day(match.group(1), match.group(2), match.group(3))
        if day:
            _add_date(day, source, dates, numbers)
    work = _SPELLED_DAY.sub(_blank, _RULE_ID.sub(_blank, work))
    work = KEY.sub(_blank, work)
    for match in NUM.finditer(work):
        token = match.group().replace(",", "")
        if token.endswith("%"):
            percent = _number(token[:-1])
            if percent is not None:
                for form in (token, f"{percent:.0f}%", f"{percent:.1f}%", _trim(f"{percent:.1f}") + "%"):
                    _put(numbers, form, source)
        else:
            for form in display_forms(token):
                _put(numbers, form, source)
    for match in _spelled_numbers(work):
        for form in display_forms(spelled_value(match.group())):
            _put(numbers, form, source)


def _spelled_numbers(text: str) -> list[re.Match[str]]:
    """The numbers the text writes in words, in text order; "one" on its own left out (see the module docstring)."""
    return [match for match in SPELLED.finditer(text) if match.group().lower() != "one"]


def spelled_value(text: str) -> int:
    """A number written in words (one SPELLED match) as its value: "Seventeen" 17, "twenty-one" 21, "a hundred and
    five" 105, "two thousand" 2000."""
    total = current = 0
    for word in re.findall(r"[a-z]+", text.lower()):
        if word == "a":
            current = 1
        elif word == "hundred":
            current = (current or 1) * 100
        elif word == "thousand":
            total += (current or 1) * 1000
            current = 0
        elif word != "and":
            current += _NUMBER_WORDS[word]
    return total + current


# --- small helpers --------------------------------------------------------------------------


def _record(
    start: int,
    token: str,
    sources: Mapping[_Source, None] | Sequence[_Source] | None,
    kind: str,
    found: list[_Found],
    problems: list[str],
    problem: str,
) -> None:
    if not sources:
        problems.append(f"{problem} {token}")
    else:
        found.append(_Found(start, token, tuple(sources), kind))


def _blank(match: re.Match[str]) -> str:
    # Masking keeps every other character where it was, so positions stay valid.
    return " " * len(match.group())


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip().replace(",", ""))
        except ValueError:
            return None
    else:
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _trim(form: str) -> str:
    """'54.00' -> '54', '12.50' -> '12.5'; integers are left alone."""
    return form.rstrip("0").rstrip(".") if "." in form else form


def _month_number(name: str) -> int | None:
    prefix = name[:3].lower()
    return _MONTHS.index(prefix) + 1 if prefix in _MONTHS else None


def _spelled_day(day: str, month: str, year: str) -> str | None:
    number = _month_number(month)
    if number is None or not 1 <= int(day) <= 31:
        return None
    return f"{int(year):04d}-{number:02d}-{int(day):02d}"


def _spelled_month(month: str, year: str) -> str | None:
    number = _month_number(month)
    return f"{int(year):04d}-{number:02d}" if number else None


__all__ = ["Binding", "Check", "DATE", "KEY", "NUM", "NUMBER_KINDS", "SPELLED", "TICKET_TEXT_COLUMNS",
           "UNTRUSTED_TEXT_COLUMNS", "display_forms", "spelled_value", "validate"]

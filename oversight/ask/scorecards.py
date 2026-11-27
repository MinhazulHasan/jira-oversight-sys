"""Ask's person scorecards and comparison: their facts, the answer built in code and the labelling check."""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from oversight.api.people import UNASSIGNED_LABEL, Person
from oversight.api.scorecard import (
    BY_KEY,
    DEFAULT_DAYS,
    MEASURES,
    MIN_TICKETS,
    NOW_COUNTS,
    OVERALL,
    RANKED_COUNTS,
    Board,
    Measure,
    Period,
    Value,
    board_values,
    effective_order,
    order,
    person_values,
    standings,
    too_few,
    values,
)
from oversight.ask.catalog import Column, Facts, _day, _row

SCORECARD_TOOL = "oversight_person_scorecard"
COMPARE_TOOL = "oversight_people_compare"
# Rates of now: no period is said beside them.
NOW_MEASURES = frozenset({"stuck_now", "open_findings_per_10"})
SCORECARD_TOOLS = frozenset((SCORECARD_TOOL, COMPARE_TOOL))
WHOLE_BOARD = "Whole board"
# Whose a ticket's figures are, as the notes say it.
CREDIT_READING = (
    "every ticket, open or finished, by whom it counts for (the board's credit setting: a people field such as "
    "Programmer, the assignee, or Automatic: Programmer where it is filled, otherwise the assignee; a ticket without "
    "one counts for no one)"
)
# The tickets that count for no one, said after a ranking by a count; matches the dashboard's sample.
UNASSIGNED_WORDS: dict[str, tuple[str, str]] = {
    "open_tickets": ("open ticket", "open tickets"),
    "in_progress": ("ticket in progress", "tickets in progress"),
    "done_in_period": ("ticket done", "tickets done"),
}
# The marker words the dashboard shows beside a figure.
STANDING = {"closer": "Closer to the process", "further": "Further from the process", "level": "Level with the board"}
TOO_FEW = "Too few tickets"
# Character labels, each form listed whole so a surname such as Lazić is never matched.
CHARACTER_LABELS = re.compile(
    r"\b(?:lazy-?bones|lazy|lazi(?:er|est|ly|ness)|useless(?:ly|ness)?|incompeten(?:t|ts|tly|ce|cy)"
    r"|careless(?:ly|ness)?|dishonest(?:ly|y)?|insincer(?:e|ely|ity)|stupid(?:er|est|ly|ity)?"
    r"|idiot(?:s|ic|ically)?|idiocy|hopeless(?:ly|ness)?|worthless(?:ness)?|pathetic(?:ally)?|slackers?|liars?"
    r"|slopp(?:y|ier|iest|ily|iness))\b",
    re.IGNORECASE,
)
# The measures' labels as an answer writes them: words, never figures ("per 10 open tickets" is no figure of anyone's).
MEASURE_LABELS = re.compile("|".join(re.escape(measure.label) for measure in MEASURES), re.IGNORECASE)
# A query of the analyst's that reads the scorecard view.
_READS_THE_VIEW = re.compile(r"\bscorecard\b", re.IGNORECASE)


class ComparisonOff(ValueError):
    """Comparing people is turned off (PEOPLE_COMPARISON=off): the sentence is the refusal, word for word."""


SCORECARD_COLUMNS = [
    Column("person_id", "Person id", "text"),
    Column("name", "Person", "text"),
    Column("measure", "Measure id", "text"),
    Column("label", "Measure", "text"),
    Column("kind", "Kind", "text"),
    Column("value", "Value", "number"),
    Column("numerator", "Numerator", "count"),
    Column("denominator", "Denominator", "count"),
    Column("shown", "Shown", "text"),
    Column("enough", "Enough tickets", "text"),
    Column("vs_board", "Next to the board", "text"),
    Column("board_value", "Board value", "number"),
    Column("board_numerator", "Board numerator", "count"),
    Column("board_denominator", "Board denominator", "count"),
    Column("board_shown", "Board shown", "text"),
    Column("measurable", "Measurable", "text"),
    Column("reason", "Not measurable because", "text"),
    Column("rule_ids", "Rules", "text"),
]
COMPARE_COLUMNS = [
    Column("person_id", "Person id", "text"),
    Column("name", "Person", "text"),
    Column("measure", "Measure id", "text"),
    Column("label", "Measure", "text"),
    Column("kind", "Kind", "text"),
    Column("value", "Value", "number"),
    Column("numerator", "Numerator", "count"),
    Column("denominator", "Denominator", "count"),
    Column("shown", "Shown", "text"),
    Column("enough", "Enough tickets", "text"),
    Column("vs_board", "Next to the board", "text"),
    Column("standing", "Standing", "text"),
    Column("measurable", "Measurable", "text"),
    Column("reason", "Not measurable because", "text"),
]
OVERALL_COLUMNS = [
    Column("person_id", "Person id", "text"),
    Column("name", "Person", "text"),
    Column("closer", "Closer to the process", "count"),
    Column("further", "Further from the process", "count"),
    Column("level", "Level with the board", "count"),
    Column("compared", "Measures compared", "count"),
    Column("too_few", "Too few tickets", "count"),
    Column("open_findings_per_10", "Open findings per 10 open tickets", "number"),
    Column("open_findings", "Open findings", "count"),
    Column("open_tickets", "Open tickets", "count"),
    Column("enough", "Enough tickets", "text"),
    Column("standing", "Standing", "text"),
]
VIEW_COLUMNS: tuple[str, ...] = (
    "person_id", "name", "measure", "label", "kind", "direction", "measurable", "value", "numerator", "denominator",
    "shown", "enough", "vs_board", "board_value", "board_numerator", "board_denominator", "board_shown", "days",
    "period_from", "period_to",
)


def shown(measure: Measure, value: Value) -> str | None:
    """A figure as the dashboard shows it: "72% · 18 of 25", "12" or "2.3"; None when there is none."""
    figure = value["value"]
    if figure is None:
        return None
    if measure.kind == "count":
        return str(figure)
    if measure.kind == "ratio":
        return f"{figure:.1f}"
    return f"{figure}% · {value['numerator']} of {value['denominator']}"


def _period_words(period: Period) -> str:
    return f"the last {period.days} days ({_day(period.since)} to {_day(period.until)})"


# Each fact set's reading in plain words; the dashboard's sample writes the same notes.
_READING = (
    f"{CREDIT_READING}, Epics left out; a rate's value is a percentage of its denominator, a count's "
    "value is the count, and open findings per 10 open tickets is the open breaches and warnings on the person's open "
    f"tickets for every 10 of those tickets; enough tickets means the figure rests on at least {MIN_TICKETS} tickets; "
    "next to the board says closer to the process than the board, further from it, or level with it (within 5 "
    "points, 0.5 for the ratio), only when both sides have enough"
)
_OVERALL_READING = (
    f"{CREDIT_READING}, Epics left out; the closer, further and level counts are the measures on "
    "which the person's figure stands closer to the process than the board's, further from it, or level with it "
    "(within 5 points, 0.5 for the ratio); measures compared is their sum; too few tickets counts the measures where "
    f"either side rests on fewer than {MIN_TICKETS} tickets; open findings per 10 open tickets is the open breaches "
    "and warnings on the person's open tickets for every 10 of those tickets, compared only when it rests on enough "
    "tickets"
)


# --- the tools' facts ------------------------------------------------------------------------


def scorecard_rows(board: Board, person_ids: Sequence[str], days: int) -> tuple[list[dict[str, Any]], str]:
    """oversight_person_scorecard's rows and note: each person asked about, each measure next to the board's."""
    period = board.period(days)
    against = board_values(board, period)
    wanted = set(person_ids)
    found = [who for who in board.everyone if who["id"] in wanted]
    rows = []
    for who in found:
        mine = person_values(board, who["id"], period, against)
        for measure in MEASURES:
            reason = board.reason(measure.key, period)
            rows.append(_row(
                person_id=who["id"], name=who["name"], measure=measure.key, label=measure.label, kind=measure.kind,
                value=mine[measure.key]["value"], numerator=mine[measure.key]["numerator"],
                denominator=mine[measure.key]["denominator"], shown=shown(measure, mine[measure.key]),
                enough=mine[measure.key]["enough"], vs_board=mine[measure.key]["vs_board"],
                board_value=against[measure.key]["value"], board_numerator=against[measure.key]["numerator"],
                board_denominator=against[measure.key]["denominator"], board_shown=shown(measure, against[measure.key]),
                measurable=reason is None, reason=reason, rule_ids=", ".join(board.rule_ids(measure)) or None,
            ))
    note = f"each person's figures over {_period_words(period)}, next to the whole board's, {_READING}"
    missing = len(wanted) - len(found)
    if missing:
        note += f"; {missing} of the people asked about {'is' if missing == 1 else 'are'} not on this board"
    return rows, note


def compare_rows(board: Board, measure: str, days: int, *, first: str = "closer", only: frozenset[str] | None = None
                 ) -> tuple[list[Column], list[dict[str, Any]], str]:
    """oversight_people_compare's columns, rows and note, in the order asked for."""
    period = board.period(days)
    against = board_values(board, period)
    first = effective_order(measure, first)
    ordered = order(standings(board, period, against), measure, first=first)
    if only is not None:
        ordered = [one for one in ordered if one.person["id"] in only]
    everyone = "the people asked about" if only is not None else "everyone with tickets"
    if measure == OVERALL:
        rows = []
        for one in ordered:
            ratio = one.values["open_findings_per_10"]
            rows.append(_row(person_id=one.person["id"], name=one.person["name"], closer=one.closer,
                             further=one.further, level=one.level, compared=one.compared,
                             too_few=too_few(board, one, period), open_findings_per_10=ratio["value"],
                             open_findings=ratio["numerator"], open_tickets=ratio["denominator"],
                             enough=ratio["enough"], standing=None if one.compared else TOO_FEW))
        fewest = first == "fewest"
        note = (f"{everyone}, by the number of measures on which they are closer to the process "
                f"than the whole board over {_period_words(period)}, {'fewest' if fewest else 'most'} first, ties by "
                f"open findings per 10 open tickets ({'more' if fewest else 'fewer'} first; a figure on too few "
                f"tickets is never compared, so it counts as the most), people compared on no measure last; "
                f"{_OVERALL_READING}")
        return OVERALL_COLUMNS, rows, note
    chosen = BY_KEY[measure]
    reason = board.reason(chosen.key, period)

    def row(who: Person | None, value: Value, standing: str | None, *, name: str = WHOLE_BOARD) -> dict[str, Any]:
        return _row(person_id=who["id"] if who else None, name=who["name"] if who else name,
                    measure=chosen.key, label=chosen.label, kind=chosen.kind, value=value["value"],
                    numerator=value["numerator"], denominator=value["denominator"], shown=shown(chosen, value),
                    enough=value["enough"], vs_board=value["vs_board"], standing=standing,
                    measurable=reason is None, reason=reason)

    counted = chosen.key in RANKED_COUNTS
    rows = [] if reason is not None else [
        row(one.person, one.values[chosen.key],
            None if counted else STANDING.get(one.values[chosen.key]["vs_board"] or "", TOO_FEW))
        for one in ordered
    ]
    # A ranking by a count ranks people only; the tickets that count for no one follow as one unranked row.
    nobody = [ticket for ticket in board.work if not ticket.builders] if only is None else []
    if counted and reason is None and nobody:
        rows.append(row(None, values(board, nobody, period)[chosen.key], None, name=UNASSIGNED_LABEL))
    rows.append(row(None, against[chosen.key], None))
    # The measure named mid-sentence, as a reader writes it: "by reopened after Done", never "by Reopened after Done".
    named = chosen.label[:1].lower() + chosen.label[1:]
    if first == "closer":
        direction = "highest" if chosen.direction == "higher_is_closer" else "lowest"
        asked = f"closest to the process first ({direction} first)"
    else:
        asked = "highest first" if first == "most" else "lowest first"
    note = (f"{everyone}, by {named} over {_period_words(period)}, {asked}, people with too few tickets last, then the "
            f"whole board; {_READING}")
    if counted:
        # A count of now has no period; its label may say so already ("In progress now").
        if chosen.key in NOW_COUNTS:
            when = "" if chosen.label.endswith(" now") else " now"
        else:
            when = f" over {_period_words(period)}"
        then = ("the tickets that count for no one (Unassigned, never ranked), then the whole board" if nobody
                else "the whole board")
        note = (f"{everyone}, by {named}{when}, {'fewest' if first == 'fewest' else 'most'} first, ties by "
                f"name, then {then} (a count is never set beside the board as closer or further); {_READING}")
    if reason is not None:
        note = f"{chosen.label} is not measurable on this board: {reason}"
    return COMPARE_COLUMNS, rows, note


# --- the answer, built in code ----------------------------------------------------------------


def join(items: Sequence[str]) -> str:
    """"A", "A and B", "A, B and C": no comma before "and"."""
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _days(fact: Mapping[str, Any]) -> int:
    days = (fact.get("params") or {}).get("days", DEFAULT_DAYS)
    return days if isinstance(days, int) and not isinstance(days, bool) else DEFAULT_DAYS


# A piece of an answer built in code: words, or a figure with where it was read from (fact ref, row index, column).
Piece = str | tuple[str, str, int, str]


def _param(fact: Mapping[str, Any], text: str) -> Piece:
    return (text, str(fact.get("ref") or ""), -1, "params")


def _cell(fact: Mapping[str, Any], index: int, column: str, text: str) -> Piece:
    return (text, str(fact.get("ref") or ""), index, column)


def _figure(fact: Mapping[str, Any], index: int, row: Mapping[str, Any]) -> list[Piece]:
    """A figure in a sentence, cited to its own row."""
    value = row.get("value")
    if row.get("kind") == "rate":
        return [_cell(fact, index, "shown", str(value)), "%"]
    if row.get("kind") == "ratio" and isinstance(value, (int, float)):
        return [_cell(fact, index, "value", f"{value:.1f}")]
    return [_cell(fact, index, "value", str(value))]


def _listed(items: Sequence[list[Piece]]) -> list[Piece]:
    """Pieces joined as join() joins words: "A", "A and B", "A, B and C"."""
    pieces: list[Piece] = []
    for number, item in enumerate(items):
        if number:
            pieces.append(" and " if number == len(items) - 1 else ", ")
        pieces.extend(item)
    return pieces


def _scorecard_answer(fact: Mapping[str, Any]) -> list[list[Piece]]:
    rows = [row for row in fact.get("rows") or [] if isinstance(row, Mapping)]
    people = list(dict.fromkeys((row.get("person_id"), row.get("name")) for row in rows if row.get("person_id")))
    if not people:
        return []
    days = str(_days(fact))
    first = [row for row in rows if row.get("person_id") == people[0][0]]
    unmeasured = [str(row.get("label")) for row in first if row.get("measurable") is False]
    paragraphs: list[list[Piece]] = []
    for person_id, name in people:
        theirs = [row for row in rows if row.get("person_id") == person_id]

        def labels(where: str, theirs: list[Mapping[str, Any]] = theirs) -> list[str]:
            return [str(row.get("label")) for row in theirs if row.get("vs_board") == where]

        clauses = []
        for where, words in (("closer", "closer to the process on"), ("further", "further from it on"),
                             ("level", "level on")):
            if labels(where):
                clauses.append(f"{words} {join(labels(where))}")
        pieces: list[Piece] = [f"{name}, last ", _param(fact, days), " days, next to the board"]
        pieces.append(f": {'; '.join(clauses)}." if clauses else ".")
        few = [str(row.get("label")) for row in theirs
               if row.get("measurable") is True and row.get("kind") != "count" and row.get("vs_board") is None]
        if few:
            pieces.append(f" Too few tickets to compare: {join(few)}.")
        if len(people) == 1 and unmeasured:
            pieces.append(f" Not measurable on this board: {join(unmeasured)}.")
        paragraphs.append(pieces)
    if len(people) > 1 and unmeasured:
        paragraphs.append([f"Not measurable on this board: {join(unmeasured)}."])
    return paragraphs


def order_words(measure: str, first: str | None) -> str:
    """The order a comparison says it used, in plain words; never "worst" or "best"."""
    asked = first if first in ("most", "fewest") else "closer"
    if measure == OVERALL or measure in RANKED_COUNTS:
        return "fewest first" if asked == "fewest" else "most first"
    return {"closer": "closest to the process first", "most": "highest first", "fewest": "lowest first"}[asked]


def _compare_answer(fact: Mapping[str, Any]) -> list[list[Piece]]:
    rows = [row for row in fact.get("rows") or [] if isinstance(row, Mapping)]
    measure = (fact.get("params") or {}).get("measure", OVERALL)
    first = order_words(str(measure), (fact.get("params") or {}).get("order"))
    days = str(_days(fact))
    people = [(index, row) for index, row in enumerate(rows) if row.get("person_id")]
    pieces: list[Piece] = []
    compared: list[tuple[int, Mapping[str, Any]]]
    if measure == OVERALL:
        compared = [(index, row) for index, row in people if row.get("compared")]
        if compared:
            pieces += ["Measures closer to the process than the board, last ", _param(fact, days), f" days, {first}: "]
            pieces += _listed([[f"{row.get('name')} ", _cell(fact, index, "closer", str(row.get("closer"))), " of ",
                                _cell(fact, index, "compared", str(row.get("compared")))] for index, row in compared])
            pieces.append(".")
    else:
        # The board's row is the last with no person; in a count's ranking, Unassigned comes just before it.
        nobody_rows = [(index, row) for index, row in enumerate(rows) if not row.get("person_id")]
        board = nobody_rows[-1] if nobody_rows else None
        unassigned = next(((index, row) for index, row in nobody_rows[:-1] if row.get("name") == UNASSIGNED_LABEL),
                          None)
        if board is not None and board[1].get("measurable") is False:
            return [[f"Not measurable on this board: {board[1].get('label')}. {board[1].get('reason')}"]]
        label = str((board[1] if board else (people[0][1] if people else {})).get("label") or "")
        counted = measure in RANKED_COUNTS
        compared = [(index, row) for index, row in people
                    if row.get("value") is not None and (counted or row.get("enough") is True)]
        if compared:
            if measure in NOW_COUNTS or measure in NOW_MEASURES:
                pieces += [f"{label}, {first}: "]  # a count or a figure of now: no period
            else:
                pieces += [f"{label}, last ", _param(fact, days), f" days, {first}: "]
            pieces += _listed([[f"{row.get('name')} ", *_figure(fact, index, row)] for index, row in compared])
            pieces.append(".")
        if counted and unassigned is not None and isinstance(unassigned[1].get("value"), int) and unassigned[1]["value"]:
            # After the ranking, never in it: "Unassigned: 148 open tickets."
            amount = unassigned[1]["value"]
            one, several = UNASSIGNED_WORDS.get(str(measure), ("ticket", "tickets"))
            pieces += [" " if pieces else "", f"{UNASSIGNED_LABEL}: ", _figure(fact, unassigned[0], unassigned[1])[0],
                       f" {one if amount == 1 else several}."]
        if board is not None and board[1].get("value") is not None:
            pieces += [" " if pieces else "", "The whole board: ", *_figure(fact, board[0], board[1]), "."]
    few = [str(row.get("name")) for index, row in people if (index, row) not in compared]
    if few:
        pieces.append(f"{' ' if pieces else ''}Too few tickets to compare: {join(few)}.")
    return [pieces] if pieces else []


def built_answer(facts: Iterable[Mapping[str, Any]]) -> tuple[str, dict[int, tuple[str, int, str]]] | None:
    """The code-built answer for the scorecard and comparison facts, with validate hints; None if there are none."""
    paragraphs: list[list[Piece]] = []
    for fact in facts:
        if fact.get("tool") == SCORECARD_TOOL:
            paragraphs.extend(_scorecard_answer(fact))
        elif fact.get("tool") == COMPARE_TOOL:
            paragraphs.extend(_compare_answer(fact))
    if not paragraphs:
        return None
    text = ""
    hints: dict[int, tuple[str, int, str]] = {}
    for number, paragraph in enumerate(paragraphs):
        if number:
            text += "\n\n"
        for piece in paragraph:
            if isinstance(piece, tuple):
                hints[len(text)] = piece[1:]
                text += piece[0]
            else:
                text += piece
    return text, hints


def code_built(facts: Iterable[Mapping[str, Any]]) -> str | None:
    """The answer built in code (built_answer), its text alone."""
    built = built_answer(facts)
    return built[0] if built is not None else None


def scorecard_days(facts: Iterable[Facts]) -> list[int] | None:
    """The periods of the scorecard figures an answer rests on, shortest first, for the caveat; None if none."""
    found = list(facts)
    days = {_days(fact.to_dict()) for fact in found
            if fact.tool in SCORECARD_TOOLS
            and not (fact.tool == COMPARE_TOOL and fact.params.get("measure") in NOW_COUNTS)}
    if any(_READS_THE_VIEW.search(str(fact.params.get("sql") or "")) for fact in found):
        days.add(DEFAULT_DAYS)
    return sorted(days) or None


def character_labels(text: str, names: Iterable[str] = ()) -> list[str]:
    """The words in an answer that label someone's character, each once; people's names are taken out first."""
    words = sorted({word for name in names for word in name.split() if len(word) > 1}, key=len, reverse=True)
    if words:
        pattern = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(word) for word in words) + r")(?!\w)")
        text = pattern.sub(lambda match: " " * len(match.group()), text)
    return list(dict.fromkeys(match.group().lower() for match in CHARACTER_LABELS.finditer(text)))


def without_labels(text: str) -> str:
    """The text with every measure's label blanked, so a figure in a label is never read as anyone's figure."""
    return MEASURE_LABELS.sub(lambda match: " " * len(match.group()), text)


def labels_problem(words: Sequence[str]) -> str:
    """The wording check's problem, as the rewrite is told it."""
    return (f"the answer labels someone's character ({', '.join(words)}): say only what the figures show next to the "
            "board, and leave the judgement to the reader")


# --- the analyst's scorecard view -------------------------------------------------------------


def view_rows(board: Board, *, comparison: bool, mentioned: Sequence[str]) -> list[tuple[Any, ...]]:
    """The scorecard view's rows for the last 90 days: the whole board, then the people it may show."""
    period = board.period(DEFAULT_DAYS)
    against = board_values(board, period)
    people = list(board.holders) if comparison else [who for who in board.holders if [who["id"]] == list(mentioned)]
    since, until = _day(period.since), _day(period.until)
    rows: list[tuple[Any, ...]] = []
    for who in [None, *people]:
        mine = against if who is None else person_values(board, who["id"], period, against)
        for measure in MEASURES:
            value, theirs = mine[measure.key], against[measure.key]
            rows.append((
                who["id"] if who else None, who["name"] if who else None, measure.key, measure.label, measure.kind,
                measure.direction, int(board.reason(measure.key, period) is None), value["value"], value["numerator"],
                value["denominator"], shown(measure, value), int(value["enough"]), value["vs_board"], theirs["value"],
                theirs["numerator"], theirs["denominator"], shown(measure, theirs), period.days, since, until,
            ))
    return rows

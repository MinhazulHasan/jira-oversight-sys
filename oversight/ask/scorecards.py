"""Ask's person scorecards and comparison (JO-11): the two tools' facts, the answer built in code, and the check
that an answer labels no one.

Every figure is oversight/api/scorecard.py's, worked out as GET /people/scorecard works it out. Here they become fact
sets, one row each, with names beside person ids (never an id alone) and each figure also as the dashboard shows it
(`shown`: "72% · 18 of 25", a count, or the ratio to one decimal), so an answer may quote "72%" and the number check
(validate.py) finds it:

  oversight_person_scorecard -> one row per person asked about and measure, in the directory's order of the people
      and the contract's order of the measures: {person_id, name, measure, label, kind, value, numerator,
      denominator, shown, enough, vs_board, board_value, board_numerator, board_denominator, board_shown,
      measurable, reason, rule_ids}
  oversight_people_compare(measure, order) -> everyone who holds a ticket in the order asked (JO-14; the params
      always carry the order used): "closer" closer to the process first, "most" the figure highest first,
      "fewest" lowest first; people with too few tickets last whatever the order, then the whole board's row
      (person_id null, name "Whole board"): {person_id, name, measure, label, kind, value, numerator, denominator,
      shown, enough, vs_board, standing, measurable, reason}; standing is "Closer to the process", "Further from
      the process", "Level with the board" or "Too few tickets"
  oversight_people_compare(overall, order) -> everyone, by the number of measures on which they are closer to the
      process than the board, most first ("closer" and "most"; ties by open findings per 10 open tickets, a figure
      on too few tickets counting as the most) or fewest first ("fewest", the exact reverse), people compared on
      none last: {person_id, name, closer, further, level, compared, too_few, open_findings_per_10, open_findings,
      open_tickets, enough, standing} (open_findings and open_tickets: the ratio's numerator and denominator;
      enough: whether it rests on enough tickets to be compared; standing "Too few tickets" for those compared on
      none, else null); no board row (the board is what each is compared with)
  oversight_people_compare(open_tickets | in_progress | done_in_period, order) -> everyone who holds a ticket, by
      that count, most first ("most", and "closer": a count has no closer side) or fewest first ("fewest"), ties by
      name; then, when the board has work nobody holds, one row for it (person_id null, name "Unassigned": no one's,
      never ranked, JO-12 G5); then the whole board's row; in the per-measure columns (standing null: a count is
      never closer or further); open_tickets and in_progress are counts of now, which no period bounds (their params
      carry no days)

The answer built in code (code_built), for the catalogue (which plans such a question in code) and as the analyst's
fallback, never labels anyone and never says "best", and knows where each of its figures came from (built_answer's
hints), so every figure is cited to the row of the person it is about (validate.py):
- a scorecard, for each person: "{Name}, last {days} days, next to the board: closer to the process on {labels};
  further from it on {labels}; level on {labels}." (each clause only when it has labels; "{Name}, last {days} days,
  next to the board." when none has), then "Too few tickets to compare: {labels}."; and once, "Not measurable on
  this board: {labels}." (in the person's paragraph for one person, as a paragraph of its own after several);
- a comparison by a measure: "{Label}, last {days} days, {order}: {Name} {figure}, ... and {Name} {figure}. The whole
  board: {figure}. Too few tickets to compare: {names}.", the first sentence saying the order it used in plain words
  (JO-14): "closest to the process first" (order "closer"), "highest first" ("most") or "lowest first" ("fewest"),
  never calling the highest worst or the lowest best ("Not measurable on this board: {Label}. {reason}" when the
  board cannot show it);
- the overall comparison: "Measures closer to the process than the board, last {days} days, most first: {Name}
  {closer} of {compared}, ... Too few tickets to compare: {names}." ("fewest first" for order "fewest");
- a comparison by a count: "Done in the period, last {days} days, most first: {Name} {count}, ... Unassigned: {count}
  tickets done. The whole board: {count}." ("fewest first" for order "fewest"; "In progress now, most first: ..."
  and "Open tickets, most first: ..." for the counts of now, which no period bounds; the Unassigned sentence only
  when the tickets nobody holds count above 0: "Unassigned: 148 open tickets.", "Unassigned: 1 ticket in
  progress.").
Labels and names are joined "A, B and C", with no comma before "and", as the no-tickets sentence joins names. The
service adds prompts.DECISION_YOURS after a personnel question's figures, and prompts.caveat() as the last paragraph
of any answer that rests on figures over a period (a ranking by a count of now alone has no period to state).

character_labels() is the wording check: an answer (never the question) that calls anyone lazy, useless,
incompetent, careless, dishonest, insincere, stupid, an idiot, hopeless, worthless or pathetic, in any form of the
word (laziness, carelessness, incompetence, lazily, lazybones), or a slacker, a liar or sloppy, is rewritten once,
then replaced by the answer built here. The forms are listed whole, never as a stem and anything after it, and the
names of the people on the board are taken out before the check: a surname such as Lazić or Stupidov is no label.

view_rows() fills the analyst's scorecard view (oversight/ask/analyst.py): the last 90 days, the whole board's rows
(person_id null) and everyone's who holds a ticket, or, with comparing people turned off, the one person the question
mentions (none when it mentions several); each row carries the period (days, period_from, period_to), so an answer
may say it.
"""
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
# The rates of now (the open tickets stuck in a status now, the open findings per 10 open tickets now): no period is
# said beside them, as none is true of them.
NOW_MEASURES = frozenset({"stuck_now", "open_findings_per_10"})
SCORECARD_TOOLS = frozenset((SCORECARD_TOOL, COMPARE_TOOL))
WHOLE_BOARD = "Whole board"
# The tickets nobody holds, said after a ranking by a count (JO-12, G5), one and several: "Unassigned: 148 open
# tickets." The dashboard's sample says the same words.
UNASSIGNED_WORDS: dict[str, tuple[str, str]] = {
    "open_tickets": ("open ticket", "open tickets"),
    "in_progress": ("ticket in progress", "tickets in progress"),
    "done_in_period": ("ticket done", "tickets done"),
}
# The marker words the dashboard shows beside a figure.
STANDING = {"closer": "Closer to the process", "further": "Further from the process", "level": "Level with the board"}
TOO_FEW = "Too few tickets"
# Words that label someone's character: never in an answer, whatever the question says. Each word family's forms
# listed whole (laziness, lazily, lazybones, carelessness, incompetence, stupidity, ...), never a stem and anything
# after it, so a surname that starts the same way (Lazić, Lazidou, Stupidov) or a place (Lazio) is no label; and the
# common insults beside them. "careful", "hopes" and "sincere" are no label either.
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
    """A figure as the dashboard shows it: "72% · 18 of 25" (a rate), "12" (a count), "2.3" (the ratio); None when
    there is none."""
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


# How each fact set reads, its columns named in plain lower-case words mid-sentence (the model reads the same words).
# The dashboard's sample writes the same notes (src/server/sample/ask/scorecard.ts).
_READING = (
    "by each ticket's current assignee, Epics left out; a rate's value is a percentage of its denominator, a count's "
    "value is the count, and open findings per 10 open tickets is the open breaches and warnings on the person's open "
    f"tickets for every 10 of those tickets; enough tickets means the figure rests on at least {MIN_TICKETS} tickets; "
    "next to the board says closer to the process than the board, further from it, or level with it (within 5 "
    "points, 0.5 for the ratio), only when both sides have enough"
)
_OVERALL_READING = (
    "by each ticket's current assignee, Epics left out; the closer, further and level counts are the measures on "
    "which the person's figure stands closer to the process than the board's, further from it, or level with it "
    "(within 5 points, 0.5 for the ratio); measures compared is their sum; too few tickets counts the measures where "
    f"either side rests on fewer than {MIN_TICKETS} tickets; open findings per 10 open tickets is the open breaches "
    "and warnings on the person's open tickets for every 10 of those tickets, compared only when it rests on enough "
    "tickets"
)


# --- the tools' facts ------------------------------------------------------------------------


def scorecard_rows(board: Board, person_ids: Sequence[str], days: int) -> tuple[list[dict[str, Any]], str]:
    """oversight_person_scorecard's rows and note: the people asked about who are on the board, in the directory's
    order, each measure next to the board's."""
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
    """oversight_people_compare's columns, rows and note (see the module docstring). first: the order asked for,
    "closer", "most" or "fewest" (scorecard.order). only: those people alone (two to ten mentioned side by side), in
    the same order, then the whole board, and never the tickets nobody holds (no one asked about them)."""
    period = board.period(days)
    against = board_values(board, period)
    first = effective_order(measure, first)
    ordered = order(standings(board, period, against), measure, first=first)
    if only is not None:
        ordered = [one for one in ordered if one.person["id"] in only]
    everyone = "the people asked about" if only is not None else "everyone who holds a ticket"
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
    # A ranking by a count ranks people only (JO-12, G5): the tickets nobody holds come after it, as one row, never
    # ranked, then the whole board's.
    nobody = [ticket for ticket in board.work if ticket.holder is None] if only is None else []
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
        then = "the tickets nobody holds (Unassigned, never ranked), then the whole board" if nobody else "the whole board"
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
    """A figure in a sentence, cited to its own row: "72%" (a rate, from the figure as shown), "2.3" (the ratio),
    "12" (a count)."""
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
    """The order a comparison's first sentence says it used, in plain words (JO-14): a rate or the ratio "closest to
    the process first", "highest first" or "lowest first"; a count, and the overall number of measures closer, "most
    first" or "fewest first". Never "worst" or "best": it reports the order. first: the facts' params order (none, on
    an older fact set, reads as "closer")."""
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
        # The whole board's row is the last with no person; a count's ranking has the tickets nobody holds before it.
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
            # After the ranking, never in it (JO-12, G5): "Unassigned: 148 open tickets."
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
    """The answer for the scorecard and comparison facts among these (fact sets as Facts.to_dict() writes them), built
    in code: a paragraph each; and where each figure it writes starts -> (fact ref, row index, column) it was read
    from, for validate's hints. None when there are none."""
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
    """The periods of the scorecard figures an answer rests on, for the caveat, each once, shortest first: every
    scorecard and comparison fact set's, and 90 when the analyst read the scorecard view (whose period it is); None
    when it rests on neither. A ranking by a count of now (open tickets, in progress) has no period: it adds none."""
    found = list(facts)
    days = {_days(fact.to_dict()) for fact in found
            if fact.tool in SCORECARD_TOOLS
            and not (fact.tool == COMPARE_TOOL and fact.params.get("measure") in NOW_COUNTS)}
    if any(_READS_THE_VIEW.search(str(fact.params.get("sql") or "")) for fact in found):
        days.add(DEFAULT_DAYS)
    return sorted(days) or None


def character_labels(text: str, names: Iterable[str] = ()) -> list[str]:
    """The words in an answer that label someone's character, each once, lower-cased, in order. names: the display
    names of the people on the board, each word of which is taken out first, as the data writes it (case and all, as
    names are found everywhere), so a person's name is never read as a label, written whole or in part ("Ms Lazy" is
    her surname), while the same word written as a word ("lazy") still is."""
    words = sorted({word for name in names for word in name.split() if len(word) > 1}, key=len, reverse=True)
    if words:
        pattern = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(word) for word in words) + r")(?!\w)")
        text = pattern.sub(lambda match: " " * len(match.group()), text)
    return list(dict.fromkeys(match.group().lower() for match in CHARACTER_LABELS.finditer(text)))


def without_labels(text: str) -> str:
    """The text with every measure's label blanked (as long as it was), so a figure in a label ("per 10 open
    tickets") is never read as anyone's figure."""
    return MEASURE_LABELS.sub(lambda match: " " * len(match.group()), text)


def labels_problem(words: Sequence[str]) -> str:
    """The wording check's problem, as the rewrite is told it."""
    return (f"the answer labels someone's character ({', '.join(words)}): say only what the figures show next to the "
            "board, and leave the judgement to the reader")


# --- the analyst's scorecard view -------------------------------------------------------------


def view_rows(board: Board, *, comparison: bool, mentioned: Sequence[str]) -> list[tuple[Any, ...]]:
    """The scorecard view's rows (VIEW_COLUMNS): the last 90 days; the whole board (person_id null), then everyone
    who holds a ticket, or with comparing people off only the one person the question mentions."""
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

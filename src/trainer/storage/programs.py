"""Programs: their days, blocks and exercises, and where training has got to."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from itertools import groupby
from typing import TYPE_CHECKING

from trainer.domain.exercises import Measure
from trainer.domain.programs import Position, Progression, Session, load_increment

if TYPE_CHECKING:
    import sqlite3
    from datetime import date


class DayKind(StrEnum):
    """Which set of days a program day belongs to."""

    GYM = "gym"  # the main days
    TRAVEL = "travel"  # the optional second set: full-body days for when there is no gym


class Change(StrEnum):
    """How a program version came about."""

    NEW = "new"  # the first version of a program
    EDIT = "edit"  # the live program changed, keeping its place
    RESTART = "restart"  # the same days, back to week 1 day 1


class ProgramStatus(StrEnum):
    """Where a program is in its life."""

    PROPOSED = "proposed"  # by the coach, awaiting the owner
    ACTIVE = "active"  # the one being trained
    ARCHIVED = "archived"  # replaced by another


@dataclass(frozen=True)
class SlotSpec:
    """An exercise to put in a block: sets of a range of reps (or seconds)."""

    exercise_id: int
    sets: int
    rep_min: int
    rep_max: int
    start_load_kg: float | None
    notes: str | None
    progression: Progression = Progression.LOAD
    variations: str | None = None
    # The slot this one continues, from an earlier version: its progression is kept.
    slot_key: int | None = None
    # A slot with no history of its own starts from the exercise's last logged load.
    start_from_log: bool = False


@dataclass(frozen=True)
class BlockSpec:
    """A block to put in a day: more than one exercise makes a superset."""

    rest_s: int
    exercises: tuple[SlotSpec, ...]


@dataclass(frozen=True)
class DaySpec:
    """A day to put in a program."""

    name: str
    blocks: tuple[BlockSpec, ...]
    # The day this one continues, from an earlier version.
    day_key: int | None = None


@dataclass(frozen=True)
class ProgramSpec:
    """A program to save: its gym days, and optionally its travel days."""

    name: str
    notes: str | None
    training_weeks: int
    days: tuple[DaySpec, ...]
    travel_days: tuple[DaySpec, ...] = ()


@dataclass(frozen=True)
class Start:
    """The next day of each set, and the week, as a program version begins."""

    week: int = 1
    day: int = 1
    travel_day: int = 1


@dataclass(frozen=True)
class Lineage:
    """Where a saved program stands among the versions of one program.

    The default is a new program: its own lineage, week 1 day 1.
    """

    lineage_id: int | None = None
    previous_id: int | None = None
    change: Change = Change.NEW


@dataclass(frozen=True)
class ProgramExercise:
    """An exercise in a block, with its catalogue details and load step."""

    id: int
    exercise_id: int
    name: str
    equipment: str | None
    measure: Measure
    sets: int
    rep_min: int
    rep_max: int
    start_load_kg: float | None
    notes: str | None
    increment_kg: float | None
    progression: Progression = Progression.LOAD
    variations: str | None = None
    start_from_log: bool = False
    slot_key: int | None = None


@dataclass(frozen=True)
class ProgramBlock:
    """A block of a day: one exercise, or a superset of several, in order."""

    rest_s: int
    exercises: list[ProgramExercise]


@dataclass(frozen=True)
class ProgramDay:
    """A day of the program, repeated every week; its blocks in order."""

    id: int
    name: str
    blocks: list[ProgramBlock]
    day_key: int | None = None


@dataclass(frozen=True)
class Program:
    """A whole program version; its gym days in order (day 1 first), then its travel days.

    ``version`` counts the versions of one program (``lineage_id``) and ``run``
    its restarts; ``previous_id`` is the version this one replaced.
    ``start`` is the next position when this version began.
    """

    id: int
    name: str
    notes: str | None
    training_weeks: int
    status: ProgramStatus
    created_at: str
    started_at: str | None
    days: list[ProgramDay]
    travel_days: list[ProgramDay] = field(default_factory=list)
    lineage_id: int | None = None
    version: int = 1
    run: int = 1
    change: Change = Change.NEW
    previous_id: int | None = None
    start: Start = field(default_factory=Start)


def insert_program(
    conn: sqlite3.Connection,
    spec: ProgramSpec,
    status: ProgramStatus,
    created_at: str,
    lineage: Lineage | None = None,
) -> int:
    """Save a program with every day, block and exercise; return its id.

    Slots and days that continue an earlier version name it (``slot_key`` and
    ``day_key``); every other one starts its own.
    """
    lineage = lineage or Lineage()
    program_id = _insert(
        conn,
        """
        INSERT INTO programs (name, notes, training_weeks, status, created_at, lineage_id,
            previous_id, change)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id
        """,
        (
            spec.name,
            spec.notes,
            spec.training_weeks,
            status.value,
            created_at,
            lineage.lineage_id,
            lineage.previous_id,
            lineage.change.value,
        ),
    )
    conn.execute(
        """UPDATE programs SET lineage_id = id WHERE id = ? AND lineage_id IS NULL""",
        (program_id,),
    )
    sets = ((DayKind.GYM, spec.days), (DayKind.TRAVEL, spec.travel_days))
    position = 0
    for kind, days in sets:
        for number, day in enumerate(days, start=1):
            position += 1
            _insert_day(conn, (program_id, position), (kind, number), day)
    conn.execute(
        """
        UPDATE program_days SET day_key = id WHERE program_id = ? AND day_key IS NULL
        """,
        (program_id,),
    )
    conn.execute(
        """
        UPDATE block_exercises SET slot_key = id WHERE slot_key IS NULL AND block_id IN (
            SELECT b.id FROM program_blocks b JOIN program_days d ON d.id = b.day_id
            WHERE d.program_id = ?
        )
        """,
        (program_id,),
    )
    return program_id


def _insert_day(
    conn: sqlite3.Connection, place: tuple[int, int], numbered: tuple[DayKind, int], day: DaySpec
) -> None:
    """``place`` is (program id, position among all days), ``numbered`` (kind, number in set)."""
    program_id, position = place
    kind, number = numbered
    day_id = _insert(
        conn,
        """
        INSERT INTO program_days (program_id, position, name, kind, number, day_key)
        VALUES (?, ?, ?, ?, ?, ?) RETURNING id
        """,
        (program_id, position, day.name, kind.value, number, day.day_key),
    )
    for block_position, block in enumerate(day.blocks, start=1):
        block_id = _insert(
            conn,
            """
            INSERT INTO program_blocks (day_id, position, rest_s) VALUES (?, ?, ?)
            RETURNING id
            """,
            (day_id, block_position, block.rest_s),
        )
        conn.executemany(
            """
            INSERT INTO block_exercises (block_id, position, exercise_id, sets, rep_min,
                rep_max, start_load_kg, notes, progression, variations, slot_key,
                start_from_log)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (block_id, slot_position, *_slot_values(slot))
                for slot_position, slot in enumerate(block.exercises, start=1)
            ],
        )


def _slot_values(slot: SlotSpec) -> tuple[object, ...]:
    return (
        slot.exercise_id,
        slot.sets,
        slot.rep_min,
        slot.rep_max,
        slot.start_load_kg,
        slot.notes,
        slot.progression.value,
        slot.variations,
        slot.slot_key,
        int(slot.start_from_log),
    )


def _insert(conn: sqlite3.Connection, sql: str, parameters: tuple[object, ...]) -> int:
    return int(conn.execute(sql, parameters).fetchone()[0])


def program_with_status(conn: sqlite3.Connection, status: ProgramStatus) -> int | None:
    """The id of the proposed or the active program, if there is one."""
    row = conn.execute("""SELECT id FROM programs WHERE status = ?""", (status.value,)).fetchone()
    return None if row is None else int(row[0])


def archive_program(conn: sqlite3.Connection, program_id: int) -> None:
    """Set a program aside: replaced, or turned down.

    Archived, not deleted, so its id is never given to another program: a
    screen still showing it can never accept or turn down a newer one by it.
    """
    conn.execute("""UPDATE programs SET status = 'archived' WHERE id = ?""", (program_id,))


def activate_program(conn: sqlite3.Connection, program_id: int, started_at: str) -> None:
    """Make a program the active one; the one it replaces is archived."""
    conn.execute("""UPDATE programs SET status = 'archived' WHERE status = 'active'""")
    conn.execute(
        """UPDATE programs SET status = 'active', started_at = ? WHERE id = ?""",
        (started_at, program_id),
    )


def activate_version(
    conn: sqlite3.Connection, program_id: int, previous: Program, start: tuple[Change, Start, str]
) -> None:
    """Make ``program_id`` the next version of ``previous``'s program, and the active one.

    ``start`` is (how it came about, its starting position, when it began).
    A restart begins a new run, with its own started date; an edit goes on in
    the same run, so the program keeps the date it was started.
    """
    change, position, now = start
    restart = change is Change.RESTART
    conn.execute("""UPDATE programs SET status = 'archived' WHERE id = ?""", (previous.id,))
    conn.execute(
        """
        UPDATE programs SET status = 'active', started_at = ?, change = ?, previous_id = ?,
            version = (SELECT max(version) FROM programs WHERE lineage_id = ?) + 1,
            run = ?, start_week = ?, start_day = ?, start_travel_day = ?
        WHERE id = ?
        """,
        (
            now if restart else previous.started_at,
            change.value,
            previous.id,
            previous.lineage_id,
            previous.run + int(restart),
            position.week,
            position.day,
            position.travel_day,
            program_id,
        ),
    )


class ProgramNotFoundError(LookupError):
    """No program has the requested id."""


def get_program(conn: sqlite3.Connection, program_id: int) -> Program:
    """A program version with all its days, blocks and exercises.

    Raises:
        ProgramNotFoundError: if there is no such program.
    """
    row = conn.execute(
        """
        SELECT id, name, notes, training_weeks, status, created_at, started_at, lineage_id,
            version, run, change, previous_id, start_week, start_day, start_travel_day
        FROM programs WHERE id = ?
        """,
        (program_id,),
    ).fetchone()
    if row is None:
        message = f"no program {program_id}"
        raise ProgramNotFoundError(message)
    found_id, name, notes, training_weeks, status, created_at, started_at = row[:7]
    lineage_id, version, run, change, previous_id, *start = row[7:]
    days, travel_days = _days(conn, program_id)
    return Program(
        found_id,
        name,
        notes,
        training_weeks,
        ProgramStatus(status),
        created_at,
        started_at,
        days,
        travel_days,
        lineage_id,
        version,
        run,
        Change(change),
        previous_id,
        Start(*start),
    )


def _days(conn: sqlite3.Connection, program_id: int) -> tuple[list[ProgramDay], list[ProgramDay]]:
    """The program's gym days and its travel days, each in order."""
    rows = conn.execute(
        """
        SELECT d.id, d.name, b.id, b.rest_s, x.id, x.exercise_id, e.display_name, e.equipment,
            e.measure, x.sets, x.rep_min, x.rep_max, x.start_load_kg, x.notes,
            e.load_increment_kg, x.progression, x.variations, x.start_from_log, x.slot_key,
            d.day_key, d.kind
        FROM program_days d
        JOIN program_blocks b ON b.day_id = d.id
        JOIN block_exercises x ON x.block_id = b.id
        JOIN exercises e ON e.id = x.exercise_id
        WHERE d.program_id = ?
        ORDER BY d.position, b.position, x.position
        """,
        (program_id,),
    ).fetchall()
    groups = [list(group) for _, group in groupby(rows, key=_day_id)]
    return (
        [_day(group) for group in groups if group[0][20] == DayKind.GYM],
        [_day(group) for group in groups if group[0][20] == DayKind.TRAVEL],
    )


def _day_id(row: sqlite3.Row) -> int:
    return int(row[0])


def _block_id(row: sqlite3.Row) -> int:
    return int(row[2])


def _day(rows: list[sqlite3.Row]) -> ProgramDay:
    """``rows`` are one day's, in order; columns 0-1 are the day's, column 19 its key."""
    day_id, name = rows[0][:2]
    blocks = [_block(list(group)) for _, group in groupby(rows, _block_id)]
    return ProgramDay(day_id, name, blocks, rows[0][19])


def _block(rows: list[sqlite3.Row]) -> ProgramBlock:
    """``rows`` are one block's, in order; column 3 is its rest."""
    return ProgramBlock(rows[0][3], [_exercise(row) for row in rows])


def _exercise(row: sqlite3.Row) -> ProgramExercise:
    """Columns 4 to 18 are the block exercise's, its catalogue entry's and its load step."""
    block_exercise_id, exercise_id, name, equipment, measure = row[4:9]
    sets, rep_min, rep_max, start_load_kg, notes, increment = row[9:15]
    progression, variations, start_from_log, slot_key = row[15:19]
    return ProgramExercise(
        block_exercise_id,
        exercise_id,
        name,
        equipment,
        Measure(measure),
        sets,
        rep_min,
        rep_max,
        start_load_kg,
        notes,
        load_increment(equipment, increment),
        Progression(progression),
        variations,
        bool(start_from_log),
        slot_key,
    )


def last_position(conn: sqlite3.Connection, program_id: int, kind: DayKind) -> Position | None:
    """Week and day number of the last finished workout of one set of days of a version.

    The most recently started one, if the version has any.
    """
    row = conn.execute(
        """
        SELECT w.program_week, d.number FROM workouts w
        JOIN program_days d ON d.id = w.program_day_id
        WHERE d.program_id = ? AND d.kind = ? AND w.ended_at IS NOT NULL
        ORDER BY w.started_at DESC, w.id DESC LIMIT 1
        """,
        (program_id, kind.value),
    ).fetchone()
    return None if row is None else Position(row[0], row[1])


def run_last_session(conn: sqlite3.Connection, program: Program) -> Session | None:
    """The program week and start day of the last finished workout of the program's run.

    Counts the workouts of every version of the program in this run (an edit
    carries on; a restart begins a new run), whichever set of days they trained.
    """
    row = conn.execute(
        """
        SELECT w.program_week, w.started_at FROM workouts w
        JOIN program_days d ON d.id = w.program_day_id
        JOIN programs p ON p.id = d.program_id
        WHERE p.lineage_id = ? AND p.run = ? AND w.ended_at IS NOT NULL
        ORDER BY w.started_at DESC, w.id DESC LIMIT 1
        """,
        (program.lineage_id, program.run),
    ).fetchone()
    return None if row is None else Session(row[0], _day_of(row[1]))


def _day_of(started_at: str) -> date:
    return datetime.fromisoformat(started_at).date()


@dataclass(frozen=True)
class SlotSet:
    """A set logged for a program exercise."""

    reps: int | None
    load_kg: float | None
    duration_s: float | None


def last_slot_sets(
    conn: sqlite3.Connection, block_exercise_id: int, up_to_week: int
) -> list[SlotSet]:
    """The sets of the last finished session of a program exercise, in order.

    A slot keeps its history across versions of the program: sessions under
    any version count. Only sessions from weeks up to ``up_to_week`` count, so
    the deload week never feeds progression.
    """
    rows = conn.execute(
        """
        SELECT reps, load_kg, duration_s FROM workout_sets
        WHERE block_exercise_id IN (SELECT id FROM block_exercises WHERE coalesce(slot_key, id) = (
                SELECT coalesce(slot_key, id) FROM block_exercises WHERE id = ?1))
            AND workout_id = (
                SELECT w.id FROM workouts w JOIN workout_sets s ON s.workout_id = w.id
                JOIN block_exercises x ON x.id = s.block_exercise_id
                WHERE coalesce(x.slot_key, x.id) = (
                        SELECT coalesce(slot_key, id) FROM block_exercises WHERE id = ?1)
                    AND w.ended_at IS NOT NULL AND w.program_week <= ?2
                ORDER BY w.started_at DESC, w.id DESC LIMIT 1
            )
        ORDER BY set_number
        """,
        (block_exercise_id, up_to_week),
    ).fetchall()
    return [SlotSet(*row) for row in rows]


def last_logged_load(conn: sqlite3.Connection, exercise_id: int, up_to_week: int) -> float | None:
    """The heaviest load of the last finished session of an exercise, wherever it was done.

    Workouts outside a program count; program workouts count up to
    ``up_to_week`` (never the deload week). ``None`` if it was never logged
    with a load.
    """
    row = conn.execute(
        """
        SELECT max(s.load_kg) FROM workout_sets s WHERE s.exercise_id = ?1 AND s.workout_id = (
            SELECT w.id FROM workouts w JOIN workout_sets x ON x.workout_id = w.id
            WHERE x.exercise_id = ?1 AND w.ended_at IS NOT NULL
                AND (w.program_week IS NULL OR w.program_week <= ?2)
            ORDER BY w.started_at DESC, w.id DESC LIMIT 1
        )
        """,
        (exercise_id, up_to_week),
    ).fetchone()
    load: float | None = row[0]  # an aggregate: there is always a row, its value maybe NULL
    return load


@dataclass(frozen=True)
class SlotPlace:
    """Which program day and exercise a block exercise belongs to."""

    day_id: int
    exercise_id: int


def slot_place(conn: sqlite3.Connection, block_exercise_id: int) -> SlotPlace | None:
    """The day and exercise of a block exercise, or ``None`` if there is none."""
    row = conn.execute(
        """
        SELECT b.day_id, x.exercise_id FROM block_exercises x
        JOIN program_blocks b ON b.id = x.block_id WHERE x.id = ?
        """,
        (block_exercise_id,),
    ).fetchone()
    return None if row is None else SlotPlace(row[0], row[1])


def set_days(conn: sqlite3.Connection, workout_client_id: str) -> set[int]:
    """The program days that the program sets of a workout belong to."""
    return {
        int(row[0])
        for row in conn.execute(
            """
            SELECT b.day_id FROM workout_sets s
            JOIN workouts w ON w.id = s.workout_id
            JOIN block_exercises x ON x.id = s.block_exercise_id
            JOIN program_blocks b ON b.id = x.block_id
            WHERE w.client_id = ?
            """,
            (workout_client_id,),
        )
    }


@dataclass(frozen=True)
class DayProgram:
    """The program a day belongs to: its weeks, deload included, and whether it is active."""

    weeks: int
    active: bool


def day_program(conn: sqlite3.Connection, day_id: int) -> DayProgram | None:
    """The program of a day, or ``None`` if there is no such day."""
    row = conn.execute(
        """
        SELECT p.training_weeks + 1, p.status = 'active' FROM program_days d
        JOIN programs p ON p.id = d.program_id WHERE d.id = ?
        """,
        (day_id,),
    ).fetchone()
    return None if row is None else DayProgram(int(row[0]), bool(row[1]))


def program_in_progress(conn: sqlite3.Connection, program_id: int) -> bool:
    """Whether an unfinished workout trains a day of the program."""
    row = conn.execute(
        """
        SELECT 1 FROM workouts w JOIN program_days d ON d.id = w.program_day_id
        WHERE d.program_id = ? AND w.ended_at IS NULL
        """,
        (program_id,),
    ).fetchone()
    return row is not None


def other_program_workout_in_progress(conn: sqlite3.Connection, client_id: str) -> bool:
    """Whether a workout other than ``client_id`` trains a program day and is unfinished."""
    row = conn.execute(
        """
        SELECT 1 FROM workouts
        WHERE program_day_id IS NOT NULL AND ended_at IS NULL AND client_id IS NOT ?
        """,
        (client_id,),
    ).fetchone()
    return row is not None


def day_claimed(conn: sqlite3.Connection, day_id: int, week: int) -> bool:
    """Whether a workout, finished or not, trains this program day this week.

    A day is the same day across the versions of its program (an edit keeps
    it), so a workout done under an earlier version counts.
    """
    row = conn.execute(
        """
        SELECT 1 FROM workouts w JOIN program_days d ON d.id = w.program_day_id
        WHERE coalesce(d.day_key, d.id) = (
                SELECT coalesce(day_key, id) FROM program_days WHERE id = ?)
            AND w.program_week = ?
        """,
        (day_id, week),
    ).fetchone()
    return row is not None


@dataclass(frozen=True)
class VersionSummary:
    """One version of a program, as the history lists it."""

    id: int
    version: int
    run: int
    change: Change
    name: str
    started_at: str
    current: bool
    # Finished workouts trained under this version.
    workouts: int


def list_versions(conn: sqlite3.Connection, lineage_id: int | None) -> list[VersionSummary]:
    """The versions of a program that were ever trained, newest first.

    A proposed edit the owner turned down never started, so it is not one.
    """
    rows = conn.execute(
        """
        SELECT p.id, p.version, p.run, p.change, p.name, p.started_at, p.status = 'active',
            (SELECT count(*) FROM workouts w JOIN program_days d ON d.id = w.program_day_id
             WHERE d.program_id = p.id AND w.ended_at IS NOT NULL)
        FROM programs p WHERE p.lineage_id = ? AND p.started_at IS NOT NULL
        ORDER BY p.version DESC
        """,
        (lineage_id,),
    ).fetchall()
    return [
        VersionSummary(id_, version, run, Change(change), name, started, bool(current), count)
        for id_, version, run, change, name, started, current, count in rows
    ]

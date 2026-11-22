"""Programs: the coach's proposals, the active block, and what today holds."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from trainer.domain.exercises import Measure
from trainer.domain.programs import (
    DELOAD_RPE,
    TRAINING_WEEKS,
    Position,
    Prescription,
    Progression,
    SetDone,
    Target,
    calendar_week,
    is_deload_week,
    next_position,
    next_rotation_day,
    target_for,
    travel_aware_position,
)
from trainer.domain.records import carried_load
from trainer.domain.times import utc_date, utc_iso
from trainer.storage.database import write_transaction
from trainer.storage.history import (
    ExerciseSession,
    WorkoutDetail,
    last_session,
    list_exercises,
    program_workout_in_progress,
)
from trainer.storage.profile import read_profile
from trainer.storage.programs import (
    BlockSpec,
    Change,
    DayKind,
    DaySpec,
    Lineage,
    Program,
    ProgramDay,
    ProgramExercise,
    ProgramSpec,
    ProgramStatus,
    SlotSet,
    SlotSpec,
    Start,
    VersionSummary,
    activate_program,
    activate_version,
    archive_program,
    day_claimed,
    get_program,
    insert_program,
    last_logged_load,
    last_position,
    last_slot_sets,
    list_versions,
    program_in_progress,
    program_with_status,
    run_last_session,
)

if TYPE_CHECKING:
    import sqlite3
    from datetime import date, datetime

# SQLite's INTEGER is signed 64-bit; a larger id would overflow in the query.
MAX_ROW_ID = 2**63 - 1
# A plain alias, not a `type` statement, so a schema shows the bounds inline.
RowId = Annotated[int, Field(ge=1, le=MAX_ROW_ID)]

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _Shape(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SlotIn(_Shape):
    """One exercise of a block."""

    exercise_id: Annotated[RowId, Field(description="An exercise id from the catalogue.")]
    sets: Annotated[int, Field(ge=1, le=10, description="Working sets.")]
    rep_min: Annotated[
        int,
        Field(ge=1, le=600, description="Bottom of the range: reps, or seconds if timed."),
    ]
    rep_max: Annotated[
        int,
        Field(
            ge=1,
            le=600,
            description="Top of the range. Reaching it on every set adds load next time.",
        ),
    ]
    start_load_kg: (
        Annotated[
            float,
            Field(
                ge=0,
                le=1000,
                description=(
                    "Load for the first session in kg: per dumbbell, or added to body weight. "
                    "Leave out to let the owner choose."
                ),
            ),
        ]
        | None
    ) = None
    notes: Annotated[Text, StringConstraints(max_length=200)] | None = None
    progression: Annotated[
        Progression,
        Field(
            description=(
                "load (default): double progression, the load rises by kg. reps: bodyweight "
                "or a band, for travel days: progresses by reps, then a harder variation; "
                "the app never suggests a kg load, so leave start_load_kg out."
            )
        ),
    ] = Progression.LOAD
    variations: (
        Annotated[
            Text,
            StringConstraints(max_length=200),
            Field(
                description=(
                    "For progression=reps: the harder variations or band levels to move "
                    "through at the top of the range, as free text, e.g. "
                    "'push-up -> feet-elevated -> archer'."
                )
            ),
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def range_in_order(self) -> Self:
        """The top of the range is not below its bottom; a reps-only slot has no load."""
        if self.rep_max < self.rep_min:
            message = "rep_max is below rep_min"
            raise ValueError(message)
        if self.progression is Progression.REPS and self.start_load_kg is not None:
            message = "a reps progression has no load: leave start_load_kg out"
            raise ValueError(message)
        return self


class BlockIn(_Shape):
    """A block: one exercise, or a superset of two or three."""

    rest_s: Annotated[
        int, Field(ge=0, le=600, description="Rest after each set or round, in seconds.")
    ] = 90
    exercises: Annotated[
        list[SlotIn],
        Field(
            min_length=1,
            max_length=3,
            description="One exercise, or two or three done as a superset (A1, A2...).",
        ),
    ]


class DayIn(_Shape):
    """A day of the program, repeated every week."""

    name: Annotated[Text, StringConstraints(max_length=40)]
    blocks: Annotated[list[BlockIn], Field(min_length=1, max_length=12)]


TRAVEL_DESCRIPTION = (
    "Optional second set of days for when there is no gym, e.g. three full-body "
    "band/bodyweight days. Travel days rotate on their own: a travel session does "
    "not advance the gym days and the reverse. Weeks count calendar weeks whichever set "
    "was trained, and the deload week applies to both sets."
)


class ProgramIn(_Shape):
    """A program: its days, trained in order every week for six weeks, then a deload week."""

    name: Annotated[Text, StringConstraints(max_length=60)]
    notes: Annotated[Text, StringConstraints(max_length=2000)] | None = None
    days: Annotated[list[DayIn], Field(min_length=1, max_length=7, description="The gym days.")]
    travel_days: Annotated[
        list[DayIn], Field(default_factory=list, max_length=7, description=TRAVEL_DESCRIPTION)
    ]


class SlotEdit(SlotIn):
    """An exercise of a block in an edit: ``id`` keeps an existing one's progression."""

    id: Annotated[
        RowId | None,
        Field(description="The exercise's id from current_program; leave out for a new one."),
    ] = None


class BlockEdit(_Shape):
    """A block in an edit."""

    rest_s: Annotated[
        int, Field(ge=0, le=600, description="Rest after each set or round, in seconds.")
    ] = 90
    exercises: Annotated[list[SlotEdit], Field(min_length=1, max_length=3)]


class DayEdit(_Shape):
    """A day in an edit: ``id`` keeps an existing day's place in the rotation."""

    id: Annotated[
        RowId | None,
        Field(description="The day's id from current_program; leave out for a new day."),
    ] = None
    name: Annotated[Text, StringConstraints(max_length=40)]
    blocks: Annotated[list[BlockEdit], Field(min_length=1, max_length=12)]


class ProgramEdit(_Shape):
    """The whole live program as it should be after an edit.

    Days, blocks and exercises not listed are removed; ones listed with an id
    keep their progression, and without one are new.
    """

    name: Annotated[Text, StringConstraints(max_length=60)]
    notes: Annotated[Text, StringConstraints(max_length=2000)] | None = None
    days: Annotated[list[DayEdit], Field(min_length=1, max_length=7, description="The gym days.")]
    travel_days: Annotated[
        list[DayEdit], Field(default_factory=list, max_length=7, description=TRAVEL_DESCRIPTION)
    ]


class ProgramError(ValueError):
    """A program names exercises that cannot be in it."""


class NoProposalError(LookupError):
    """There is no such proposed program."""


class ProgramInUseError(RuntimeError):
    """The active program has a workout in progress, so it cannot be replaced yet."""


class NoActiveProgramError(LookupError):
    """There is no active program to edit or restart."""


@dataclass(frozen=True)
class _Keys:
    """What an edit's ids stand for in the live program: slot keys and day keys."""

    slots: dict[int, int]
    days: dict[int, int]

    @classmethod
    def of(cls, program: Program) -> Self:
        """The keys of ``program``'s exercises and days."""
        days = [*program.days, *program.travel_days]
        slots = {
            exercise.id: exercise.slot_key
            for day in days
            for block in day.blocks
            for exercise in block.exercises
            if exercise.slot_key is not None
        }
        return cls(slots, {day.id: day.day_key for day in days if day.day_key is not None})


def _slot_spec(slot: SlotIn | SlotEdit, keys: _Keys | None) -> SlotSpec:
    """``keys`` are the live program's, when this is an edit of it."""
    kept = None
    # why: an edit always comes with its keys; `or` for `and` is equal for every real caller
    if keys is not None and isinstance(slot, SlotEdit) and slot.id is not None:  # pragma: no mutate
        kept = keys.slots.get(slot.id)
    return SlotSpec(
        slot.exercise_id,
        slot.sets,
        slot.rep_min,
        slot.rep_max,
        slot.start_load_kg,
        slot.notes,
        slot.progression,
        slot.variations,
        kept,
        # A slot new to an edit starts from what was last logged for its exercise.
        start_from_log=isinstance(slot, SlotEdit) and kept is None,
    )


def _day_spec(day: DayIn | DayEdit, keys: _Keys | None) -> DaySpec:
    kept = None
    # why: an edit always comes with its keys; `or` for `and` is equal for every real caller
    if keys is not None and isinstance(day, DayEdit) and day.id is not None:  # pragma: no mutate
        kept = keys.days.get(day.id)
    return DaySpec(
        day.name,
        tuple(
            BlockSpec(block.rest_s, tuple(_slot_spec(slot, keys) for slot in block.exercises))
            for block in day.blocks
        ),
        kept,
    )


def _spec(program: ProgramIn | ProgramEdit, keys: _Keys | None = None) -> ProgramSpec:
    """The program to save; ``keys`` are the live program's, when this is an edit of it."""
    return ProgramSpec(
        program.name,
        program.notes,
        TRAINING_WEEKS,
        tuple(_day_spec(day, keys) for day in program.days),
        tuple(_day_spec(day, keys) for day in program.travel_days),
    )


def _check_exercises(conn: sqlite3.Connection, program: ProgramIn | ProgramEdit) -> None:
    measures = {exercise.id: exercise.measure for exercise in list_exercises(conn)}
    wanted = {
        slot.exercise_id
        for day in [*program.days, *program.travel_days]
        for block in day.blocks
        for slot in block.exercises
    }
    unknown = sorted(wanted - measures.keys())
    if unknown:
        message = f"no exercise has id {', '.join(map(str, unknown))}"
        raise ProgramError(message)
    distance = sorted(id_ for id_ in wanted if measures[id_] is Measure.DISTANCE)
    if distance:
        message = f"exercise {', '.join(map(str, distance))} is measured in distance"
        raise ProgramError(message)


def _check_ids(program: ProgramEdit, keys: _Keys) -> None:
    """Every id an edit names is one of the live program's, and named once."""
    days = [*program.days, *program.travel_days]
    day_ids = [day.id for day in days if day.id is not None]
    slot_ids = [
        slot.id
        for day in days
        for block in day.blocks
        for slot in block.exercises
        if slot.id is not None
    ]
    for kind, named, known in (("day", day_ids, keys.days), ("exercise", slot_ids, keys.slots)):
        unknown = sorted(set(named) - known.keys())
        if unknown:
            message = f"the live program has no {kind} with id {', '.join(map(str, unknown))}"
            raise ProgramError(message)
        repeated = sorted({id_ for id_ in named if named.count(id_) > 1})
        if repeated:
            message = f"{kind} id {', '.join(map(str, repeated))} is used more than once"
            raise ProgramError(message)


def propose_program(conn: sqlite3.Connection, program: ProgramIn, now: datetime) -> Program:
    """Save ``program`` as the proposal; any earlier one is archived.

    Raises:
        ProgramError: if an exercise does not exist or is measured in distance.
    """
    with write_transaction(conn):
        _check_exercises(conn, program)
        _archive_proposal(conn)
        program_id = insert_program(conn, _spec(program), ProgramStatus.PROPOSED, utc_iso(now))
    return get_program(conn, program_id)


def _archive_proposal(conn: sqlite3.Connection) -> None:
    earlier = program_with_status(conn, ProgramStatus.PROPOSED)
    if earlier is not None:
        archive_program(conn, earlier)


def _live(conn: sqlite3.Connection) -> Program:
    """The active program.

    Raises:
        NoActiveProgramError: if there is none.
    """
    active = program_with_status(conn, ProgramStatus.ACTIVE)
    if active is None:
        message = "there is no program being trained"
        raise NoActiveProgramError(message)
    return get_program(conn, active)


def propose_edit(conn: sqlite3.Connection, edit: ProgramEdit, now: datetime) -> Program:
    """Save ``edit`` of the active program as the proposal; any earlier proposal is archived.

    The active program is unchanged until the owner accepts the proposal, and
    accepting applies it as an edit: the program keeps its place and history.

    Raises:
        NoActiveProgramError: if there is no active program to edit.
        ProgramError: if an exercise does not exist or is measured in distance,
            or an id is not one of the active program's.
    """
    with write_transaction(conn):
        live = _live(conn)
        keys = _check_edit(conn, edit, live)
        _archive_proposal(conn)
        lineage = Lineage(live.lineage_id, live.id, Change.EDIT)
        program_id = insert_program(
            conn, _spec(edit, keys), ProgramStatus.PROPOSED, utc_iso(now), lineage
        )
    return get_program(conn, program_id)


def _check_edit(conn: sqlite3.Connection, edit: ProgramEdit, live: Program) -> _Keys:
    _check_exercises(conn, edit)
    keys = _Keys.of(live)
    _check_ids(edit, keys)
    return keys


def edit_program(conn: sqlite3.Connection, edit: ProgramEdit, now: datetime) -> Program:
    """Apply ``edit`` to the active program now, as a new version of it.

    The week and next days are kept, and so are the logged history and every
    kept exercise's progression; the version it replaces is archived.

    Raises:
        NoActiveProgramError: if there is no active program.
        ProgramError: if an exercise does not exist or is measured in distance,
            or an id is not one of the active program's.
        ProgramInUseError: if a workout of the program is in progress.
    """
    with write_transaction(conn):
        live = _live(conn)
        _check_idle(conn, live)
        keys = _check_edit(conn, edit, live)
        program_id = insert_program(
            conn, _spec(edit, keys), ProgramStatus.ARCHIVED, utc_iso(now), Lineage(live.lineage_id)
        )
        _start_version(conn, program_id, live, (Change.EDIT, now))
    return get_program(conn, program_id)


def restart_program(conn: sqlite3.Connection, now: datetime) -> Program:
    """Restart the active program: the same days, back to week 1 day 1 of both sets.

    A new version of the program in a new run. Logged history is kept; each
    slot starts afresh from the last load logged for its exercise.

    Raises:
        NoActiveProgramError: if there is no active program.
        ProgramInUseError: if a workout of the program is in progress.
    """
    with write_transaction(conn):
        live = _live(conn)
        _check_idle(conn, live)
        program_id = insert_program(
            conn,
            _restart_spec(live),
            ProgramStatus.ARCHIVED,
            utc_iso(now),
            Lineage(live.lineage_id),
        )
        _start_version(conn, program_id, live, (Change.RESTART, now))
    return get_program(conn, program_id)


def _restart_spec(program: Program) -> ProgramSpec:
    """The program's days again, each exercise new: no progression carries over."""

    def day(source: ProgramDay) -> DaySpec:
        return DaySpec(
            source.name,
            tuple(
                BlockSpec(
                    block.rest_s,
                    tuple(
                        SlotSpec(
                            slot.exercise_id,
                            slot.sets,
                            slot.rep_min,
                            slot.rep_max,
                            slot.start_load_kg,
                            slot.notes,
                            slot.progression,
                            slot.variations,
                            start_from_log=True,
                        )
                        for slot in block.exercises
                    ),
                )
                for block in source.blocks
            ),
        )

    return ProgramSpec(
        program.name,
        program.notes,
        program.training_weeks,
        tuple(day(source) for source in program.days),
        tuple(day(source) for source in program.travel_days),
    )


def _check_idle(conn: sqlite3.Connection, live: Program) -> None:
    if program_in_progress(conn, live.id):
        message = "finish or discard the workout in progress first"
        raise ProgramInUseError(message)


def _start_version(
    conn: sqlite3.Connection, program_id: int, live: Program, how: tuple[Change, datetime]
) -> None:
    """Make ``program_id`` the active version after ``live``: from the same place, or the start."""
    change, now = how
    if change is Change.RESTART:
        start = Start()
    else:
        start = _kept_start(conn, live, get_program(conn, program_id), now)
    activate_version(conn, program_id, live, (change, start, utc_iso(now)))


def _kept_start(conn: sqlite3.Connection, live: Program, new: Program, now: datetime) -> Start:
    """Where ``new``, an edit of ``live``, carries on: the same week, and each set's next day.

    A day that is still there is the next day again, wherever the edit moved
    it; if it was removed, the next day takes its number (or the last one).
    """
    gym, travel = _positions(conn, live, None, utc_date(now))
    week = TRAINING_WEEKS + 2 if gym is None else gym.week
    return Start(
        week,
        _renumbered(live.days, new.days, gym),
        _renumbered(live.travel_days, new.travel_days, travel),
    )


def _renumbered(
    before: list[ProgramDay], after: list[ProgramDay], next_day: Position | None
) -> int:
    """The number, among ``after``, of the day that was next among ``before``."""
    if next_day is None or not after:
        return 1
    key = before[next_day.day - 1].day_key
    for number, day in enumerate(after, start=1):
        if key is not None and day.day_key == key:
            return number
    return min(next_day.day, len(after))


def decline_proposal(conn: sqlite3.Connection, program_id: int) -> None:
    """Turn down the proposed program ``program_id``: it is archived.

    Raises:
        NoProposalError: if ``program_id`` is not the proposed program (one
            the owner has not seen may have replaced it).
    """
    with write_transaction(conn):
        _check_proposal(conn, program_id)
        archive_program(conn, program_id)


def _check_proposal(conn: sqlite3.Connection, program_id: int) -> None:
    if program_with_status(conn, ProgramStatus.PROPOSED) != program_id:
        message = f"program {program_id} is not the proposal"
        raise NoProposalError(message)


def accept_proposal(conn: sqlite3.Connection, program_id: int, now: datetime) -> Program:
    """Start training the proposed program ``program_id``; the active one is archived.

    A proposed edit of the active program is applied as an edit instead: the
    program keeps its place, history and progression.

    Raises:
        NoProposalError: if ``program_id`` is not the proposed program, or it
            is an edit of a version that is no longer the active one.
        ProgramInUseError: if a workout of the active program is in progress:
            replacing the program then would leave that workout without a plan.
    """
    with write_transaction(conn):
        _check_proposal(conn, program_id)
        proposed = get_program(conn, program_id)
        active = program_with_status(conn, ProgramStatus.ACTIVE)
        live = None if active is None else get_program(conn, active)
        if live is not None:
            _check_idle(conn, live)
        if proposed.change is Change.EDIT:
            _apply_edit(conn, proposed, live, now)
        else:
            activate_program(conn, program_id, utc_iso(now))
    return get_program(conn, program_id)


def _apply_edit(
    conn: sqlite3.Connection, proposed: Program, live: Program | None, now: datetime
) -> None:
    if live is None or proposed.previous_id != live.id:
        message = f"program {proposed.id} edits a version that is no longer the live program"
        raise NoProposalError(message)
    _start_version(conn, proposed.id, live, (Change.EDIT, now))


@dataclass(frozen=True)
class Programs:
    """The active and the proposed program, and each set's next day in the active one.

    ``next`` is the next gym day and ``next_travel`` the next travel day:
    ``None`` without an active program (or without travel days), or once its
    block is done.
    """

    active: Program | None
    proposed: Program | None
    next: Position | None
    next_travel: Position | None = None


def programs(conn: sqlite3.Connection, now: datetime) -> Programs:
    """The programs the owner can see: what is trained, and what is proposed."""
    active_id = program_with_status(conn, ProgramStatus.ACTIVE)
    proposed_id = program_with_status(conn, ProgramStatus.PROPOSED)
    active = None if active_id is None else get_program(conn, active_id)
    proposed = None if proposed_id is None else get_program(conn, proposed_id)
    current = program_workout_in_progress(conn)
    if active is None:
        return Programs(None, proposed, None)
    following, travel = _positions(conn, active, current, utc_date(now))
    return Programs(active, proposed, following, travel)


def versions(conn: sqlite3.Connection) -> list[VersionSummary]:
    """The versions of the active program, newest first; none without an active program."""
    active = program_with_status(conn, ProgramStatus.ACTIVE)
    if active is None:
        return []
    return list_versions(conn, get_program(conn, active).lineage_id)


@dataclass(frozen=True)
class PlannedExercise:
    """An exercise of today's day: its prescription, its target and last time."""

    block_exercise_id: int
    exercise_id: int
    name: str
    equipment: str | None
    measure: Measure
    # Share of body weight moved in each rep, on top of the load (bodyweight exercises).
    carried_kg: float
    sets: int
    rep_min: int
    rep_max: int
    notes: str | None
    target: Target
    # The exercise's most recent session outside today's workout, wherever it was.
    last: ExerciseSession | None
    progression: Progression = Progression.LOAD
    # The harder variations to move through at the top of the range, for reps-only slots.
    variations: str | None = None


@dataclass(frozen=True)
class PlannedBlock:
    """A block of today's day: more than one exercise is a superset."""

    rest_s: int
    exercises: list[PlannedExercise]


@dataclass(frozen=True)
class PlannedDay:
    """Today's program day, in its week.

    ``position`` is its number within its set (gym or travel). In the deload
    week ``deload_rpe`` is the effort to hold: easy, about RPE 6.
    """

    id: int
    position: int
    name: str
    week: int
    deload: bool
    blocks: list[PlannedBlock]
    kind: DayKind = DayKind.GYM
    deload_rpe: int | None = None


@dataclass(frozen=True)
class Today:
    """What the active program holds next.

    ``day`` is the next gym day and ``travel_day`` the next travel day: each
    is ``None`` once the block is done, and ``travel_day`` also without travel
    days. ``workout_client_id`` is the unfinished workout already training a
    day, if there is one. ``left_over`` is an unfinished workout of a replaced
    program (data from before that was refused): it must be finished first.
    All come from the same read, so a screen never has to pair two answers.
    """

    program_id: int
    program_name: str
    training_weeks: int
    days: int
    day: PlannedDay | None
    workout_client_id: str | None
    left_over: WorkoutDetail | None
    travel_days: int = 0
    travel_day: PlannedDay | None = None
    # Which set of days the unfinished workout trains, if there is one.
    training_kind: DayKind | None = None


def today(conn: sqlite3.Connection, now: datetime) -> Today | None:
    """The next days of the active program, planned; ``None`` without an active program."""
    program_id = program_with_status(conn, ProgramStatus.ACTIVE)
    if program_id is None:
        return None
    program = get_program(conn, program_id)
    current = program_workout_in_progress(conn)
    gym, travel = _positions(conn, program, current, utc_date(now))
    training = _in_progress(program, current)
    return Today(
        program.id,
        program.name,
        program.training_weeks,
        len(program.days),
        None if gym is None else _plan(conn, program, (DayKind.GYM, gym), current),
        None if training is None or current is None else current.client_id,
        current if training is None else None,
        len(program.travel_days),
        None if travel is None else _plan(conn, program, (DayKind.TRAVEL, travel), current),
        None if training is None else training[0],
    )


# coverage-critical
def _in_progress(
    program: Program, current: WorkoutDetail | None
) -> tuple[DayKind, Position] | None:
    """The set and day the unfinished workout ``current`` trains, if it is a day of ``program``."""
    if current is None or current.program_week is None:
        return None
    for kind, days in ((DayKind.GYM, program.days), (DayKind.TRAVEL, program.travel_days)):
        for day_number, day in enumerate(days, start=1):
            if day.id == current.program_day_id:
                return kind, Position(current.program_week, day_number)
    return None


# coverage-critical
def _positions(
    conn: sqlite3.Connection, program: Program, current: WorkoutDetail | None, day: date
) -> tuple[Position | None, Position | None]:
    """The next gym day and the next travel day, each ``None`` if it has none.

    A day being trained (a workout in progress) is that set's day; the other
    set's is the next one. Without travel days the position follows the
    sequence of completed gym days alone, as it always has; with them, weeks
    count calendar weeks (see :func:`trainer.domain.programs.calendar_week`).
    """
    training = _in_progress(program, current)
    if program.travel_days:
        gym, travel = _travel_aware(conn, program, day)
    else:
        gym, travel = _sequence(conn, program), None
    if training is not None and training[0] is DayKind.GYM:
        gym = training[1]
    if training is not None and training[0] is DayKind.TRAVEL:
        travel = training[1]
    return gym, travel


def _sequence(conn: sqlite3.Connection, program: Program) -> Position | None:
    """The next day of a program with gym days alone: the one after the last finished."""
    done = last_position(conn, program.id, DayKind.GYM)
    if done is not None:
        return next_position(done, len(program.days), program.training_weeks)
    # Nothing finished under this version: where it began (an edit keeps its place).
    start = Position(program.start.week, program.start.day)
    return None if start.week > program.training_weeks + 1 else start


def _travel_aware(
    conn: sqlite3.Connection, program: Program, day: date
) -> tuple[Position | None, Position | None]:
    week = calendar_week(run_last_session(conn, program), day)
    return (
        _rotation(conn, program, (DayKind.GYM, week)),
        _rotation(conn, program, (DayKind.TRAVEL, week)),
    )


def _rotation(
    conn: sqlite3.Connection, program: Program, place: tuple[DayKind, int]
) -> Position | None:
    """The next day of one set, in calendar week ``week``, if the block is not over."""
    kind, week = place
    days, first = (
        (program.days, program.start.day)
        if kind is DayKind.GYM
        else (program.travel_days, program.start.travel_day)
    )
    done = last_position(conn, program.id, kind)
    number = next_rotation_day(None if done is None else done.day, first, len(days))
    return travel_aware_position(
        week,
        number,
        claimed=day_claimed(conn, days[number - 1].id, week),
        training_weeks=program.training_weeks,
    )


def _plan(
    conn: sqlite3.Connection,
    program: Program,
    place: tuple[DayKind, Position],
    current: WorkoutDetail | None,
) -> PlannedDay:
    kind, position = place
    days = program.days if kind is DayKind.GYM else program.travel_days
    day = days[position.day - 1]
    deload = is_deload_week(position.week, program.training_weeks)
    bodyweight = read_profile(conn).bodyweight_kg
    exclude = None if current is None else current.id
    return PlannedDay(
        day.id,
        position.day,
        day.name,
        position.week,
        deload,
        [
            PlannedBlock(
                block.rest_s,
                [
                    _planned(conn, (exercise, program.training_weeks, deload), bodyweight, exclude)
                    for exercise in block.exercises
                ],
            )
            for block in day.blocks
        ],
        kind,
        DELOAD_RPE if deload else None,
    )


def _prescription(
    conn: sqlite3.Connection, exercise: ProgramExercise, training_weeks: int, *, fresh: bool
) -> Prescription:
    """What the exercise prescribes; ``fresh`` (no history of its own) may start from the log."""
    start = exercise.start_load_kg
    if fresh and exercise.start_from_log:
        # A slot new to an edit or a restart: the last logged load wins over its start load.
        logged = last_logged_load(conn, exercise.exercise_id, training_weeks)
        start = start if logged is None else logged
    return Prescription(
        exercise.sets,
        exercise.rep_min,
        exercise.rep_max,
        start,
        exercise.increment_kg,
        reps_only=exercise.progression is Progression.REPS,
    )


def _planned(
    conn: sqlite3.Connection,
    slot: tuple[ProgramExercise, int, bool],
    bodyweight: float | None,
    exclude: int | None,
) -> PlannedExercise:
    """``slot`` is (the program exercise, the program's training weeks, deload week)."""
    exercise, training_weeks, deload = slot
    done = [
        SetDone(_amount(exercise.measure, logged), logged.load_kg)
        for logged in last_slot_sets(conn, exercise.id, training_weeks)
    ]
    prescription = _prescription(conn, exercise, training_weeks, fresh=not done)
    return PlannedExercise(
        exercise.id,
        exercise.exercise_id,
        exercise.name,
        exercise.equipment,
        exercise.measure,
        carried_load(exercise.name, exercise.equipment, bodyweight),
        exercise.sets,
        exercise.rep_min,
        exercise.rep_max,
        exercise.notes,
        target_for(prescription, done, deload_week=deload),
        last_session(conn, exercise.exercise_id, exclude),
        exercise.progression,
        exercise.variations,
    )


# coverage-critical
def _amount(measure: Measure, logged: SlotSet) -> float:
    """What a set counts towards its range: seconds if timed, else reps; none counts 0."""
    amount = logged.duration_s if measure is Measure.SECONDS else logged.reps
    return amount or 0

"""The coach's tools: reading the log (D10), proposing a program (D12), adding exercises."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from trainer.mcp.protocol import Json, Tool, ToolError
from trainer.services.journal import ExerciseIn
from trainer.services.programs import ProgramEdit, ProgramIn, programs
from trainer.storage.database import connect_readonly
from trainer.storage.history import (
    WorkoutNotFoundError,
    exercise_history,
    get_workout,
    list_exercises,
    list_workouts,
)
from trainer.storage.profile import read_profile
from trainer.storage.programs import Change

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from trainer.mcp.proposals import ProgramsApi
    from trainer.storage.programs import ProgramDay


# SQLite's INTEGER is signed 64-bit, and row ids start at 1. A larger Python int
# would raise OverflowError in the query instead of finding nothing.
MAX_ROW_ID = 2**63 - 1
# A plain alias, not a `type` statement, so the schema shows the bounds inline.
RowId = Annotated[int, Field(ge=1, le=MAX_ROW_ID)]


class _Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RecentWorkouts(_Arguments):
    """Arguments of ``recent_workouts``."""

    limit: Annotated[int, Field(ge=1, le=50, description="How many workouts, newest first.")] = 10


class Workout(_Arguments):
    """Arguments of ``get_workout``."""

    workout_id: Annotated[RowId, Field(description="A workout id from recent_workouts.")]


class Catalogue(_Arguments):
    """Arguments of ``list_exercises``: none."""


class History(_Arguments):
    """Arguments of ``exercise_history``."""

    exercise_id: Annotated[RowId, Field(description="An exercise id from list_exercises.")]
    sessions: Annotated[
        int, Field(ge=1, le=100, description="How many sessions, newest first.")
    ] = 20


class BodyWeight(_Arguments):
    """Arguments of ``body_weight``: none."""


class CurrentProgram(_Arguments):
    """Arguments of ``current_program``."""

    detail: Annotated[
        Literal["summary", "full"],
        Field(
            description=(
                "summary: the days and exercises, enough to talk about the program. "
                "full: also every note and start load; read it before proposing a change."
            )
        ),
    ] = "summary"


def _recent_workouts(conn: sqlite3.Connection, arguments: RecentWorkouts) -> object:
    return [asdict(summary) for summary in list_workouts(conn, arguments.limit)]


def _get_workout(conn: sqlite3.Connection, arguments: Workout) -> object:
    try:
        return asdict(get_workout(conn, arguments.workout_id))
    except WorkoutNotFoundError as error:
        message = f"there is no workout {arguments.workout_id}"
        raise ToolError(message) from error


def _list_exercises(conn: sqlite3.Connection, _arguments: Catalogue) -> object:
    return [asdict(summary) for summary in list_exercises(conn)]


def _exercise_history(conn: sqlite3.Connection, arguments: History) -> object:
    found = exercise_history(conn, arguments.exercise_id)
    if found is None:
        message = f"there is no exercise {arguments.exercise_id}"
        raise ToolError(message)
    return asdict(replace(found, sessions=found.sessions[: arguments.sessions]))


def _body_weight(conn: sqlite3.Connection, _arguments: BodyWeight) -> object:
    return asdict(read_profile(conn))


def _day_summary(day: ProgramDay) -> dict[str, object]:
    """One day, without notes or loads; ids stay so a set can be logged against it."""
    return {
        "id": day.id,
        "name": day.name,
        "blocks": [
            {
                "rest_s": block.rest_s,
                "exercises": [
                    {
                        "id": exercise.id,
                        "exercise_id": exercise.exercise_id,
                        "name": exercise.name,
                        "measure": exercise.measure,
                        "sets": exercise.sets,
                        "rep_min": exercise.rep_min,
                        "rep_max": exercise.rep_max,
                        "progression": exercise.progression,
                    }
                    for exercise in block.exercises
                ],
            }
            for block in day.blocks
        ],
    }


def _current_program(conn: sqlite3.Connection, arguments: CurrentProgram) -> object:
    found = programs(conn, datetime.now(UTC))
    if arguments.detail == "full":
        return asdict(found)
    # Every request carries the whole conversation, tool results included, so the
    # everyday answer leaves out the notes and loads only a new proposal needs.
    # Day and exercise ids stay: recording a program workout needs them.
    active = found.active
    proposed = found.proposed
    return {
        "active": None
        if active is None
        else {
            "name": active.name,
            "version": active.version,
            "training_weeks": active.training_weeks,
            "started_at": active.started_at,
            "days": [_day_summary(day) for day in active.days],
            "travel_days": [_day_summary(day) for day in active.travel_days],
        },
        "next": None if found.next is None else asdict(found.next),
        "next_travel": None if found.next_travel is None else asdict(found.next_travel),
        "proposed": None
        if proposed is None
        else {
            "name": proposed.name,
            "edit": proposed.change is Change.EDIT,
            "days": [day.name for day in proposed.days],
            "travel_days": [day.name for day in proposed.travel_days],
        },
    }


# Each tool's description, argument model and answer, by name.
_TOOLS: dict[str, tuple[str, type[_Arguments], Callable[[sqlite3.Connection, Any], object]]] = {
    "recent_workouts": (
        (
            "The most recent workouts, newest first: when, each exercise with its set "
            "count and best set, the total set count and volume in kg."
        ),
        RecentWorkouts,
        _recent_workouts,
    ),
    "get_workout": (
        (
            "One workout with every set: reps, load_kg, duration_s, rpe and notes per set, "
            "grouped by exercise in the order done. carried_kg is the body weight each rep "
            "of a bodyweight exercise moves on top of load_kg."
        ),
        Workout,
        _get_workout,
    ),
    "list_exercises": (
        (
            "The exercise catalogue: id, name, equipment, muscle groups, measure (reps or "
            "seconds), how many workouts included it, when it was last done, best load."
        ),
        Catalogue,
        _list_exercises,
    ),
    "exercise_history": (
        (
            "Every set of one exercise, by session, newest first, with carried_kg for "
            "bodyweight exercises. Use it for progress, records and what was done last time."
        ),
        History,
        _exercise_history,
    ),
    "body_weight": (
        "The owner's body weight in kg, or null if not set.",
        BodyWeight,
        _body_weight,
    ),
    "current_program": (
        (
            "The program being trained (active), with every gym day (days) and travel day "
            "(travel_days, empty if it has none), each with its blocks and exercises: "
            "the day's id, each exercise's id (its block exercise) and exercise_id, sets, "
            "rep_min-rep_max (seconds if timed), progression (load, or reps for "
            "bodyweight/band), rest_s; a block of more than one exercise is a superset. "
            "Gym and travel days rotate separately, so there are two next positions: next "
            "(the next gym day) and next_travel (the next travel day; null without travel "
            "days), each as {week, day} (day counts within its set; week 7 is the deload "
            "week, for both sets). A workout's program_week is the week of the set's next. "
            "Also any proposal still waiting for the owner (proposed: its name, days, "
            "travel_days and whether it is an edit). detail=full adds the program's and each "
            "exercise's notes, variations, start_load_kg and the whole proposal: read it "
            "with detail=full before proposing a change to a program."
        ),
        CurrentProgram,
        _current_program,
    ),
}


PROPOSE_DESCRIPTION = (
    "Propose a training program for the owner to accept in the app's Program screen. It "
    "replaces any earlier proposal and never changes the program being trained; accepting "
    "it starts a brand-new program at week 1 (to change the live program, use "
    "propose_program_edit instead). A program is six training weeks then a deload week; "
    "its days repeat every week in order. A day is blocks in order: one exercise, or two "
    "or three done as a superset. Use exercise ids from list_exercises. Give each exercise "
    "its sets and a rep range (seconds for a timed exercise). The app sets every load after "
    "the first session from the log, by double progression, so give start_load_kg only for "
    "the first session, from exercise_history. Optionally add travel_days: a second set of "
    "days (say three full-body band or bodyweight days) for when there is no gym. For "
    "those, set progression to reps on an exercise to progress by reps with no kg load, "
    "and put the harder variations in variations (e.g. 'push-up -> feet-elevated -> "
    "archer'). To refine a proposal, propose it again in full."
)

EDIT_DESCRIPTION = (
    "Propose an edit of the program being trained, for the owner to approve in the app's "
    "Program screen. Approving applies it as an edit, not a restart: the current week, the "
    "next gym day and next travel day, the logged history and every kept exercise's "
    "progression carry over, and the old version is kept for history. Read current_program "
    "with detail=full first, then send the whole program as it should be: days, "
    "travel_days, blocks and exercises left out are removed. Give an existing day's or "
    "exercise's id (from current_program) to keep it; leave id out to add a new one. A new "
    "exercise starts from the exercise's last logged load if there is one, else its "
    "start_load_kg. It replaces any earlier proposal and never changes the live program "
    "itself. Same fields as propose_program."
)


ADD_EXERCISE_DESCRIPTION = (
    "Add an exercise to the catalogue, so it can be logged and put in a program. Check "
    "list_exercises first and use an existing exercise when there is one. Equipment is part "
    "of an exercise's identity: a dumbbell and a barbell bench press are two exercises. "
    "It refuses a name that is already taken, and one that looks like an existing exercise, "
    "naming the matches; use a match's id, or, only if it really is a different exercise, "
    "call again with allow_similar true. Returns the new exercise with its id."
)


def tools(
    database: Path,
    programs_api: ProgramsApi | None = None,
    program_writer: Callable[[ProgramIn], object] | None = None,
    edit_writer: Callable[[ProgramEdit], object] | None = None,
    exercise_writer: Callable[[ExerciseIn], object] | None = None,
) -> dict[str, Tool]:
    """The tool set over the database at ``database``.

    Read tools open it read-only per call. ``propose_program``,
    ``propose_program_edit`` and ``add_exercise`` are offered when
    ``programs_api`` (or the matching writer: ``program_writer``,
    ``edit_writer``, ``exercise_writer``) is given; ``programs_api`` wins.
    """
    found = {
        name: Tool(name, description, model.model_json_schema(), _caller(database, model, answer))
        for name, (description, model, answer) in _TOOLS.items()
    }
    if programs_api is not None or program_writer is not None:
        writer = programs_api.propose if programs_api is not None else program_writer
        found["propose_program"] = Tool(
            "propose_program",
            PROPOSE_DESCRIPTION,
            inline_refs(ProgramIn.model_json_schema()),
            _proposer(ProgramIn, writer),
        )
    if programs_api is not None or edit_writer is not None:
        edit = programs_api.propose_edit if programs_api is not None else edit_writer
        found["propose_program_edit"] = Tool(
            "propose_program_edit",
            EDIT_DESCRIPTION,
            inline_refs(ProgramEdit.model_json_schema()),
            _proposer(ProgramEdit, edit),
        )
    if programs_api is not None or exercise_writer is not None:
        add = programs_api.add_exercise if programs_api is not None else exercise_writer
        found["add_exercise"] = Tool(
            "add_exercise",
            ADD_EXERCISE_DESCRIPTION,
            inline_refs(ExerciseIn.model_json_schema()),
            _adder(add),
        )
    return found


def duplicate_refusal(*, exact: bool, matches: Iterable[tuple[int, str, str | None]]) -> str:
    """Why an exercise was not added, naming each ``(id, name, equipment)`` it matched."""
    named = "; ".join(
        f"{name} (id {exercise_id}{'' if equipment is None else ', ' + equipment})"
        for exercise_id, name, equipment in matches
    )
    if exact:
        return f"the exercise was not added: the catalogue already has it as {named}"
    return (
        f"the exercise was not added: it looks like {named}. Use one of those ids, or call "
        "add_exercise again with allow_similar true if it really is a different exercise"
    )


def inline_refs(schema: Json) -> Json:
    """``schema``, which has ``$defs``, with each ``$ref`` replaced by its definition.

    Model providers differ in how much JSON Schema they take; a plain nested
    schema is the most widely understood.
    """
    definitions: Json = schema["$defs"]
    return {key: _resolve(value, definitions) for key, value in schema.items() if key != "$defs"}


def _resolve(node: object, definitions: Json) -> object:
    if isinstance(node, list):
        return [_resolve(item, definitions) for item in node]
    if not isinstance(node, dict):
        return node
    reference = node.get("$ref")
    if isinstance(reference, str):
        return _resolve(definitions[reference.removeprefix("#/$defs/")], definitions)
    return {key: _resolve(value, definitions) for key, value in node.items()}


def _proposer[Proposal: BaseModel](
    model: type[Proposal], writer: Callable[[Proposal], object] | None
) -> Callable[[Json], object]:
    def call(arguments: Json) -> object:
        try:
            program = model.model_validate(arguments)
        except ValidationError as error:
            raise ToolError(_describe(error)) from error
        if writer is None:
            message = "program proposals are not configured"
            raise ToolError(message)
        try:
            saved = writer(program)
        except ToolError:
            raise
        except Exception as error:
            message = f"the program was not saved: {error}"
            raise ToolError(message) from error
        return {
            "proposed": saved,
            "next": "The owner reviews it in the app's Program screen and accepts it there.",
        }

    return call


def _adder(writer: Callable[[ExerciseIn], object] | None) -> Callable[[Json], object]:
    def call(arguments: Json) -> object:
        try:
            exercise = ExerciseIn.model_validate(arguments)
        except ValidationError as error:
            raise ToolError(_describe(error)) from error
        if writer is None:
            message = "adding exercises is not configured"
            raise ToolError(message)
        return writer(exercise)

    return call


def _caller(
    database: Path,
    model: type[_Arguments],
    answer: Callable[[sqlite3.Connection, Any], object],
) -> Callable[[Json], object]:
    def call(arguments: Json) -> object:
        try:
            parsed = model.model_validate(arguments)
        except ValidationError as error:
            raise ToolError(_describe(error)) from error
        try:
            with closing(connect_readonly(database)) as conn:
                return answer(conn, parsed)
        except sqlite3.OperationalError as error:
            message = f"the training log cannot be read right now ({error})"
            raise ToolError(message) from error

    return call


def _describe(error: ValidationError) -> str:
    problems = "; ".join(
        # Each problem names where it is: "days.0.blocks.1.exercises.0.rep_max".
        f"{'.'.join(map(str, problem['loc']))}: {problem['msg']}"
        for problem in error.errors()
    )
    return f"invalid arguments: {problems}"

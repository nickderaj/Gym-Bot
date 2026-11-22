"""Travel days: a second set of days with its own rotation, calendar weeks and reps-only slots."""

import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from itertools import count
from typing import Any

import pytest
from pydantic import ValidationError

from trainer.domain.exercises import Measure
from trainer.domain.programs import DELOAD_RPE, Decision, Position, Progression
from trainer.domain.times import NaiveTimestampError
from trainer.services.journal import StaleProgramDayError, record_set, record_workout
from trainer.services.programs import (
    PlannedDay,
    PlannedExercise,
    ProgramIn,
    SlotIn,
    accept_proposal,
    programs,
    propose_program,
    today,
)
from trainer.storage.catalogue import ExerciseSpec, upsert_exercise
from trainer.storage.journal import ProgramLink, SetValues
from trainer.storage.programs import DayKind, Program, ProgramDay

# A Monday: ISO weeks start here, whatever the host's timezone.
MONDAY = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)


def at(weeks: int = 0, days: int = 0) -> datetime:
    """A time ``weeks`` and ``days`` after the first Monday."""
    return MONDAY + timedelta(weeks=weeks, days=days)


@pytest.fixture
def ids(db: sqlite3.Connection) -> dict[str, int]:
    catalogue = {
        "bench": ("barbell", Measure.REPS),
        "row": ("cable", Measure.REPS),
        "squat": ("barbell", Measure.REPS),
        "push-up": ("bodyweight", Measure.REPS),
        "band row": ("band", Measure.REPS),
    }
    found = {
        name: upsert_exercise(db, ExerciseSpec(name, name.title(), equipment, None, measure))
        for name, (equipment, measure) in catalogue.items()
    }
    db.commit()
    return found


def slot(
    exercise_id: int, sets: int = 3, reps: tuple[int, int] = (8, 10), **extra: object
) -> dict[str, Any]:
    return {
        "exercise_id": exercise_id,
        "sets": sets,
        "rep_min": reps[0],
        "rep_max": reps[1],
    } | extra


def day(name: str, *slots: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "blocks": [{"exercises": [one]} for one in slots]}


def hybrid(ids: dict[str, int]) -> ProgramIn:
    """Two gym days and three travel days."""
    return ProgramIn.model_validate(
        {
            "name": "Hybrid",
            "days": [
                day("Upper", slot(ids["bench"], start_load_kg=60.0), slot(ids["row"])),
                day("Lower", slot(ids["squat"], start_load_kg=80.0)),
            ],
            "travel_days": [
                day(
                    "Travel A",
                    slot(
                        ids["push-up"],
                        sets=4,
                        reps=(10, 15),
                        progression="reps",
                        variations="push-up -> feet-elevated -> archer",
                    ),
                    slot(ids["band row"], reps=(12, 20), progression="reps"),
                ),
                day("Travel B", slot(ids["push-up"], reps=(10, 15), progression="reps")),
                day("Travel C", slot(ids["band row"], reps=(12, 20), progression="reps")),
            ],
        }
    )


class Trainer:
    """Trains a program through the logging services, at times of the test's choosing."""

    def __init__(self, db: sqlite3.Connection, program: Program) -> None:
        """Train ``program``."""
        self.db = db
        self.program = program
        self.ids = count(1)

    def days(self, kind: DayKind) -> list[ProgramDay]:
        return self.program.days if kind is DayKind.GYM else self.program.travel_days

    def start(self, kind: DayKind, number: int, week: int, when: datetime) -> str:
        """Start (unfinished) day ``number`` of ``kind`` in program ``week``."""
        client_id = f"00000000-0000-4000-8000-{next(self.ids):012d}"
        link = ProgramLink(self.days(kind)[number - 1].id, week)
        record_workout(self.db, client_id, (when, None), None, link)
        return client_id

    def finish(self, client_id: str, kind: DayKind, number: int, week: int, when: datetime) -> None:
        link = ProgramLink(self.days(kind)[number - 1].id, week)
        record_workout(self.db, client_id, (when, when + timedelta(hours=1)), None, link)

    def train(
        self,
        kind: DayKind,
        number: int,
        week: int,
        when: datetime,
        sets: dict[str, list[tuple[int, float | None]]] | None = None,
    ) -> str:
        """Log a finished workout; ``sets`` maps an exercise name to its (reps, load) sets."""
        client_id = self.start(kind, number, week, when)
        planned = {
            exercise.name.lower(): exercise
            for block in self.days(kind)[number - 1].blocks
            for exercise in block.exercises
        }
        for name, logged in (sets or {}).items():
            for reps, load in logged:
                values = SetValues(reps, load, None, None, None)
                placement = (client_id, planned[name].exercise_id)
                record_set(
                    self.db, f"{client_id}-{next(self.ids)}", placement, values, planned[name].id
                )
        self.finish(client_id, kind, number, week, when)
        return client_id


@pytest.fixture
def trainer(db: sqlite3.Connection, ids: dict[str, int]) -> Trainer:
    return Trainer(db, accept_proposal(db, propose_program(db, hybrid(ids), MONDAY).id, MONDAY))


def named(plan: PlannedDay | None) -> dict[str, PlannedExercise]:
    assert plan is not None
    return {
        exercise.name.lower(): exercise for block in plan.blocks for exercise in block.exercises
    }


def positions(db: sqlite3.Connection, now: datetime) -> tuple[Position | None, Position | None]:
    found = programs(db, now)
    return found.next, found.next_travel


class TestShape:
    def test_travel_days_are_optional_and_a_second_set(self, ids: dict[str, int]) -> None:
        program = hybrid(ids)

        assert [d.name for d in program.days] == ["Upper", "Lower"]
        assert [d.name for d in program.travel_days] == ["Travel A", "Travel B", "Travel C"]
        without = ProgramIn.model_validate({"name": "Gym", "days": [day("Up", slot(1))]})
        assert without.travel_days == []

    def test_a_reps_slot_has_no_load(self) -> None:
        with pytest.raises(ValidationError, match="a reps progression has no load"):
            SlotIn.model_validate(slot(1, progression="reps", start_load_kg=5.0))

    def test_a_slot_progresses_by_load_unless_told_otherwise(self) -> None:
        assert SlotIn.model_validate(slot(1)).progression is Progression.LOAD

    def test_at_most_seven_travel_days(self, ids: dict[str, int]) -> None:
        eight = [day(f"T{n}", slot(ids["push-up"])) for n in range(8)]
        with pytest.raises(ValidationError, match="at most 7"):
            ProgramIn.model_validate(
                {"name": "x", "days": [day("Up", slot(1))], "travel_days": eight}
            )

    def test_saved_with_both_sets_in_order(self, trainer: Trainer) -> None:
        program = trainer.program

        assert [d.name for d in program.days] == ["Upper", "Lower"]
        assert [d.name for d in program.travel_days] == ["Travel A", "Travel B", "Travel C"]
        pushups = program.travel_days[0].blocks[0].exercises[0]
        assert pushups.progression is Progression.REPS
        assert pushups.variations == "push-up -> feet-elevated -> archer"
        assert pushups.start_load_kg is None


class TestSeparateRotations:
    def test_both_sets_start_at_their_first_day(self, trainer: Trainer) -> None:
        assert positions(trainer.db, MONDAY) == (Position(1, 1), Position(1, 1))
        plan = today(trainer.db, MONDAY)
        assert plan is not None
        assert plan.day is not None
        assert plan.travel_day is not None
        assert (plan.days, plan.travel_days) == (2, 3)
        assert (plan.day.name, plan.day.kind) == ("Upper", DayKind.GYM)
        assert (plan.travel_day.name, plan.travel_day.kind) == ("Travel A", DayKind.TRAVEL)

    def test_a_travel_session_does_not_advance_the_gym_rotation(self, trainer: Trainer) -> None:
        trainer.train(DayKind.TRAVEL, 1, 1, at(days=0))

        assert positions(trainer.db, at(days=1)) == (Position(1, 1), Position(1, 2))

    def test_a_gym_session_does_not_advance_the_travel_rotation(self, trainer: Trainer) -> None:
        trainer.train(DayKind.GYM, 1, 1, at(days=0))

        assert positions(trainer.db, at(days=1)) == (Position(1, 2), Position(1, 1))

    def test_each_set_follows_its_own_last_day(self, trainer: Trainer) -> None:
        trainer.train(DayKind.GYM, 1, 1, at(days=0))
        trainer.train(DayKind.TRAVEL, 1, 1, at(days=1))
        trainer.train(DayKind.TRAVEL, 2, 1, at(days=2))

        assert positions(trainer.db, at(days=3)) == (Position(1, 2), Position(1, 3))

    def test_the_travel_rotation_wraps_to_its_first_day(self, trainer: Trainer) -> None:
        for number in (1, 2, 3):
            trainer.train(DayKind.TRAVEL, number, 1, at(days=number - 1))

        # The wrap lands in the same calendar week, where Travel A is done: next week.
        assert positions(trainer.db, at(days=3)) == (Position(1, 1), Position(2, 1))

    def test_the_day_being_trained_is_that_sets_day(self, trainer: Trainer) -> None:
        trainer.train(DayKind.GYM, 1, 1, at(days=0))
        client_id = trainer.start(DayKind.TRAVEL, 1, 1, at(days=1))

        plan = today(trainer.db, at(days=1))

        assert plan is not None
        assert plan.workout_client_id == client_id
        assert plan.travel_day is not None
        assert (plan.travel_day.name, plan.travel_day.week) == ("Travel A", 1)
        assert plan.day is not None
        assert plan.day.name == "Lower"
        assert positions(trainer.db, at(days=1)) == (Position(1, 2), Position(1, 1))

    def test_the_workout_in_progress_does_not_count_as_last_time(self, trainer: Trainer) -> None:
        trainer.train(DayKind.TRAVEL, 1, 1, at(days=0), {"push-up": [(12, None)] * 4})
        client_id = trainer.start(DayKind.TRAVEL, 2, 1, at(days=1))
        pushups = trainer.program.travel_days[1].blocks[0].exercises[0]
        record_set(
            trainer.db,
            f"{client_id}-x",
            (client_id, pushups.exercise_id),
            SetValues(5, None, None, None, None),
            pushups.id,
        )

        plan = today(trainer.db, at(days=1))

        assert plan is not None
        assert plan.travel_day is not None
        last = named(plan.travel_day)["push-up"].last
        # The last session of the exercise is the earlier one: not the 5 reps being logged now.
        assert last is not None
        assert [s.reps for s in last.sets] == [12, 12, 12, 12]

    def test_the_set_being_trained_is_told(self, trainer: Trainer) -> None:
        plan = today(trainer.db, MONDAY)
        assert plan is not None
        assert (plan.workout_client_id, plan.training_kind) == (None, None)
        trainer.start(DayKind.TRAVEL, 1, 1, at(days=1))

        trained = today(trainer.db, at(days=1))

        assert trained is not None
        assert trained.training_kind is DayKind.TRAVEL
        assert trained.workout_client_id is not None

    def test_a_gym_workout_in_progress_leaves_travel_alone(self, trainer: Trainer) -> None:
        trainer.train(DayKind.TRAVEL, 1, 1, at(days=0))
        trainer.start(DayKind.GYM, 1, 1, at(days=1))

        assert positions(trainer.db, at(days=1)) == (Position(1, 1), Position(1, 2))

    def test_a_travel_day_is_trained_once_a_week(self, trainer: Trainer) -> None:
        trainer.train(DayKind.TRAVEL, 1, 1, at(days=0))

        with pytest.raises(StaleProgramDayError, match="already has a workout"):
            trainer.start(DayKind.TRAVEL, 1, 1, at(days=1))


class TestWeeksCountCalendarWeeks:
    def test_sessions_of_either_set_share_the_week(self, trainer: Trainer) -> None:
        trainer.train(DayKind.GYM, 1, 1, at(days=0))
        trainer.train(DayKind.TRAVEL, 1, 1, at(days=2))

        assert positions(trainer.db, at(days=4)) == (Position(1, 2), Position(1, 2))

    def test_the_next_calendar_week_is_the_next_program_week(self, trainer: Trainer) -> None:
        trainer.train(DayKind.GYM, 1, 1, at(days=0))
        trainer.train(DayKind.TRAVEL, 1, 1, at(days=2))

        assert positions(trainer.db, at(weeks=1)) == (Position(2, 2), Position(2, 2))

    def test_a_week_of_travel_only_still_counts(self, trainer: Trainer) -> None:
        trainer.train(DayKind.GYM, 1, 1, at(days=0))
        for week, number in ((2, 1), (3, 2)):
            trainer.train(DayKind.TRAVEL, number, week, at(weeks=week - 1))

        # Weeks 2 and 3 were travel only: the gym rotation did not move, the weeks did.
        assert positions(trainer.db, at(weeks=3)) == (Position(4, 2), Position(4, 3))

    def test_a_week_without_training_does_not_use_one_up(self, trainer: Trainer) -> None:
        trainer.train(DayKind.GYM, 1, 1, at(days=0))

        assert positions(trainer.db, at(weeks=3))[0] == Position(2, 2)

    def test_a_gym_rotation_finished_within_a_week_starts_the_next_program_week(
        self, trainer: Trainer
    ) -> None:
        trainer.train(DayKind.GYM, 1, 1, at(days=0))
        trainer.train(DayKind.GYM, 2, 1, at(days=1))

        # Upper is done in week 1: its next turn is week 2, though the calendar week is the same.
        assert positions(trainer.db, at(days=2)) == (Position(2, 1), Position(1, 1))
        trainer.train(DayKind.GYM, 1, 2, at(days=2))
        assert positions(trainer.db, at(days=3))[0] == Position(2, 2)

    def test_the_week_comes_from_the_calendar_not_the_stored_week_alone(
        self, trainer: Trainer
    ) -> None:
        trainer.train(DayKind.TRAVEL, 1, 1, at(days=6))  # a Sunday

        assert positions(trainer.db, at(days=6, weeks=0))[1] == Position(1, 2)
        assert positions(trainer.db, at(days=7))[1] == Position(2, 2)

    @pytest.mark.usefixtures("far_east_timezone")
    def test_the_clock_is_read_in_utc(self, trainer: Trainer) -> None:
        trainer.train(DayKind.TRAVEL, 1, 1, datetime(2026, 10, 11, 23, 30, tzinfo=UTC))
        # Monday 00:30 at UTC+1 is Sunday 23:30 in UTC: still the same calendar week.
        monday_in_london = datetime(2026, 10, 12, 0, 30, tzinfo=timezone(timedelta(hours=1)))

        assert positions(trainer.db, monday_in_london)[1] == Position(1, 2)
        assert positions(trainer.db, datetime(2026, 10, 12, 0, 30, tzinfo=UTC))[1] == Position(2, 2)


class TestDeloadAcrossBothSets:
    def reach_deload(self, trainer: Trainer) -> None:
        for week in range(1, 7):
            trainer.train(DayKind.GYM, 1 + (week + 1) % 2, week, at(weeks=week - 1))

    def test_week_seven_deloads_both_sets(self, trainer: Trainer) -> None:
        self.reach_deload(trainer)

        plan = today(trainer.db, at(weeks=6))

        assert plan is not None
        assert plan.day is not None
        assert plan.travel_day is not None
        assert (plan.day.week, plan.day.deload, plan.day.deload_rpe) == (7, True, DELOAD_RPE)
        assert (plan.travel_day.week, plan.travel_day.deload) == (7, True)
        assert plan.travel_day.deload_rpe == DELOAD_RPE

    def test_about_half_the_sets_and_no_kg_for_reps_slots(self, trainer: Trainer) -> None:
        self.reach_deload(trainer)
        plan = today(trainer.db, at(weeks=6))
        assert plan is not None

        pushups = named(plan.travel_day)["push-up"]
        band = named(plan.travel_day)["band row"]
        assert (pushups.target.decision, pushups.target.load_kg) == (Decision.DELOAD, None)
        assert pushups.target.reps == (10, 10)  # 4 sets -> 2
        assert band.target.reps == (12, 12)  # 3 sets -> 2
        squat_or_bench = next(iter(named(plan.day).values()))
        assert squat_or_bench.target.decision is Decision.DELOAD

    def test_a_normal_week_has_no_deload_effort(self, trainer: Trainer) -> None:
        plan = today(trainer.db, MONDAY)

        assert plan is not None
        assert plan.day is not None
        assert plan.day.deload_rpe is None
        assert plan.day.deload is False

    def test_the_block_is_done_after_the_deload_week(self, trainer: Trainer) -> None:
        self.reach_deload(trainer)
        trainer.train(DayKind.TRAVEL, 1, 7, at(weeks=6))

        assert positions(trainer.db, at(weeks=6, days=1))[1] == Position(7, 2)
        assert positions(trainer.db, at(weeks=7)) == (None, None)
        plan = today(trainer.db, at(weeks=7))
        assert plan is not None
        assert (plan.day, plan.travel_day) == (None, None)


class TestTheClockIsAware:
    def test_a_time_without_a_timezone_is_refused(self, trainer: Trainer) -> None:
        naive = datetime(2026, 10, 5, 9, 0)  # noqa: DTZ001  # why: the point of the test

        with pytest.raises(NaiveTimestampError, match="has no timezone"):
            programs(trainer.db, naive)
        with pytest.raises(NaiveTimestampError, match="has no timezone"):
            today(trainer.db, naive)


class TestRepsOnlySlots:
    def test_the_first_session_aims_for_the_bottom_with_no_load(self, trainer: Trainer) -> None:
        plan = today(trainer.db, MONDAY)
        assert plan is not None

        pushups = named(plan.travel_day)["push-up"]

        assert pushups.target.decision is Decision.START
        assert pushups.target.load_kg is None
        assert pushups.target.reps == (10, 10, 10, 10)
        assert pushups.progression is Progression.REPS
        assert pushups.variations == "push-up -> feet-elevated -> archer"

    def test_reps_climb_within_the_range(self, trainer: Trainer) -> None:
        trainer.train(
            DayKind.TRAVEL, 1, 1, at(days=0), {"push-up": [(12, None), (11, None), (10, None)] * 1}
        )
        trainer.train(DayKind.TRAVEL, 2, 1, at(days=1))
        trainer.train(DayKind.TRAVEL, 3, 1, at(days=2))

        plan = today(trainer.db, at(weeks=1))
        assert plan is not None
        pushups = named(plan.travel_day)["push-up"]

        assert (pushups.target.decision, pushups.target.load_kg) == (Decision.REPEAT, None)
        assert pushups.target.reps == (12, 11, 10, 10)

    def test_the_top_of_the_range_on_every_set_moves_to_the_next_variation(
        self, trainer: Trainer
    ) -> None:
        trainer.train(DayKind.TRAVEL, 1, 1, at(days=0), {"push-up": [(15, None)] * 4})
        trainer.train(DayKind.TRAVEL, 2, 1, at(days=1))
        trainer.train(DayKind.TRAVEL, 3, 1, at(days=2))

        plan = today(trainer.db, at(weeks=1))
        assert plan is not None
        pushups = named(plan.travel_day)["push-up"]

        assert (pushups.target.decision, pushups.target.load_kg) == (Decision.ADVANCE, None)
        assert pushups.target.reps == (10, 10, 10, 10)

    def test_a_load_typed_in_is_never_suggested_back(self, trainer: Trainer) -> None:
        trainer.train(DayKind.TRAVEL, 1, 1, at(days=0), {"band row": [(20, 12.5)] * 3})
        trainer.train(DayKind.TRAVEL, 2, 1, at(days=1))
        trainer.train(DayKind.TRAVEL, 3, 1, at(days=2))

        plan = today(trainer.db, at(weeks=1))
        assert plan is not None
        band = named(plan.travel_day)["band row"]

        assert band.target.load_kg is None

    def test_gym_slots_keep_double_progression_by_kg(self, trainer: Trainer) -> None:
        trainer.train(DayKind.GYM, 1, 1, at(days=0), {"bench": [(10, 60.0)] * 3})
        trainer.train(DayKind.GYM, 2, 1, at(days=1))

        plan = today(trainer.db, at(weeks=1))
        assert plan is not None
        bench = named(plan.day)["bench"]

        assert (bench.target.decision, bench.target.load_kg) == (Decision.PROGRESS, 62.5)
        assert bench.progression is Progression.LOAD


class TestWithoutTravelDays:
    @pytest.fixture
    def plain(self, db: sqlite3.Connection, ids: dict[str, int]) -> Trainer:
        program = ProgramIn.model_validate(
            {
                "name": "Gym only",
                "days": [day("Upper", slot(ids["bench"])), day("Lower", slot(ids["squat"]))],
            }
        )
        return Trainer(db, accept_proposal(db, propose_program(db, program, MONDAY).id, MONDAY))

    def test_there_is_no_travel_day(self, plain: Trainer) -> None:
        plan = today(plain.db, MONDAY)

        assert plan is not None
        assert (plan.travel_days, plan.travel_day) == (0, None)
        assert positions(plain.db, MONDAY) == (Position(1, 1), None)
        assert plain.program.travel_days == []

    def test_weeks_follow_the_rotation_not_the_calendar(self, plain: Trainer) -> None:
        plain.train(DayKind.GYM, 1, 1, at(days=0))
        plain.train(DayKind.GYM, 2, 1, at(days=1))

        # Same calendar week, but the rotation finished: week 2. A month later still week 2.
        assert positions(plain.db, at(days=2))[0] == Position(2, 1)
        assert positions(plain.db, at(weeks=5))[0] == Position(2, 1)

    def test_a_missed_week_is_just_the_next_session(self, plain: Trainer) -> None:
        plain.train(DayKind.GYM, 1, 1, at(days=0))

        assert positions(plain.db, at(weeks=4))[0] == Position(1, 2)

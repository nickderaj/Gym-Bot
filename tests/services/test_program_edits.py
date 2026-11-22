"""Editing a live program, its version history, and restarting it."""

import sqlite3
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

import pytest

from trainer.domain.exercises import Measure
from trainer.domain.programs import Decision, Position
from trainer.services.journal import record_set, record_workout
from trainer.services.programs import (
    NoActiveProgramError,
    NoProposalError,
    PlannedExercise,
    ProgramEdit,
    ProgramError,
    ProgramIn,
    ProgramInUseError,
    accept_proposal,
    decline_proposal,
    edit_program,
    programs,
    propose_edit,
    propose_program,
    restart_program,
    today,
    versions,
)
from trainer.storage.catalogue import ExerciseSpec, upsert_exercise
from trainer.storage.history import get_workout
from trainer.storage.journal import ProgramLink, SetValues
from trainer.storage.programs import (
    Change,
    DayKind,
    Program,
    ProgramDay,
    ProgramStatus,
    Start,
    get_program,
    list_versions,
)

MONDAY = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
LATER = MONDAY + timedelta(days=30)


def at(weeks: int = 0, days: int = 0) -> datetime:
    return MONDAY + timedelta(weeks=weeks, days=days)


@pytest.fixture
def ids(db: sqlite3.Connection) -> dict[str, int]:
    catalogue = {
        "bench": ("barbell", Measure.REPS),
        "row": ("cable", Measure.REPS),
        "squat": ("barbell", Measure.REPS),
        "curl": ("dumbbell", Measure.REPS),
        "push-up": ("bodyweight", Measure.REPS),
        "run": ("other", Measure.DISTANCE),
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


def gym_only(ids: dict[str, int]) -> ProgramIn:
    body = program_body(ids)
    body["travel_days"] = []
    return ProgramIn.model_validate(body)


def program_body(ids: dict[str, int]) -> dict[str, Any]:
    return {
        "name": "Four days",
        "notes": "Program notes.",
        "days": [
            day("Upper A", slot(ids["bench"], start_load_kg=60.0)),
            day("Lower A", slot(ids["squat"], start_load_kg=80.0, notes="warm up")),
            day("Upper B", slot(ids["row"], start_load_kg=40.0)),
        ],
        "travel_days": [
            day("Travel A", slot(ids["push-up"], progression="reps", variations="a -> b"))
        ],
    }


class Trainer:
    """Trains the active program, whichever version it is, through the logging services."""

    def __init__(self, db: sqlite3.Connection) -> None:
        """Train from whatever program is active."""
        self.db = db
        self.ids = count(1)

    @property
    def program(self) -> Program:
        active = programs(self.db, LATER).active
        assert active is not None
        return active

    def start(self, kind: DayKind, number: int, week: int, when: datetime) -> str:
        days = self.program.days if kind is DayKind.GYM else self.program.travel_days
        client_id = f"00000000-0000-4000-8000-{next(self.ids):012d}"
        record_workout(
            self.db, client_id, (when, None), None, ProgramLink(days[number - 1].id, week)
        )
        return client_id

    def train(
        self,
        kind: DayKind,
        number: int,
        week: int,
        when: datetime,
        sets: list[tuple[int, float | None]] | None = None,
    ) -> str:
        """Log a finished workout; ``sets`` are the (reps, load) of its first exercise."""
        days = self.program.days if kind is DayKind.GYM else self.program.travel_days
        client_id = self.start(kind, number, week, when)
        planned = days[number - 1].blocks[0].exercises[0]
        for reps, load in sets or []:
            values = SetValues(reps, load, None, None, None)
            placement = (client_id, planned.exercise_id)
            record_set(self.db, f"{client_id}-{next(self.ids)}", placement, values, planned.id)
        link = ProgramLink(days[number - 1].id, week)
        record_workout(self.db, client_id, (when, when + timedelta(hours=1)), None, link)
        return client_id

    def free_session(self, exercise_id: int, when: datetime, load: float) -> None:
        """A workout outside the program: three sets of 8 at ``load``."""
        client_id = f"11111111-0000-4000-8000-{next(self.ids):012d}"
        record_workout(self.db, client_id, (when, when + timedelta(hours=1)), None)
        for _ in range(3):
            values = SetValues(8, load, None, None, None)
            record_set(self.db, f"{client_id}-{next(self.ids)}", (client_id, exercise_id), values)


@pytest.fixture
def trainer(db: sqlite3.Connection, ids: dict[str, int]) -> Trainer:
    program = ProgramIn.model_validate(program_body(ids))
    accept_proposal(db, propose_program(db, program, MONDAY).id, MONDAY)
    return Trainer(db)


def as_edit(program: Program, **changes: object) -> dict[str, Any]:
    """The program as an edit body that changes nothing, ids included, then ``changes``."""

    def of_day(source: ProgramDay) -> dict[str, Any]:
        return {
            "id": source.id,
            "name": source.name,
            "blocks": [
                {
                    "rest_s": block.rest_s,
                    "exercises": [
                        {
                            "id": exercise.id,
                            "exercise_id": exercise.exercise_id,
                            "sets": exercise.sets,
                            "rep_min": exercise.rep_min,
                            "rep_max": exercise.rep_max,
                            "start_load_kg": exercise.start_load_kg,
                            "notes": exercise.notes,
                            "progression": exercise.progression,
                            "variations": exercise.variations,
                        }
                        for exercise in block.exercises
                    ],
                }
                for block in source.blocks
            ],
        }

    return {
        "name": program.name,
        "notes": program.notes,
        "days": [of_day(source) for source in program.days],
        "travel_days": [of_day(source) for source in program.travel_days],
    } | changes


def planned_gym(db: sqlite3.Connection, now: datetime) -> dict[str, PlannedExercise]:
    plan = today(db, now)
    assert plan is not None
    assert plan.day is not None
    return {
        exercise.name.lower(): exercise for block in plan.day.blocks for exercise in block.exercises
    }


class TestAnEditKeepsPlaceAndProgression:
    def test_an_edit_proposal_leaves_the_live_program_alone_until_approved(
        self, trainer: Trainer
    ) -> None:
        live = trainer.program
        edit = ProgramEdit.model_validate(as_edit(live, name="Renamed"))

        proposed = propose_edit(trainer.db, edit, at(days=1))

        assert proposed.status is ProgramStatus.PROPOSED
        assert (proposed.change, proposed.previous_id) == (Change.EDIT, live.id)
        found = programs(trainer.db, at(days=1))
        assert found.active is not None
        assert (found.active.id, found.active.name) == (live.id, "Four days")
        assert found.proposed is not None
        assert found.proposed.id == proposed.id

    def test_approving_applies_it_as_an_edit_not_a_restart(self, trainer: Trainer) -> None:
        live = trainer.program
        trainer.train(DayKind.GYM, 1, 1, at(days=0), [(10, 60.0)] * 3)
        edit = ProgramEdit.model_validate(as_edit(live, name="Renamed"))
        proposed = propose_edit(trainer.db, edit, at(days=1))

        accepted = accept_proposal(trainer.db, proposed.id, at(days=2))

        assert accepted.status is ProgramStatus.ACTIVE
        assert (accepted.lineage_id, accepted.version, accepted.run) == (live.lineage_id, 2, 1)
        assert (accepted.change, accepted.previous_id) == (Change.EDIT, live.id)
        assert accepted.started_at == live.started_at  # the program's own start date
        assert get_program(trainer.db, live.id).status is ProgramStatus.ARCHIVED
        assert programs(trainer.db, at(days=2)).next == Position(1, 2)

    def test_an_approved_proposal_keeps_progression_like_a_direct_edit(
        self, trainer: Trainer
    ) -> None:
        trainer.train(DayKind.GYM, 1, 1, at(days=0), [(10, 60.0)] * 3)
        body = as_edit(trainer.program)
        body["days"][0]["blocks"][0]["exercises"][0]["notes"] = "pause"
        proposed = propose_edit(trainer.db, ProgramEdit.model_validate(body), at(days=1))

        accept_proposal(trainer.db, proposed.id, at(days=2))

        trainer.train(DayKind.GYM, 2, 1, at(days=3))
        trainer.train(DayKind.GYM, 3, 1, at(days=4))
        bench = planned_gym(trainer.db, at(weeks=1))["bench"]
        assert (bench.target.decision, bench.target.load_kg) == (Decision.PROGRESS, 62.5)
        assert bench.notes == "pause"

    def test_the_current_week_and_next_days_are_kept(self, trainer: Trainer) -> None:
        trainer.train(DayKind.GYM, 1, 1, at(days=0))
        trainer.train(DayKind.GYM, 2, 1, at(days=1))
        trainer.train(DayKind.TRAVEL, 1, 1, at(days=2))
        before = programs(trainer.db, at(days=3))

        edit_program(trainer.db, ProgramEdit.model_validate(as_edit(trainer.program)), at(days=3))

        after = programs(trainer.db, at(days=3))
        assert (after.next, after.next_travel) == (before.next, before.next_travel)
        assert (after.next, after.next_travel) == (Position(1, 3), Position(2, 1))

    def test_the_next_day_stays_the_same_day_when_days_are_reordered(
        self, trainer: Trainer
    ) -> None:
        trainer.train(DayKind.GYM, 1, 1, at(days=0))  # next: Lower A
        live = trainer.program
        body = as_edit(live)
        body["days"] = [body["days"][2], body["days"][1], body["days"][0]]

        edited = edit_program(trainer.db, ProgramEdit.model_validate(body), at(days=1))

        assert [d.name for d in edited.days] == ["Upper B", "Lower A", "Upper A"]
        plan = today(trainer.db, at(days=1))
        assert plan is not None
        assert plan.day is not None
        assert (plan.day.name, plan.day.position, plan.day.week) == ("Lower A", 2, 1)

    def test_the_next_travel_day_stays_the_same_day_when_travel_days_are_reordered(
        self, trainer: Trainer, ids: dict[str, int]
    ) -> None:
        body = as_edit(trainer.program)
        body["travel_days"].append(day("Travel B", slot(ids["push-up"], progression="reps")))
        body["travel_days"].append(day("Travel C", slot(ids["push-up"], progression="reps")))
        added = edit_program(trainer.db, ProgramEdit.model_validate(body), at(days=0))
        trainer.train(DayKind.TRAVEL, 1, 1, at(days=1))  # next travel: Travel B
        reorder = as_edit(added)
        reorder["travel_days"] = [reorder["travel_days"][i] for i in (2, 1, 0)]

        edited = edit_program(trainer.db, ProgramEdit.model_validate(reorder), at(days=2))

        assert [d.name for d in edited.travel_days] == ["Travel C", "Travel B", "Travel A"]
        assert edited.start.travel_day == 2
        plan = today(trainer.db, at(days=2))
        assert plan is not None
        assert plan.travel_day is not None
        assert (plan.travel_day.name, plan.travel_day.position) == ("Travel B", 2)

    def test_a_removed_next_day_hands_over_to_the_day_in_its_place(self, trainer: Trainer) -> None:
        trainer.train(DayKind.GYM, 1, 1, at(days=0))  # next: Lower A (day 2)
        body = as_edit(trainer.program)
        del body["days"][1]

        edit_program(trainer.db, ProgramEdit.model_validate(body), at(days=1))

        plan = today(trainer.db, at(days=1))
        assert plan is not None
        assert plan.day is not None
        assert (plan.day.name, plan.day.position) == ("Upper B", 2)

    def test_a_removed_last_next_day_falls_back_to_the_last_day(self, trainer: Trainer) -> None:
        trainer.train(DayKind.GYM, 1, 1, at(days=0))
        trainer.train(DayKind.GYM, 2, 1, at(days=1))  # next: Upper B (day 3)
        body = as_edit(trainer.program)
        del body["days"][2]

        edit_program(trainer.db, ProgramEdit.model_validate(body), at(days=2))

        plan = today(trainer.db, at(days=2))
        assert plan is not None
        assert plan.day is not None
        assert (plan.day.name, plan.day.position) == ("Lower A", 2)

    def test_days_added_before_the_next_one_do_not_move_it(
        self, trainer: Trainer, ids: dict[str, int]
    ) -> None:
        trainer.train(DayKind.GYM, 1, 1, at(days=0))  # next: Lower A
        body = as_edit(trainer.program)
        body["days"].insert(0, day("Warm-up", slot(ids["curl"])))

        edit_program(trainer.db, ProgramEdit.model_validate(body), at(days=1))

        plan = today(trainer.db, at(days=1))
        assert plan is not None
        assert plan.day is not None
        assert (plan.day.name, plan.day.position) == ("Lower A", 3)

    def test_a_kept_slot_keeps_its_progression_and_next_load(self, trainer: Trainer) -> None:
        trainer.train(DayKind.GYM, 1, 1, at(days=0), [(10, 60.0)] * 3)
        before = planned_gym(trainer.db, at(days=1))  # Lower A: squat still new
        trainer.train(DayKind.GYM, 2, 1, at(days=1))
        trainer.train(DayKind.GYM, 3, 1, at(days=2))  # back to Upper A in week 2
        target = planned_gym(trainer.db, at(weeks=1))["bench"].target
        assert (target.decision, target.load_kg) == (Decision.PROGRESS, 62.5)

        body = as_edit(trainer.program)
        body["days"][0]["blocks"][0]["exercises"][0]["notes"] = "pause"
        edited = edit_program(trainer.db, ProgramEdit.model_validate(body), at(weeks=1))

        assert before["squat"].target.decision is Decision.START
        kept = planned_gym(trainer.db, at(weeks=1))["bench"]
        assert (kept.target.decision, kept.target.load_kg) == (Decision.PROGRESS, 62.5)
        assert kept.notes == "pause"
        assert kept.block_exercise_id == edited.days[0].blocks[0].exercises[0].id
        assert (
            kept.block_exercise_id
            != trainer.db.execute("SELECT min(id) FROM block_exercises").fetchone()[0]
        )

    def test_sets_and_rep_ranges_can_change_while_the_history_is_kept(
        self, trainer: Trainer
    ) -> None:
        trainer.train(DayKind.GYM, 1, 1, at(days=0), [(10, 60.0)] * 3)
        trainer.train(DayKind.GYM, 2, 1, at(days=1))
        trainer.train(DayKind.GYM, 3, 1, at(days=2))
        body = as_edit(trainer.program)
        bench = body["days"][0]["blocks"][0]
        bench["rest_s"] = 150
        bench["exercises"][0] |= {"sets": 4, "rep_min": 5, "rep_max": 6}

        edited = edit_program(trainer.db, ProgramEdit.model_validate(body), at(weeks=1))

        assert edited.days[0].blocks[0].rest_s == 150
        planned = planned_gym(trainer.db, at(weeks=1))["bench"]
        # Three sets of 10 at 60 kg clear a 5-6 range, but fall short of 4 sets: the load repeats.
        assert (planned.sets, planned.rep_min, planned.rep_max) == (4, 5, 6)
        assert (planned.target.decision, planned.target.load_kg) == (Decision.REPEAT, 60.0)
        assert planned.target.reps == (6, 6, 6, 5)

    def test_travel_days_can_be_added_and_removed(
        self, trainer: Trainer, ids: dict[str, int]
    ) -> None:
        trainer.train(DayKind.GYM, 1, 1, at(days=0))
        body = as_edit(trainer.program)
        body["travel_days"].append(day("Travel B", slot(ids["push-up"], progression="reps")))
        added = edit_program(trainer.db, ProgramEdit.model_validate(body), at(days=1))
        assert [d.name for d in added.travel_days] == ["Travel A", "Travel B"]

        removed = edit_program(
            trainer.db,
            ProgramEdit.model_validate(as_edit(added, travel_days=[])),
            at(days=2),
        )

        assert removed.travel_days == []
        found = programs(trainer.db, at(days=2))
        assert (found.next, found.next_travel) == (Position(1, 2), None)

    def test_adding_travel_days_to_a_gym_only_program_keeps_the_place(
        self, db: sqlite3.Connection, ids: dict[str, int]
    ) -> None:
        body = program_body(ids)
        body["travel_days"] = []
        accept_proposal(db, propose_program(db, ProgramIn.model_validate(body), MONDAY).id, MONDAY)
        trainer = Trainer(db)
        trainer.train(DayKind.GYM, 1, 1, at(days=0))
        trainer.train(DayKind.GYM, 2, 1, at(days=1))
        edit = as_edit(trainer.program, travel_days=[day("Travel A", slot(ids["push-up"]))])

        edit_program(db, ProgramEdit.model_validate(edit), at(days=2))

        found = programs(db, at(days=2))
        assert (found.next, found.next_travel) == (Position(1, 3), Position(1, 1))

    def test_a_finished_block_stays_finished(self, trainer: Trainer) -> None:
        for week in range(1, 8):
            for number in (1, 2, 3):
                trainer.train(DayKind.GYM, number, week, at(weeks=week - 1, days=number - 1))
        assert programs(trainer.db, at(weeks=7)).next is None

        edit_program(trainer.db, ProgramEdit.model_validate(as_edit(trainer.program)), at(weeks=7))

        assert programs(trainer.db, at(weeks=7)).next is None
        assert programs(trainer.db, at(weeks=7)).next_travel is None

    @pytest.mark.parametrize(
        ("weeks_done", "expected_week"),
        [(5, 6), (6, 7)],  # an edit in the last training week, and in the deload week
    )
    def test_an_edit_in_the_last_weeks_keeps_the_week(
        self, db: sqlite3.Connection, ids: dict[str, int], weeks_done: int, expected_week: int
    ) -> None:
        accept_proposal(db, propose_program(db, gym_only(ids), MONDAY).id, MONDAY)
        trainer = Trainer(db)
        for week in range(1, weeks_done + 1):
            for number in (1, 2, 3):
                trainer.train(DayKind.GYM, number, week, at(weeks=week - 1, days=number - 1))

        edited = edit_program(
            db, ProgramEdit.model_validate(as_edit(trainer.program)), at(weeks=weeks_done)
        )

        assert edited.start == Start(expected_week, 1, 1)
        assert programs(db, at(weeks=weeks_done)).next == Position(expected_week, 1)

    def test_an_edit_after_the_block_keeps_it_finished_without_travel_days(
        self, db: sqlite3.Connection, ids: dict[str, int]
    ) -> None:
        accept_proposal(db, propose_program(db, gym_only(ids), MONDAY).id, MONDAY)
        trainer = Trainer(db)
        for week in range(1, 8):
            for number in (1, 2, 3):
                trainer.train(DayKind.GYM, number, week, at(weeks=week - 1, days=number - 1))
        assert programs(db, at(weeks=7)).next is None

        edited = edit_program(db, ProgramEdit.model_validate(as_edit(trainer.program)), at(weeks=7))

        assert edited.start.week == 8  # past the deload week
        assert programs(db, at(weeks=7)).next is None
        assert today(db, at(weeks=7)) is not None

    def test_the_week_is_kept_without_travel_days_too(
        self, db: sqlite3.Connection, ids: dict[str, int]
    ) -> None:
        body = program_body(ids)
        body["travel_days"] = []
        accept_proposal(db, propose_program(db, ProgramIn.model_validate(body), MONDAY).id, MONDAY)
        trainer = Trainer(db)
        for number in (1, 2, 3):
            trainer.train(DayKind.GYM, number, 1, at(days=number - 1))  # week 1 done

        edit_program(db, ProgramEdit.model_validate(as_edit(trainer.program)), at(days=3))

        assert programs(db, at(days=3)).next == Position(2, 1)
        trainer.train(DayKind.GYM, 1, 2, at(days=4))
        assert programs(db, at(days=5)).next == Position(2, 2)


class TestANewSlotStartsFromTheLog:
    def new_curl(
        self, trainer: Trainer, ids: dict[str, int], start_load_kg: float | None = None
    ) -> dict[str, Any]:
        body = as_edit(trainer.program)
        body["days"][0]["blocks"].append(
            {"exercises": [slot(ids["curl"], start_load_kg=start_load_kg)]}
        )
        return body

    def test_from_start_load_kg_when_the_exercise_was_never_logged(
        self, trainer: Trainer, ids: dict[str, int]
    ) -> None:
        body = self.new_curl(trainer, ids, start_load_kg=10.0)

        edit_program(trainer.db, ProgramEdit.model_validate(body), at(days=1))

        trainer.train(DayKind.GYM, 1, 1, at(days=1))
        trainer.train(DayKind.GYM, 2, 1, at(days=2))
        trainer.train(DayKind.GYM, 3, 1, at(days=3))
        curl = planned_gym(trainer.db, at(weeks=1))["curl"]
        assert (curl.target.decision, curl.target.load_kg) == (Decision.START, 10.0)
        assert curl.target.reps == (8, 8, 8)

    def test_from_the_exercises_last_logged_load_if_there_is_one(
        self, trainer: Trainer, ids: dict[str, int]
    ) -> None:
        trainer.free_session(ids["curl"], at(days=-3), 12.0)
        trainer.free_session(ids["curl"], at(days=-1), 14.0)
        body = self.new_curl(trainer, ids, start_load_kg=10.0)

        edit_program(trainer.db, ProgramEdit.model_validate(body), at(days=1))

        curl = planned_gym(trainer.db, at(days=1))["curl"]
        assert (curl.target.decision, curl.target.load_kg) == (Decision.START, 14.0)

    def test_without_either_the_owner_chooses(self, trainer: Trainer, ids: dict[str, int]) -> None:
        edit_program(
            trainer.db, ProgramEdit.model_validate(self.new_curl(trainer, ids)), at(days=1)
        )

        curl = planned_gym(trainer.db, at(days=1))["curl"]

        assert (curl.target.decision, curl.target.load_kg) == (Decision.START, None)

    def test_once_trained_it_progresses_from_its_own_history(
        self, trainer: Trainer, ids: dict[str, int]
    ) -> None:
        trainer.free_session(ids["curl"], at(days=-1), 14.0)
        edit_program(
            trainer.db,
            ProgramEdit.model_validate(self.new_curl(trainer, ids, start_load_kg=10.0)),
            at(days=0),
        )
        client_id = trainer.start(DayKind.GYM, 1, 1, at(days=1))
        curl = trainer.program.days[0].blocks[1].exercises[0]
        for _ in range(3):
            values = SetValues(10, 16.0, None, None, None)
            record_set(
                trainer.db,
                f"{client_id}-{next(trainer.ids)}",
                (client_id, curl.exercise_id),
                values,
                curl.id,
            )
        record_workout(
            trainer.db,
            client_id,
            (
                at(days=1),
                at(
                    days=1,
                ).replace(hour=10),
            ),
            None,
            ProgramLink(trainer.program.days[0].id, 1),
        )
        trainer.train(DayKind.GYM, 2, 1, at(days=2))
        trainer.train(DayKind.GYM, 3, 1, at(days=3))

        target = planned_gym(trainer.db, at(weeks=1))["curl"].target

        assert (target.decision, target.load_kg) == (Decision.PROGRESS, 18.5)

    def test_a_brand_new_program_still_starts_from_its_own_start_loads(
        self, db: sqlite3.Connection, ids: dict[str, int]
    ) -> None:
        # No edit: the proposer chose these loads, so the log never overrides them.
        Trainer(db).free_session(ids["bench"], at(days=-1), 100.0)
        body = program_body(ids)
        accept_proposal(db, propose_program(db, ProgramIn.model_validate(body), MONDAY).id, MONDAY)

        assert planned_gym(db, MONDAY)["bench"].target.load_kg == 60.0

    def test_a_kept_slot_with_no_history_keeps_its_start_load(self, trainer: Trainer) -> None:
        edit_program(trainer.db, ProgramEdit.model_validate(as_edit(trainer.program)), at(days=1))

        assert planned_gym(trainer.db, at(days=1))["bench"].target.load_kg == 60.0


class TestEditChecks:
    def test_there_must_be_an_active_program(
        self, db: sqlite3.Connection, ids: dict[str, int]
    ) -> None:
        body = ProgramEdit.model_validate({"name": "x", "days": [day("A", slot(ids["bench"]))]})

        with pytest.raises(NoActiveProgramError, match="no program being trained"):
            propose_edit(db, body, MONDAY)
        with pytest.raises(NoActiveProgramError):
            edit_program(db, body, MONDAY)
        with pytest.raises(NoActiveProgramError):
            restart_program(db, MONDAY)
        assert not db.in_transaction

    def test_unknown_ids_are_refused(self, trainer: Trainer) -> None:
        body = as_edit(trainer.program)
        body["days"][0]["blocks"][0]["exercises"][0]["id"] = 9999
        with pytest.raises(ProgramError, match="no exercise with id 9999"):
            edit_program(trainer.db, ProgramEdit.model_validate(body), at(days=1))
        body = as_edit(trainer.program)
        body["days"][0]["id"] = 9999
        with pytest.raises(ProgramError, match="no day with id 9999"):
            propose_edit(trainer.db, ProgramEdit.model_validate(body), at(days=1))
        assert not trainer.db.in_transaction

    def test_every_unknown_id_is_named(self, trainer: Trainer) -> None:
        body = as_edit(trainer.program)
        body["days"][0]["id"] = 9998
        body["days"][1]["id"] = 9999

        with pytest.raises(ProgramError, match=r"no day with id 9998, 9999$"):
            edit_program(trainer.db, ProgramEdit.model_validate(body), at(days=1))

    def test_every_repeated_id_is_named(self, trainer: Trainer) -> None:
        body = as_edit(trainer.program)
        slots = [d["blocks"][0]["exercises"][0] for d in [*body["days"], *body["travel_days"]]]
        first, _second, third, fourth = slots
        first_id, third_id = first["id"], third["id"]
        slots[1]["id"] = first_id
        fourth["id"] = third_id
        message = rf"^exercise id {first_id}, {third_id} is used more than once$"

        with pytest.raises(ProgramError, match=message):
            edit_program(trainer.db, ProgramEdit.model_validate(body), at(days=1))

    def test_an_id_is_used_once(self, trainer: Trainer) -> None:
        body = as_edit(trainer.program)
        body["days"][1]["blocks"][0]["exercises"][0]["id"] = body["days"][0]["blocks"][0][
            "exercises"
        ][0]["id"]

        with pytest.raises(ProgramError, match="exercise id 1 is used more than once"):
            edit_program(trainer.db, ProgramEdit.model_validate(body), at(days=1))

    def test_a_day_id_is_used_once(self, trainer: Trainer) -> None:
        body = as_edit(trainer.program)
        body["days"][1]["id"] = body["days"][0]["id"]

        with pytest.raises(ProgramError, match="day id 1 is used more than once"):
            edit_program(trainer.db, ProgramEdit.model_validate(body), at(days=1))

    def test_exercises_must_exist_and_not_be_distance(
        self, trainer: Trainer, ids: dict[str, int]
    ) -> None:
        body = as_edit(trainer.program)
        body["days"][0]["blocks"].append({"exercises": [slot(ids["run"])]})
        with pytest.raises(ProgramError, match="measured in distance"):
            edit_program(trainer.db, ProgramEdit.model_validate(body), at(days=1))
        body = as_edit(trainer.program)
        body["travel_days"][0]["blocks"].append({"exercises": [slot(99999)]})
        with pytest.raises(ProgramError, match="no exercise has id 99999"):
            propose_edit(trainer.db, ProgramEdit.model_validate(body), at(days=1))

    def test_a_workout_in_progress_blocks_an_edit_and_a_restart(self, trainer: Trainer) -> None:
        trainer.start(DayKind.TRAVEL, 1, 1, at(days=0))
        edit = ProgramEdit.model_validate(as_edit(trainer.program))

        with pytest.raises(ProgramInUseError, match="finish or discard"):
            edit_program(trainer.db, edit, at(days=1))
        with pytest.raises(ProgramInUseError):
            restart_program(trainer.db, at(days=1))
        proposed = propose_edit(trainer.db, edit, at(days=1))  # proposing is harmless
        with pytest.raises(ProgramInUseError):
            accept_proposal(trainer.db, proposed.id, at(days=1))
        assert not trainer.db.in_transaction

    def test_an_edit_of_a_replaced_version_cannot_be_approved(self, trainer: Trainer) -> None:
        edit = ProgramEdit.model_validate(as_edit(trainer.program))
        stale = propose_edit(trainer.db, edit, at(days=1))
        # The owner edits directly meanwhile; the proposal still points at the old version.
        edit_program(trainer.db, edit, at(days=2))

        with pytest.raises(NoProposalError, match="no longer the live program"):
            accept_proposal(trainer.db, stale.id, at(days=3))
        assert get_program(trainer.db, stale.id).status is ProgramStatus.PROPOSED

    def test_a_proposed_edit_can_be_turned_down(self, trainer: Trainer) -> None:
        edit = ProgramEdit.model_validate(as_edit(trainer.program, name="No thanks"))
        proposed = propose_edit(trainer.db, edit, at(days=1))

        decline_proposal(trainer.db, proposed.id)

        assert get_program(trainer.db, proposed.id).status is ProgramStatus.ARCHIVED
        assert [v.version for v in versions(trainer.db)] == [1]

    def test_a_new_proposal_replaces_a_proposed_edit_and_starts_a_new_program(
        self, trainer: Trainer, ids: dict[str, int]
    ) -> None:
        live = trainer.program
        edit = propose_edit(trainer.db, ProgramEdit.model_validate(as_edit(live)), at(days=1))
        fresh = propose_program(trainer.db, ProgramIn.model_validate(program_body(ids)), at(days=1))

        assert get_program(trainer.db, edit.id).status is ProgramStatus.ARCHIVED
        accepted = accept_proposal(trainer.db, fresh.id, at(days=2))
        assert accepted.lineage_id == fresh.id
        assert accepted.lineage_id != live.lineage_id
        assert (accepted.version, accepted.change, accepted.previous_id) == (1, Change.NEW, None)

    def test_a_proposed_edit_replaces_an_earlier_proposal(
        self, trainer: Trainer, ids: dict[str, int]
    ) -> None:
        fresh = propose_program(trainer.db, ProgramIn.model_validate(program_body(ids)), at(days=1))

        edit = propose_edit(
            trainer.db, ProgramEdit.model_validate(as_edit(trainer.program)), at(days=1)
        )

        assert get_program(trainer.db, fresh.id).status is ProgramStatus.ARCHIVED
        assert programs(trainer.db, at(days=1)).proposed is not None
        assert programs(trainer.db, at(days=1)).proposed.id == edit.id  # type: ignore[union-attr]  # why: asserted just above


class TestVersionHistory:
    def test_past_workouts_stay_with_the_version_they_were_done_under(
        self, trainer: Trainer
    ) -> None:
        first = trainer.program
        old = trainer.train(DayKind.GYM, 1, 1, at(days=0), [(10, 60.0)] * 3)
        second = edit_program(
            trainer.db, ProgramEdit.model_validate(as_edit(first, name="v2")), at(days=1)
        )
        new = trainer.train(DayKind.GYM, 2, 1, at(days=2))

        assert get_workout(trainer.db, _id(trainer.db, old)).program_day_id == first.days[0].id
        assert get_workout(trainer.db, _id(trainer.db, new)).program_day_id == second.days[1].id
        assert first.days[0].id not in {d.id for d in second.days}
        # The old version is still readable as it was.
        assert get_program(trainer.db, first.id).name == "Four days"
        assert get_program(trainer.db, first.id).status is ProgramStatus.ARCHIVED

    def test_versions_are_listed_newest_first_with_their_workouts(self, trainer: Trainer) -> None:
        first = trainer.program
        trainer.train(DayKind.GYM, 1, 1, at(days=0))
        second = edit_program(trainer.db, ProgramEdit.model_validate(as_edit(first)), at(days=1))
        trainer.train(DayKind.GYM, 2, 1, at(days=2))
        trainer.train(DayKind.TRAVEL, 1, 1, at(days=3))
        third = edit_program(trainer.db, ProgramEdit.model_validate(as_edit(second)), at(days=4))

        listed = versions(trainer.db)

        assert [(v.id, v.version, v.change, v.workouts, v.current) for v in listed] == [
            (third.id, 3, Change.EDIT, 0, True),
            (second.id, 2, Change.EDIT, 2, False),
            (first.id, 1, Change.NEW, 1, False),
        ]
        assert all(v.run == 1 for v in listed)
        assert [v.name for v in listed] == ["Four days", "Four days", "Four days"]
        assert [v.started_at for v in listed] == [first.started_at] * 3

    def test_each_new_version_links_to_the_one_it_replaced(self, trainer: Trainer) -> None:
        first = trainer.program
        second = edit_program(trainer.db, ProgramEdit.model_validate(as_edit(first)), at(days=1))
        third = edit_program(trainer.db, ProgramEdit.model_validate(as_edit(second)), at(days=2))

        assert (second.previous_id, third.previous_id) == (first.id, second.id)
        assert {first.lineage_id, second.lineage_id, third.lineage_id} == {first.id}

    def test_a_new_program_is_its_own_history(self, trainer: Trainer, ids: dict[str, int]) -> None:
        old = trainer.program
        fresh = propose_program(trainer.db, ProgramIn.model_validate(program_body(ids)), at(days=1))
        accept_proposal(trainer.db, fresh.id, at(days=1))

        assert [v.id for v in versions(trainer.db)] == [fresh.id]
        assert [v.id for v in list_versions(trainer.db, old.id)] == [old.id]

    def test_no_history_without_a_program(self, db: sqlite3.Connection) -> None:
        assert versions(db) == []


class TestRestart:
    def test_back_to_week_one_day_one_of_both_sets(self, trainer: Trainer) -> None:
        trainer.train(DayKind.GYM, 1, 1, at(days=0))
        trainer.train(DayKind.GYM, 2, 1, at(days=1))
        trainer.train(DayKind.TRAVEL, 1, 1, at(days=2))
        assert programs(trainer.db, at(weeks=2)).next == Position(2, 3)

        restarted = restart_program(trainer.db, at(weeks=2))

        found = programs(trainer.db, at(weeks=2))
        assert (found.next, found.next_travel) == (Position(1, 1), Position(1, 1))
        assert [d.name for d in restarted.days] == ["Upper A", "Lower A", "Upper B"]
        assert [d.name for d in restarted.travel_days] == ["Travel A"]

    def test_a_restart_is_a_new_run_of_the_same_program(self, trainer: Trainer) -> None:
        first = trainer.program
        trainer.train(DayKind.GYM, 1, 1, at(days=0))

        restarted = restart_program(trainer.db, at(days=5))

        assert (restarted.lineage_id, restarted.version, restarted.run) == (first.lineage_id, 2, 2)
        assert (restarted.change, restarted.previous_id) == (Change.RESTART, first.id)
        assert restarted.started_at != first.started_at
        assert restarted.started_at is not None
        assert restarted.started_at.startswith("2026-10-10")
        assert get_program(trainer.db, first.id).status is ProgramStatus.ARCHIVED
        assert [(v.version, v.run, v.change) for v in versions(trainer.db)] == [
            (2, 2, Change.RESTART),
            (1, 1, Change.NEW),
        ]

    def test_logged_history_is_kept(self, trainer: Trainer) -> None:
        trainer.train(DayKind.GYM, 1, 1, at(days=0), [(10, 60.0)] * 3)
        before = trainer.db.execute("SELECT count(*) FROM workouts").fetchone()[0]
        sets = trainer.db.execute("SELECT count(*) FROM workout_sets").fetchone()[0]

        restart_program(trainer.db, at(days=3))

        assert trainer.db.execute("SELECT count(*) FROM workouts").fetchone()[0] == before
        assert trainer.db.execute("SELECT count(*) FROM workout_sets").fetchone()[0] == sets

    def test_loads_start_from_the_most_recent_logged_loads(self, trainer: Trainer) -> None:
        trainer.train(DayKind.GYM, 1, 1, at(days=0), [(10, 60.0)] * 3)
        trainer.train(DayKind.GYM, 2, 1, at(days=1))
        trainer.train(DayKind.GYM, 3, 1, at(days=2))
        trainer.train(DayKind.GYM, 1, 2, at(weeks=1), [(10, 62.5)] * 3)  # progressed to 62.5
        assert planned_gym(trainer.db, at(weeks=1, days=1))["squat"].target.load_kg == 80.0

        restart_program(trainer.db, at(weeks=2))

        bench = planned_gym(trainer.db, at(weeks=2))["bench"]
        # Week 1 day 1 again, from the last logged 62.5, not from the program's 60 and
        # not progressed further: the target is the bottom of the range.
        assert (bench.target.decision, bench.target.load_kg) == (Decision.START, 62.5)
        assert bench.target.reps == (8, 8, 8)

    def test_a_slot_never_logged_still_starts_from_its_start_load(self, trainer: Trainer) -> None:
        restart_program(trainer.db, at(days=1))

        assert planned_gym(trainer.db, at(days=1))["bench"].target.load_kg == 60.0

    def test_the_old_runs_sessions_do_not_count_towards_the_new_position(
        self, trainer: Trainer
    ) -> None:
        for number in (1, 2, 3):
            trainer.train(DayKind.GYM, number, 1, at(days=number - 1))
        restart_program(trainer.db, at(weeks=1))

        trainer.train(DayKind.GYM, 1, 1, at(weeks=1, days=1))

        assert programs(trainer.db, at(weeks=1, days=2)).next == Position(1, 2)

    def test_reps_only_slots_restart_with_no_load(self, trainer: Trainer) -> None:
        trainer.train(DayKind.TRAVEL, 1, 1, at(days=0), [(15, None)] * 3)

        restart_program(trainer.db, at(weeks=1))

        plan = today(trainer.db, at(weeks=1))
        assert plan is not None
        assert plan.travel_day is not None
        pushups = plan.travel_day.blocks[0].exercises[0]
        assert (pushups.target.decision, pushups.target.load_kg) == (Decision.START, None)

    def test_it_keeps_the_days_exactly(self, trainer: Trainer) -> None:
        before = trainer.program

        after = restart_program(trainer.db, at(days=1))

        def shape(program: Program) -> list[Any]:
            return [
                (
                    d.name,
                    [
                        (
                            b.rest_s,
                            [
                                (
                                    e.exercise_id,
                                    e.sets,
                                    e.rep_min,
                                    e.rep_max,
                                    e.progression,
                                    e.start_load_kg,
                                    e.notes,
                                    e.variations,
                                )
                                for e in b.exercises
                            ],
                        )
                        for b in d.blocks
                    ],
                )
                for d in [*program.days, *program.travel_days]
            ]

        assert shape(after) == shape(before)
        assert after.notes == before.notes == "Program notes."
        assert after.name == before.name

    def test_a_restart_can_be_edited_and_restarted_again(self, trainer: Trainer) -> None:
        second = restart_program(trainer.db, at(days=1))
        third = edit_program(trainer.db, ProgramEdit.model_validate(as_edit(second)), at(days=2))
        fourth = restart_program(trainer.db, at(days=3))

        assert [(third.version, third.run), (fourth.version, fourth.run)] == [(3, 2), (4, 3)]


def _id(db: sqlite3.Connection, client_id: str) -> int:
    return int(
        db.execute("SELECT id FROM workouts WHERE client_id = ?", (client_id,)).fetchone()[0]
    )

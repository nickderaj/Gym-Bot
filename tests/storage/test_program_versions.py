"""Program versions, travel days and slot history across versions, in storage."""

import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest

from trainer.domain.exercises import Measure
from trainer.domain.programs import Position, Progression, Session
from trainer.storage.catalogue import ExerciseSpec, upsert_exercise
from trainer.storage.database import MIGRATIONS, connect, migrate
from trainer.storage.journal import ProgramLink, SetValues, save_set, save_workout
from trainer.storage.programs import (
    BlockSpec,
    Change,
    DayKind,
    DaySpec,
    Lineage,
    Program,
    ProgramSpec,
    ProgramStatus,
    SlotSpec,
    Start,
    activate_version,
    day_claimed,
    get_program,
    insert_program,
    last_logged_load,
    last_position,
    last_slot_sets,
    list_versions,
    run_last_session,
)

T0 = "2026-10-05T08:00:00+00:00"


def day(name: str, exercise_id: int, **slot: object) -> DaySpec:
    return DaySpec(name, (BlockSpec(90, (SlotSpec(exercise_id, 3, 8, 10, None, None, **slot),)),))  # type: ignore[arg-type]  # why: keyword extras are the slot's optional fields


@pytest.fixture
def ids(db: sqlite3.Connection) -> dict[str, int]:
    return {
        name: upsert_exercise(db, ExerciseSpec(name, name.title(), equipment, None, Measure.REPS))
        for name, equipment in (
            ("bench", "barbell"),
            ("squat", "barbell"),
            ("push-up", "bodyweight"),
        )
    }


def hybrid(ids: dict[str, int]) -> ProgramSpec:
    return ProgramSpec(
        "Hybrid",
        None,
        6,
        (day("Upper", ids["bench"]), day("Lower", ids["squat"])),
        (
            day("Travel A", ids["push-up"], progression=Progression.REPS, variations="a -> b"),
            day("Travel B", ids["push-up"], progression=Progression.REPS),
        ),
    )


def activate(db: sqlite3.Connection, spec: ProgramSpec) -> Program:
    program_id = insert_program(db, spec, ProgramStatus.ACTIVE, T0)
    db.execute("UPDATE programs SET started_at = ? WHERE id = ?", (T0, program_id))
    return get_program(db, program_id)


def workout(
    db: sqlite3.Connection, name: str, started: str, link: ProgramLink | None, *, done: bool = True
) -> int:
    return save_workout(db, name, (started, started if done else None), None, link)


class TestMigration:
    def test_existing_programs_become_version_one_of_their_own_program(
        self, tmp_path: Path
    ) -> None:
        with closing(connect(tmp_path / "t.db")) as conn:
            for version, sql in enumerate(MIGRATIONS[:8], start=1):
                conn.executescript(sql)
                conn.execute(f"PRAGMA user_version = {version}")
            conn.executescript(
                """
                INSERT INTO exercises (id, name, display_name, equipment, measure)
                    VALUES (1, 'bench', 'Bench', 'barbell', 'reps');
                INSERT INTO programs (id, name, training_weeks, status, created_at, started_at)
                    VALUES (7, 'Old', 6, 'active', 't', 't'), (9, 'Older', 6, 'archived', 't', 't');
                INSERT INTO program_days (id, program_id, position, name)
                    VALUES (3, 7, 1, 'A'), (4, 7, 2, 'B');
                INSERT INTO program_blocks (id, day_id, position, rest_s)
                    VALUES (1, 3, 1, 90), (2, 4, 1, 90);
                INSERT INTO block_exercises (id, block_id, position, exercise_id, sets, rep_min,
                    rep_max) VALUES (5, 1, 1, 1, 3, 8, 10), (6, 2, 1, 1, 3, 8, 10);
                """
            )

            assert migrate(conn) == len(MIGRATIONS)

            programs = [
                tuple(row)
                for row in conn.execute(
                    "SELECT id, lineage_id, version, run, change, previous_id, start_week, "
                    "start_day, start_travel_day FROM programs ORDER BY id"
                )
            ]
            assert programs == [
                (7, 7, 1, 1, "new", None, 1, 1, 1),
                (9, 9, 1, 1, "new", None, 1, 1, 1),
            ]
            days = [
                tuple(row)
                for row in conn.execute("SELECT id, kind, number, day_key FROM program_days")
            ]
            assert days == [(3, "gym", 1, 3), (4, "gym", 2, 4)]
            slot = conn.execute(
                "SELECT progression, variations, slot_key, start_from_log FROM block_exercises"
            ).fetchone()
            assert tuple(slot) == ("load", None, 5, 0)
            # The program reads back as before, with a gym day set and no travel days.
            found = get_program(conn, 7)
            assert [d.name for d in found.days] == ["A", "B"]
            assert found.travel_days == []
            assert found.lineage_id == 7

    def test_a_travel_day_has_its_own_numbering_but_shares_the_position_order(
        self, db: sqlite3.Connection, ids: dict[str, int]
    ) -> None:
        program = activate(db, hybrid(ids))

        rows = [
            tuple(row)
            for row in db.execute(
                "SELECT position, kind, number, name FROM program_days WHERE program_id = ? "
                "ORDER BY position",
                (program.id,),
            )
        ]

        assert rows == [
            (1, "gym", 1, "Upper"),
            (2, "gym", 2, "Lower"),
            (3, "travel", 1, "Travel A"),
            (4, "travel", 2, "Travel B"),
        ]

    @pytest.mark.parametrize(
        ("column", "value"),
        [("kind", "away"), ("number", 0)],
    )
    def test_days_are_constrained(
        self, db: sqlite3.Connection, ids: dict[str, int], column: str, value: object
    ) -> None:
        program = activate(db, hybrid(ids))

        sql = f"UPDATE program_days SET {column} = ? WHERE program_id = ?"  # noqa: S608  # why: the column name comes from the parametrize list
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql, (value, program.id))

    @pytest.mark.parametrize(
        ("column", "value"),
        [("progression", "kg"), ("start_from_log", 2)],
    )
    def test_slots_are_constrained(
        self, db: sqlite3.Connection, ids: dict[str, int], column: str, value: object
    ) -> None:
        activate(db, hybrid(ids))

        with pytest.raises(sqlite3.IntegrityError):
            db.execute(f"UPDATE block_exercises SET {column} = ?", (value,))  # noqa: S608  # why: the column name comes from the parametrize list

    @pytest.mark.parametrize(
        ("column", "value"),
        [("version", 0), ("run", 0), ("change", "fork"), ("start_week", 0), ("start_day", 0)],
    )
    def test_programs_are_constrained(
        self, db: sqlite3.Connection, ids: dict[str, int], column: str, value: object
    ) -> None:
        activate(db, hybrid(ids))

        with pytest.raises(sqlite3.IntegrityError):
            db.execute(f"UPDATE programs SET {column} = ?", (value,))  # noqa: S608  # why: the column name comes from the parametrize list


class TestRoundTrip:
    def test_both_sets_come_back_in_order_with_their_slots(
        self, db: sqlite3.Connection, ids: dict[str, int]
    ) -> None:
        program = activate(db, hybrid(ids))

        assert [d.name for d in program.days] == ["Upper", "Lower"]
        assert [d.name for d in program.travel_days] == ["Travel A", "Travel B"]
        pushup = program.travel_days[0].blocks[0].exercises[0]
        assert (pushup.progression, pushup.variations) == (Progression.REPS, "a -> b")
        assert pushup.slot_key == pushup.id  # a new slot is its own key
        assert program.days[0].day_key == program.days[0].id
        assert (program.lineage_id, program.version, program.run) == (program.id, 1, 1)
        assert (program.change, program.previous_id, program.start) == (Change.NEW, None, Start())

    def test_a_gym_only_program_has_no_travel_days(
        self, db: sqlite3.Connection, ids: dict[str, int]
    ) -> None:
        spec = ProgramSpec("Gym", None, 6, (day("Upper", ids["bench"]),))

        assert activate(db, spec).travel_days == []

    def test_a_slot_that_continues_another_keeps_its_key(
        self, db: sqlite3.Connection, ids: dict[str, int]
    ) -> None:
        first = activate(db, hybrid(ids))
        slot = first.days[0].blocks[0].exercises[0]
        edit = ProgramSpec(
            "Hybrid",
            None,
            6,
            (
                DaySpec(
                    "Upper",
                    (
                        BlockSpec(
                            90,
                            (SlotSpec(ids["bench"], 3, 8, 10, None, None, slot_key=slot.slot_key),),
                        ),
                    ),
                    day_key=first.days[0].day_key,
                ),
            ),
        )

        second = get_program(
            db,
            insert_program(
                db, edit, ProgramStatus.ARCHIVED, T0, Lineage(first.id, first.id, Change.EDIT)
            ),
        )

        kept = second.days[0].blocks[0].exercises[0]
        assert (kept.slot_key, kept.id == slot.id) == (slot.slot_key, False)
        assert second.days[0].day_key == first.days[0].day_key
        assert (second.lineage_id, second.previous_id, second.change) == (
            first.id,
            first.id,
            Change.EDIT,
        )


class TestLastPositionAndSession:
    def test_each_set_has_its_own_last_day_number(
        self, db: sqlite3.Connection, ids: dict[str, int]
    ) -> None:
        program = activate(db, hybrid(ids))
        workout(db, "g", "2026-10-05T08:00:00+00:00", ProgramLink(program.days[1].id, 1))
        workout(db, "t", "2026-10-06T08:00:00+00:00", ProgramLink(program.travel_days[0].id, 2))

        assert last_position(db, program.id, DayKind.GYM) == Position(1, 2)
        assert last_position(db, program.id, DayKind.TRAVEL) == Position(2, 1)

    def test_the_last_session_is_of_either_set_and_counts_the_run(
        self, db: sqlite3.Connection, ids: dict[str, int]
    ) -> None:
        first = activate(db, hybrid(ids))
        workout(db, "g", "2026-10-05T08:00:00+00:00", ProgramLink(first.days[0].id, 1))
        workout(db, "t", "2026-10-07T23:30:00+00:00", ProgramLink(first.travel_days[0].id, 1))
        workout(db, "u", "2026-10-09T08:00:00+00:00", ProgramLink(first.days[1].id, 1), done=False)

        assert run_last_session(db, first) == Session(1, date(2026, 10, 7))

    def test_an_earlier_version_of_the_run_counts_but_an_earlier_run_does_not(
        self, db: sqlite3.Connection, ids: dict[str, int]
    ) -> None:
        first = activate(db, hybrid(ids))
        workout(db, "g", "2026-10-05T08:00:00+00:00", ProgramLink(first.days[0].id, 3))
        second_id = insert_program(
            db, hybrid(ids), ProgramStatus.ARCHIVED, T0, Lineage(first.id, first.id, Change.EDIT)
        )
        activate_version(db, second_id, first, (Change.EDIT, Start(), T0))
        second = get_program(db, second_id)
        assert run_last_session(db, second) == Session(3, date(2026, 10, 5))

        third_id = insert_program(
            db,
            hybrid(ids),
            ProgramStatus.ARCHIVED,
            T0,
            Lineage(first.id, second_id, Change.RESTART),
        )
        activate_version(db, third_id, second, (Change.RESTART, Start(), T0))

        assert run_last_session(db, get_program(db, third_id)) is None

    def test_nothing_done_yet(self, db: sqlite3.Connection, ids: dict[str, int]) -> None:
        assert run_last_session(db, activate(db, hybrid(ids))) is None


class TestSlotHistoryAcrossVersions:
    def test_a_kept_slot_sees_the_sets_logged_under_an_earlier_version(
        self, db: sqlite3.Connection, ids: dict[str, int]
    ) -> None:
        first = activate(db, hybrid(ids))
        bench = first.days[0].blocks[0].exercises[0]
        wid = workout(db, "w", "2026-10-05T08:00:00+00:00", ProgramLink(first.days[0].id, 1))
        save_set(db, "s1", (wid, ids["bench"], bench.id), SetValues(10, 60.0, None, None, None))
        edit = ProgramSpec(
            "Hybrid",
            None,
            6,
            (
                DaySpec(
                    "Upper",
                    (
                        BlockSpec(
                            90,
                            (
                                SlotSpec(
                                    ids["bench"], 3, 8, 10, None, None, slot_key=bench.slot_key
                                ),
                            ),
                        ),
                    ),
                    day_key=first.days[0].day_key,
                ),
            ),
        )
        second_id = insert_program(
            db, edit, ProgramStatus.ARCHIVED, T0, Lineage(first.id, first.id, Change.EDIT)
        )
        second = get_program(db, second_id)
        new_bench = second.days[0].blocks[0].exercises[0]

        sets = last_slot_sets(db, new_bench.id, 6)

        assert [(s.reps, s.load_kg) for s in sets] == [(10, 60.0)]
        assert new_bench.id != bench.id

    def test_a_new_slot_has_no_history_of_its_own(
        self, db: sqlite3.Connection, ids: dict[str, int]
    ) -> None:
        program = activate(db, hybrid(ids))
        bench = program.days[0].blocks[0].exercises[0]
        squat = program.days[1].blocks[0].exercises[0]
        wid = workout(db, "w", "2026-10-05T08:00:00+00:00", ProgramLink(program.days[0].id, 1))
        save_set(db, "s1", (wid, ids["bench"], bench.id), SetValues(10, 60.0, None, None, None))

        assert last_slot_sets(db, squat.id, 6) == []

    def test_the_deload_week_does_not_count(
        self, db: sqlite3.Connection, ids: dict[str, int]
    ) -> None:
        program = activate(db, hybrid(ids))
        bench = program.days[0].blocks[0].exercises[0]
        for name, started, week, load in (
            ("a", "2026-10-05T08:00:00+00:00", 6, 60.0),
            ("b", "2026-10-12T08:00:00+00:00", 7, 50.0),
        ):
            wid = workout(db, name, started, ProgramLink(program.days[0].id, week))
            save_set(db, name, (wid, ids["bench"], bench.id), SetValues(10, load, None, None, None))

        assert [s.load_kg for s in last_slot_sets(db, bench.id, 6)] == [60.0]


class TestLastLoggedLoad:
    def log(
        self,
        db: sqlite3.Connection,
        name: str,
        started: str,
        loads: list[float | None],
        link: ProgramLink | None = None,
    ) -> None:
        workout_id = workout(db, name, started, link)
        for number, load in enumerate(loads):
            values = SetValues(8, load, None, None, None)
            save_set(db, f"{name}{number}", (workout_id, self.exercise, None), values)

    @pytest.fixture(autouse=True)
    def _exercise(self, ids: dict[str, int]) -> None:
        self.exercise = ids["bench"]

    def test_never_logged(self, db: sqlite3.Connection) -> None:
        assert last_logged_load(db, self.exercise, 6) is None

    def test_the_heaviest_load_of_the_last_session(self, db: sqlite3.Connection) -> None:
        self.log(db, "old", "2026-10-01T08:00:00+00:00", [100.0])
        self.log(db, "new", "2026-10-03T08:00:00+00:00", [40.0, 60.0, 50.0])

        assert last_logged_load(db, self.exercise, 6) == 60.0

    def test_sets_without_a_load_are_no_load(self, db: sqlite3.Connection) -> None:
        self.log(db, "bw", "2026-10-03T08:00:00+00:00", [None, None])

        assert last_logged_load(db, self.exercise, 6) is None

    def test_an_unfinished_session_does_not_count(self, db: sqlite3.Connection) -> None:
        wid = workout(db, "open", "2026-10-03T08:00:00+00:00", None, done=False)
        save_set(db, "x", (wid, self.exercise, None), SetValues(8, 90.0, None, None, None))

        assert last_logged_load(db, self.exercise, 6) is None

    def test_the_deload_week_does_not_count(
        self, db: sqlite3.Connection, ids: dict[str, int]
    ) -> None:
        program = activate(db, hybrid(ids))
        link = ProgramLink(program.days[0].id, 7)
        self.log(db, "free", "2026-10-01T08:00:00+00:00", [55.0])
        self.log(db, "deload", "2026-10-12T08:00:00+00:00", [45.0], link)

        assert last_logged_load(db, self.exercise, 6) == 55.0

    def test_other_exercises_do_not_count(
        self, db: sqlite3.Connection, ids: dict[str, int]
    ) -> None:
        self.log(db, "bench", "2026-10-03T08:00:00+00:00", [60.0])
        squats = workout(db, "squats", "2026-10-04T08:00:00+00:00", None)
        save_set(db, "sq", (squats, ids["squat"], None), SetValues(8, 130.0, None, None, None))

        assert last_logged_load(db, self.exercise, 6) == 60.0


class TestDayClaimedAcrossVersions:
    def test_a_day_done_under_an_earlier_version_is_still_claimed(
        self, db: sqlite3.Connection, ids: dict[str, int]
    ) -> None:
        first = activate(db, hybrid(ids))
        workout(db, "g", "2026-10-05T08:00:00+00:00", ProgramLink(first.days[0].id, 2), done=False)
        edit = ProgramSpec(
            "Hybrid",
            None,
            6,
            (day("Upper", ids["bench"]),),
        )
        edit = ProgramSpec(
            edit.name,
            None,
            6,
            (DaySpec("Upper", edit.days[0].blocks, day_key=first.days[0].day_key),),
        )
        second = get_program(
            db,
            insert_program(
                db, edit, ProgramStatus.ARCHIVED, T0, Lineage(first.id, first.id, Change.EDIT)
            ),
        )

        assert day_claimed(db, second.days[0].id, 2) is True
        assert day_claimed(db, second.days[0].id, 3) is False

    def test_another_day_is_not_claimed(self, db: sqlite3.Connection, ids: dict[str, int]) -> None:
        first = activate(db, hybrid(ids))
        workout(db, "g", "2026-10-05T08:00:00+00:00", ProgramLink(first.days[0].id, 2))

        assert day_claimed(db, first.days[1].id, 2) is False


class TestVersions:
    def test_activating_the_next_version(self, db: sqlite3.Connection, ids: dict[str, int]) -> None:
        first = activate(db, hybrid(ids))
        second_id = insert_program(
            db, hybrid(ids), ProgramStatus.ARCHIVED, T0, Lineage(first.id, first.id, Change.EDIT)
        )

        activate_version(
            db, second_id, first, (Change.EDIT, Start(3, 2, 1), "2026-10-20T08:00:00+00:00")
        )

        second = get_program(db, second_id)
        assert second.status is ProgramStatus.ACTIVE
        assert (second.version, second.run, second.change, second.previous_id) == (
            2,
            1,
            Change.EDIT,
            first.id,
        )
        assert second.started_at == T0  # an edit goes on in the same run
        assert second.start == Start(3, 2, 1)
        assert get_program(db, first.id).status is ProgramStatus.ARCHIVED

    def test_a_restart_begins_a_new_run_with_its_own_start(
        self, db: sqlite3.Connection, ids: dict[str, int]
    ) -> None:
        first = activate(db, hybrid(ids))
        second_id = insert_program(
            db, hybrid(ids), ProgramStatus.ARCHIVED, T0, Lineage(first.id, first.id, Change.RESTART)
        )

        activate_version(
            db, second_id, first, (Change.RESTART, Start(), "2026-10-20T08:00:00+00:00")
        )

        second = get_program(db, second_id)
        assert (second.version, second.run) == (2, 2)
        assert second.started_at == "2026-10-20T08:00:00+00:00"

    def test_versions_count_on_from_the_newest(
        self, db: sqlite3.Connection, ids: dict[str, int]
    ) -> None:
        first = activate(db, hybrid(ids))
        previous = first
        for _ in range(2):
            new_id = insert_program(
                db,
                hybrid(ids),
                ProgramStatus.ARCHIVED,
                T0,
                Lineage(first.id, previous.id, Change.EDIT),
            )
            activate_version(db, new_id, previous, (Change.EDIT, Start(), T0))
            previous = get_program(db, new_id)

        assert previous.version == 3
        assert [(v.version, v.current) for v in list_versions(db, first.id)] == [
            (3, True),
            (2, False),
            (1, False),
        ]

    def test_a_proposal_never_started_is_not_a_version(
        self, db: sqlite3.Connection, ids: dict[str, int]
    ) -> None:
        first = activate(db, hybrid(ids))
        insert_program(
            db, hybrid(ids), ProgramStatus.PROPOSED, T0, Lineage(first.id, first.id, Change.EDIT)
        )

        assert [v.id for v in list_versions(db, first.id)] == [first.id]

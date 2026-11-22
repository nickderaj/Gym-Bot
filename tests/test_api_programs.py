"""Program endpoints, and workouts and sets trained from a program."""

from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

from trainer.api.app import create_app
from trainer.api.settings import CoachSettings, Settings

OWNER = {"Tailscale-User-Login": "owner@example.com"}
W1 = "11111111-1111-4111-8111-111111111111"
S1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
S2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
COACH = {"Authorization": "Bearer gateway-key"}


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(Settings(database=tmp_path / "trainer.db", owner_login="owner@example.com"))
    )


def new_exercise(client: TestClient, name: str, equipment: str = "barbell") -> int:
    body = {"name": name, "equipment": equipment}
    response = client.post("/api/exercises", json=body, headers=OWNER)
    assert response.status_code == 201, response.text
    exercise_id: int = response.json()["id"]
    return exercise_id


def program_body(bench: int, row: int) -> dict[str, Any]:
    return {
        "name": "Full Body",
        "days": [
            {
                "name": "A",
                "blocks": [
                    {
                        "exercises": [
                            {"exercise_id": bench, "sets": 3, "rep_min": 8, "rep_max": 10},
                        ]
                    },
                    {
                        "rest_s": 60,
                        "exercises": [
                            {"exercise_id": row, "sets": 2, "rep_min": 10, "rep_max": 12},
                            {"exercise_id": bench, "sets": 2, "rep_min": 12, "rep_max": 15},
                        ],
                    },
                ],
            },
            {
                "name": "B",
                "blocks": [
                    {
                        "exercises": [
                            {
                                "exercise_id": row,
                                "sets": 3,
                                "rep_min": 8,
                                "rep_max": 10,
                                "start_load_kg": 50,
                            }
                        ]
                    }
                ],
            },
        ],
    }


@pytest.fixture
def ids(client: TestClient) -> tuple[int, int]:
    return new_exercise(client, "Bench Press"), new_exercise(client, "Cable Row", "cable")


def propose(client: TestClient, ids: tuple[int, int]) -> dict[str, Any]:
    response = client.put("/api/programs/proposal", json=program_body(*ids), headers=OWNER)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def accept(client: TestClient, program_id: int) -> dict[str, Any]:
    response = client.post(f"/api/programs/{program_id}/accept", headers=OWNER)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/programs"),
        ("PUT", "/api/programs/proposal"),
        ("PUT", "/api/programs/edit-proposal"),
        ("PUT", "/api/programs/active"),
        ("POST", "/api/programs/active/restart"),
        ("GET", "/api/programs/versions"),
        ("GET", "/api/programs/1"),
        ("POST", "/api/programs/1/decline"),
        ("POST", "/api/programs/1/accept"),
        ("GET", "/api/today"),
    ],
)
def test_owner_only(client: TestClient, method: str, path: str) -> None:
    assert client.request(method, path, json={}).status_code == 403


def test_no_programs_yet(client: TestClient) -> None:
    assert client.get("/api/programs", headers=OWNER).json() == {
        "active": None,
        "proposed": None,
        "next": None,
        "next_travel": None,
    }
    response = client.get("/api/today", headers=OWNER)
    assert response.status_code == 200
    assert response.json() is None


def test_propose_then_accept(client: TestClient, ids: tuple[int, int]) -> None:
    proposed = propose(client, ids)

    assert proposed["status"] == "proposed"
    assert proposed["training_weeks"] == 6
    assert proposed["started_at"] is None
    assert proposed["created_at"].endswith("+00:00")
    day_a = proposed["days"][0]
    assert [len(block["exercises"]) for block in day_a["blocks"]] == [1, 2]
    assert day_a["blocks"][1]["rest_s"] == 60
    assert day_a["blocks"][0]["rest_s"] == 90
    assert day_a["blocks"][1]["exercises"][0] | {"id": 0, "slot_key": 0} == {
        "id": 0,
        "slot_key": 0,
        "exercise_id": ids[1],
        "name": "Cable Row",
        "equipment": "cable",
        "measure": "reps",
        "sets": 2,
        "rep_min": 10,
        "rep_max": 12,
        "start_load_kg": None,
        "notes": None,
        "increment_kg": 2.5,
        "progression": "load",
        "variations": None,
        "start_from_log": False,
    }
    listed = client.get("/api/programs", headers=OWNER).json()
    assert (listed["active"], listed["proposed"], listed["next"]) == (None, proposed, None)

    active = accept(client, proposed["id"])

    assert active["status"] == "active"
    assert active["started_at"].endswith("+00:00")
    listed = client.get("/api/programs", headers=OWNER).json()
    assert (listed["active"], listed["proposed"], listed["next"]) == (
        active,
        None,
        {"week": 1, "day": 1},
    )


def test_accepting_while_the_program_is_being_trained(
    client: TestClient, ids: tuple[int, int]
) -> None:
    active = accept(client, propose(client, ids)["id"])
    link = {"day_id": active["days"][0]["id"], "week": 1}
    started = {"started_at": "2026-10-01T09:00:00Z", "program": link}
    client.put(f"/api/workouts/{W1}", json=started, headers=OWNER)
    new = propose(client, ids)

    response = client.post(f"/api/programs/{new['id']}/accept", headers=OWNER)

    assert response.status_code == 409
    assert response.json() == {"detail": "finish or discard the workout in progress first"}


def test_accepting_what_is_not_the_proposal(client: TestClient, ids: tuple[int, int]) -> None:
    active = accept(client, propose(client, ids)["id"])

    response = client.post(f"/api/programs/{active['id']}/accept", headers=OWNER)

    assert response.status_code == 409
    assert response.json() == {"detail": f"program {active['id']} is not the proposal"}


@pytest.mark.parametrize("program_id", ["0", str(2**63), "x"])
def test_accepting_a_bad_id(client: TestClient, program_id: str) -> None:
    response = client.post(f"/api/programs/{program_id}/accept", headers=OWNER)

    assert response.status_code == 422


def test_decline(client: TestClient, ids: tuple[int, int]) -> None:
    shown = propose(client, ids)

    declined = client.post(f"/api/programs/{shown['id']}/decline", headers=OWNER)
    again = client.post(f"/api/programs/{shown['id']}/decline", headers=OWNER)

    assert declined.status_code == 204
    assert again.status_code == 409
    assert again.json() == {"detail": f"program {shown['id']} is not the proposal"}
    assert client.get("/api/programs", headers=OWNER).json()["proposed"] is None


def test_decline_only_the_proposal_shown(client: TestClient, ids: tuple[int, int]) -> None:
    shown = propose(client, ids)
    newer = propose(client, ids)  # the coach replaced it meanwhile

    response = client.post(f"/api/programs/{shown['id']}/decline", headers=OWNER)

    assert response.status_code == 409
    assert client.get("/api/programs", headers=OWNER).json()["proposed"] == newer


@pytest.mark.parametrize("program_id", ["0", str(2**63), "x"])
def test_declining_a_bad_id(client: TestClient, program_id: str) -> None:
    assert client.post(f"/api/programs/{program_id}/decline", headers=OWNER).status_code == 422


def test_a_proposal_of_unknown_exercises(client: TestClient, ids: tuple[int, int]) -> None:
    response = client.put("/api/programs/proposal", json=program_body(ids[0], 99), headers=OWNER)

    assert response.status_code == 422
    assert response.json() == {"detail": "no exercise has id 99"}


def test_a_proposal_of_the_wrong_shape(client: TestClient, ids: tuple[int, int]) -> None:
    body = program_body(*ids)
    body["days"][0]["blocks"][0]["exercises"][0]["rep_max"] = 5

    response = client.put("/api/programs/proposal", json=body, headers=OWNER)

    assert response.status_code == 422


class TestTraining:
    @pytest.fixture
    def program(self, client: TestClient, ids: tuple[int, int]) -> dict[str, Any]:
        return accept(client, propose(client, ids)["id"])

    def start(self, client: TestClient, day_id: int, week: int = 1) -> httpx2.Response:
        return client.put(
            f"/api/workouts/{W1}",
            json={
                "started_at": "2026-10-01T09:00:00Z",
                "program": {"day_id": day_id, "week": week},
            },
            headers=OWNER,
        )

    def test_today(self, client: TestClient, program: dict[str, Any]) -> None:
        today = client.get("/api/today", headers=OWNER).json()

        assert {key: today[key] for key in ("program_id", "program_name", "days")} == {
            "program_id": program["id"],
            "program_name": "Full Body",
            "days": 2,
        }
        assert today["training_weeks"] == 6
        assert (today["workout_client_id"], today["left_over"]) == (None, None)
        day = today["day"]
        assert (day["name"], day["week"], day["deload"]) == ("A", 1, False)
        exercise = day["blocks"][0]["exercises"][0]
        assert exercise["target"] == {"decision": "start", "load_kg": None, "reps": [8, 8, 8]}
        assert (exercise["last"], exercise["carried_kg"]) == (None, 0.0)

    def test_train_a_day(self, client: TestClient, program: dict[str, Any]) -> None:
        day = program["days"][0]
        slot = day["blocks"][0]["exercises"][0]

        started = self.start(client, day["id"])
        logged = client.put(
            f"/api/sets/{S1}",
            json={
                "workout_client_id": W1,
                "exercise_id": slot["exercise_id"],
                "block_exercise_id": slot["id"],
                "reps": 10,
                "load_kg": 60,
            },
            headers=OWNER,
        )

        assert started.status_code == 200
        assert (started.json()["program_day_id"], started.json()["program_week"]) == (day["id"], 1)
        assert logged.status_code == 200
        assert logged.json()["exercises"][0]["block_exercise_id"] == slot["id"]
        assert client.get("/api/today", headers=OWNER).json()["workout_client_id"] == W1

    @pytest.mark.parametrize(
        "link",
        [
            {"day_id": None},  # no week
            {"week": 1},  # no day
            {"day_id": 0, "week": 1},
            {"day_id": None, "week": 0},
            {"day_id": None, "week": 53},
        ],
    )
    def test_a_workout_names_a_day_and_its_week_together(
        self, client: TestClient, program: dict[str, Any], link: dict[str, int | None]
    ) -> None:
        real = {
            key: program["days"][0]["id"] if value is None else value for key, value in link.items()
        }
        body = {"started_at": "2026-10-01T09:00:00Z", "program": real}

        response = client.put(f"/api/workouts/{W1}", json=body, headers=OWNER)

        assert response.status_code == 422
        assert isinstance(response.json()["detail"], list)  # refused by the shape, not the day

    def test_a_finished_day_cannot_be_started_again(
        self, client: TestClient, program: dict[str, Any]
    ) -> None:
        day_id = program["days"][0]["id"]
        link = {"day_id": day_id, "week": 1}
        done = {"started_at": "2026-10-01T09:00:00Z", "ended_at": "2026-10-01T10:00:00Z"}
        client.put(f"/api/workouts/{W1}", json=done | {"program": link}, headers=OWNER)

        again = client.put(
            "/api/workouts/22222222-2222-4222-8222-222222222222",
            json={"started_at": "2026-10-02T09:00:00Z", "program": link},
            headers=OWNER,
        )

        assert again.status_code == 409
        assert again.json() == {"detail": f"week 1 of day {day_id} already has a workout"}

    def test_a_workout_of_a_missing_day(self, client: TestClient, program: dict[str, Any]) -> None:
        response = self.start(client, program["days"][1]["id"] + 100)

        assert response.status_code == 422
        assert response.json() == {"detail": "program day not found"}

    def test_week_bounds(self, client: TestClient, program: dict[str, Any]) -> None:
        day_id = program["days"][0]["id"]

        assert self.start(client, day_id, 52).json() == {"detail": "the program has 7 weeks"}
        assert self.start(client, day_id, 7).status_code == 200

    def test_a_workout_with_program_sets_keeps_its_day(
        self, client: TestClient, program: dict[str, Any]
    ) -> None:
        day = program["days"][0]
        slot = day["blocks"][0]["exercises"][0]
        self.start(client, day["id"])
        set_body = {
            "workout_client_id": W1,
            "exercise_id": slot["exercise_id"],
            "block_exercise_id": slot["id"],
            "reps": 10,
        }
        client.put(f"/api/sets/{S1}", json=set_body, headers=OWNER)

        cleared = client.put(
            f"/api/workouts/{W1}", json={"started_at": "2026-10-01T09:00:00Z"}, headers=OWNER
        )
        moved = self.start(client, program["days"][1]["id"])

        assert (cleared.status_code, moved.status_code) == (422, 422)
        assert moved.json() == {"detail": "the workout has sets for another program day"}
        assert client.put(f"/api/sets/{S1}", json=set_body, headers=OWNER).status_code == 200

    def test_a_set_for_another_days_exercise(
        self, client: TestClient, program: dict[str, Any]
    ) -> None:
        other = program["days"][1]["blocks"][0]["exercises"][0]
        self.start(client, program["days"][0]["id"])

        response = client.put(
            f"/api/sets/{S2}",
            json={
                "workout_client_id": W1,
                "exercise_id": other["exercise_id"],
                "block_exercise_id": other["id"],
                "reps": 10,
            },
            headers=OWNER,
        )

        assert response.status_code == 422
        assert response.json() == {
            "detail": "the program exercise is not on this workout's program day"
        }


class TestTheCoachsKey:
    """The coach's tool server proposes programs with the gateway's key, and does nothing else."""

    @pytest.fixture
    def coached(self, tmp_path: Path) -> TestClient:
        settings = Settings(
            database=tmp_path / "trainer.db",
            owner_login="owner@example.com",
            coach=CoachSettings("http://127.0.0.1:1", "gateway-key"),
        )
        return TestClient(create_app(settings))

    def test_it_may_propose(self, coached: TestClient) -> None:
        bench = new_exercise(coached, "Bench Press")
        body = program_body(bench, bench)

        response = coached.put("/api/programs/proposal", json=body, headers=COACH)

        assert response.status_code == 200
        assert response.json()["status"] == "proposed"

    def test_it_may_propose_an_edit_but_never_apply_one(self, coached: TestClient) -> None:
        bench = new_exercise(coached, "Bench Press")
        proposed = coached.put(
            "/api/programs/proposal", json=program_body(bench, bench), headers=OWNER
        )
        coached.post(f"/api/programs/{proposed.json()['id']}/accept", headers=OWNER)
        edit = edit_body(coached.get("/api/programs", headers=OWNER).json()["active"])

        response = coached.put("/api/programs/edit-proposal", json=edit, headers=COACH)

        assert response.status_code == 200
        assert response.json()["change"] == "edit"
        applied = coached.put("/api/programs/active", json=edit, headers=COACH)
        assert applied.status_code == 403

    def test_it_may_add_an_exercise(self, coached: TestClient) -> None:
        body = {"name": "Cossack Squat", "equipment": "bodyweight"}

        response = coached.post("/api/exercises", json=body, headers=COACH)

        assert response.status_code == 201
        assert response.json()["name"] == "Cossack Squat"

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/api/programs"),
            ("PUT", "/api/programs/active"),
            ("POST", "/api/programs/active/restart"),
            ("GET", "/api/programs/versions"),
            ("POST", "/api/programs/1/decline"),
            ("POST", "/api/programs/1/accept"),
            ("GET", "/api/today"),
            ("GET", "/api/workouts"),
            ("GET", "/api/exercises"),
            ("PUT", "/api/exercises"),
            ("POST", "/api/coach/messages"),
        ],
    )
    def test_it_may_do_nothing_else(self, coached: TestClient, method: str, path: str) -> None:
        assert coached.request(method, path, json={}, headers=COACH).status_code == 403

    @pytest.mark.parametrize(
        "headers",
        [
            {},
            {"Authorization": "Bearer wrong"},
            {"Authorization": "gateway-key"},
            {"Authorization": "bearer gateway-key"},
            {"Authorization": "Bearer gateway-key "},
        ],
    )
    def test_only_with_the_key(self, coached: TestClient, headers: dict[str, str]) -> None:
        response = coached.put("/api/programs/proposal", json={}, headers=headers)

        assert response.status_code == 403

    def test_not_without_a_coach(self, client: TestClient) -> None:
        response = client.put(
            "/api/programs/proposal", json={}, headers={"Authorization": "Bearer "}
        )

        assert response.status_code == 403


def edit_body(program: dict[str, Any], **changes: object) -> dict[str, Any]:
    """The program as an edit that changes nothing (every id kept), then ``changes``."""

    def of_day(day: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": day["id"],
            "name": day["name"],
            "blocks": [
                {
                    "rest_s": block["rest_s"],
                    "exercises": [
                        {
                            key: exercise[key]
                            for key in (
                                "id",
                                "exercise_id",
                                "sets",
                                "rep_min",
                                "rep_max",
                                "start_load_kg",
                                "notes",
                            )
                        }
                        for exercise in block["exercises"]
                    ],
                }
                for block in day["blocks"]
            ],
        }

    return {
        "name": program["name"],
        "notes": program["notes"],
        "days": [of_day(day) for day in program["days"]],
        "travel_days": [of_day(day) for day in program["travel_days"]],
    } | changes


class TestEditsAndRestart:
    @pytest.fixture
    def program(self, client: TestClient, ids: tuple[int, int]) -> dict[str, Any]:
        return accept(client, propose(client, ids)["id"])

    def train(self, client: TestClient, day_id: int, week: int, when: str) -> None:
        link = {"day_id": day_id, "week": week}
        body = {"started_at": when, "ended_at": when, "program": link}
        client.put(f"/api/workouts/{W1}", json=body, headers=OWNER)

    def test_travel_days_in_a_proposal_come_back_and_show_in_today(
        self, client: TestClient, ids: tuple[int, int]
    ) -> None:
        body = program_body(*ids)
        body["travel_days"] = [
            {
                "name": "Travel",
                "blocks": [
                    {
                        "exercises": [
                            {
                                "exercise_id": ids[0],
                                "sets": 3,
                                "rep_min": 10,
                                "rep_max": 15,
                                "progression": "reps",
                                "variations": "push-up -> archer",
                            }
                        ]
                    }
                ],
            }
        ]
        proposed = client.put("/api/programs/proposal", json=body, headers=OWNER).json()
        accept(client, proposed["id"])

        found = client.get("/api/programs", headers=OWNER).json()
        today = client.get("/api/today", headers=OWNER).json()

        assert found["active"]["travel_days"][0]["name"] == "Travel"
        assert found["next"] == {"week": 1, "day": 1}
        assert found["next_travel"] == {"week": 1, "day": 1}
        assert (today["days"], today["travel_days"]) == (2, 1)
        travel = today["travel_day"]
        assert (travel["name"], travel["kind"]) == ("Travel", "travel")
        pushups = travel["blocks"][0]["exercises"][0]
        assert (pushups["progression"], pushups["variations"]) == ("reps", "push-up -> archer")
        assert pushups["target"] == {"decision": "start", "load_kg": None, "reps": [10, 10, 10]}

    def test_a_reps_slot_with_a_load_is_refused(
        self, client: TestClient, ids: tuple[int, int]
    ) -> None:
        body = program_body(*ids)
        slot = body["days"][0]["blocks"][0]["exercises"][0]
        slot |= {"progression": "reps", "start_load_kg": 20}

        assert client.put("/api/programs/proposal", json=body, headers=OWNER).status_code == 422

    def test_without_travel_days_the_answers_are_as_before(
        self, client: TestClient, program: dict[str, Any]
    ) -> None:
        found = client.get("/api/programs", headers=OWNER).json()
        today = client.get("/api/today", headers=OWNER).json()

        assert program["travel_days"] == []
        assert found["next_travel"] is None
        assert (today["travel_days"], today["travel_day"]) == (0, None)

    def test_the_owner_edits_the_live_program(
        self, client: TestClient, program: dict[str, Any]
    ) -> None:
        self.train(client, program["days"][0]["id"], 1, "2026-10-01T09:00:00Z")

        response = client.put(
            "/api/programs/active",
            json=edit_body(program, name="Full Body v2"),
            headers=OWNER,
        )

        assert response.status_code == 200, response.text
        edited = response.json()
        assert (edited["name"], edited["version"], edited["change"]) == ("Full Body v2", 2, "edit")
        assert edited["previous_id"] == program["id"]
        found = client.get("/api/programs", headers=OWNER).json()
        assert found["active"]["id"] == edited["id"]
        assert found["next"] == {"week": 1, "day": 2}

    def test_a_proposed_edit_is_approved_like_a_proposal(
        self, client: TestClient, program: dict[str, Any]
    ) -> None:
        proposed = client.put(
            "/api/programs/edit-proposal", json=edit_body(program, name="Tweaked"), headers=OWNER
        )
        assert proposed.status_code == 200, proposed.text
        shown = client.get("/api/programs", headers=OWNER).json()
        assert (shown["active"]["name"], shown["proposed"]["change"]) == ("Full Body", "edit")

        accepted = accept(client, proposed.json()["id"])

        assert (accepted["name"], accepted["version"], accepted["run"]) == ("Tweaked", 2, 1)
        assert accepted["lineage_id"] == program["lineage_id"]

    def test_an_edit_without_a_program(self, client: TestClient) -> None:
        slot = {"exercise_id": 1, "sets": 1, "rep_min": 1, "rep_max": 1}
        body: dict[str, Any] = {
            "name": "x",
            "days": [{"name": "A", "blocks": [{"exercises": [slot]}]}],
        }

        for method, path in (
            ("PUT", "/api/programs/active"),
            ("PUT", "/api/programs/edit-proposal"),
        ):
            response = client.request(method, path, json=body, headers=OWNER)
            assert response.status_code == 409
            assert response.json() == {"detail": "there is no program being trained"}

    def test_an_edit_that_names_a_day_that_is_not_there(
        self, client: TestClient, program: dict[str, Any]
    ) -> None:
        body = edit_body(program)
        body["days"][0]["id"] = 99999

        for path in ("/api/programs/active", "/api/programs/edit-proposal"):
            response = client.put(path, json=body, headers=OWNER)
            assert response.status_code == 422
            assert response.json() == {"detail": "the live program has no day with id 99999"}

    def test_a_workout_in_progress_blocks_an_edit_and_a_restart(
        self, client: TestClient, program: dict[str, Any]
    ) -> None:
        link = {"day_id": program["days"][0]["id"], "week": 1}
        client.put(
            f"/api/workouts/{W1}",
            json={"started_at": "2026-10-01T09:00:00Z", "program": link},
            headers=OWNER,
        )

        edit = client.put("/api/programs/active", json=edit_body(program), headers=OWNER)
        restart = client.post("/api/programs/active/restart", headers=OWNER)

        for response in (edit, restart):
            assert response.status_code == 409
            assert response.json() == {"detail": "finish or discard the workout in progress first"}

    def test_restart(self, client: TestClient, program: dict[str, Any]) -> None:
        self.train(client, program["days"][0]["id"], 1, "2026-10-01T09:00:00Z")
        assert client.get("/api/programs", headers=OWNER).json()["next"] == {"week": 1, "day": 2}

        response = client.post("/api/programs/active/restart", headers=OWNER)

        assert response.status_code == 200, response.text
        restarted = response.json()
        assert (restarted["version"], restarted["run"], restarted["change"]) == (2, 2, "restart")
        assert client.get("/api/programs", headers=OWNER).json()["next"] == {"week": 1, "day": 1}
        assert client.get("/api/workouts", headers=OWNER).json() != []  # history kept

    def test_restart_without_a_program(self, client: TestClient) -> None:
        response = client.post("/api/programs/active/restart", headers=OWNER)

        assert response.status_code == 409
        assert response.json() == {"detail": "there is no program being trained"}

    def test_versions_and_an_old_version(self, client: TestClient, program: dict[str, Any]) -> None:
        self.train(client, program["days"][0]["id"], 1, "2026-10-01T09:00:00Z")
        client.put("/api/programs/active", json=edit_body(program, name="v2"), headers=OWNER)

        listed = client.get("/api/programs/versions", headers=OWNER).json()
        old = client.get(f"/api/programs/{program['id']}", headers=OWNER)

        assert [(v["version"], v["change"], v["workouts"], v["current"]) for v in listed] == [
            (2, "edit", 0, True),
            (1, "new", 1, False),
        ]
        assert old.status_code == 200
        assert (old.json()["name"], old.json()["status"]) == ("Full Body", "archived")

    def test_no_versions_without_a_program(self, client: TestClient) -> None:
        assert client.get("/api/programs/versions", headers=OWNER).json() == []

    def test_an_unknown_version(self, client: TestClient) -> None:
        response = client.get("/api/programs/99", headers=OWNER)

        assert response.status_code == 404
        assert response.json() == {"detail": "no program 99"}

    @pytest.mark.parametrize("program_id", ["0", str(2**63), "x"])
    def test_a_bad_version_id(self, client: TestClient, program_id: str) -> None:
        assert client.get(f"/api/programs/{program_id}", headers=OWNER).status_code == 422

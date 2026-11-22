"""The authenticated Streamable HTTP MCP endpoint for remote bots."""

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from fastapi.testclient import TestClient

from trainer.api.app import _bearer_matches, create_app
from trainer.api.settings import CoachSettings, Settings
from trainer.mcp.http import _SetInput, _WorkoutInput

OWNER = {"Tailscale-User-Login": "owner@example.com"}
KEY = "mcp-secret"
AUTH = {"Authorization": f"Bearer {KEY}"}


@pytest.fixture
def client(imported: sqlite3.Connection, tmp_path: Path) -> TestClient:
    """The HTTP MCP endpoint with an imported log and a configured key."""
    imported.commit()
    return TestClient(
        create_app(
            Settings(
                database=tmp_path / "trainer.db",
                owner_login="owner@example.com",
                mcp_key=KEY,
            )
        )
    )


def request(
    client: TestClient,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Send a JSON-RPC call and return its decoded body."""
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers=headers or AUTH,
    )
    assert response.status_code == 200, response.text
    return cast("dict[str, Any]", response.json())


def tool_call(client: TestClient, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call one MCP tool and parse its JSON text result."""
    response = request(client, "tools/call", {"name": name, "arguments": arguments})
    result = response["result"]
    text = result["content"][0]["text"]
    value = text if result["isError"] else json.loads(text)
    return {"isError": result["isError"], "value": value}


class TestAccess:
    def test_endpoint_requires_the_bearer_key(self, client: TestClient) -> None:
        for headers in ({}, {"Authorization": "Bearer wrong"}, OWNER):
            response = client.post(
                "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=headers
            )
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == "Bearer"

    def test_the_mcp_key_does_not_authorize_the_web_api(self, client: TestClient) -> None:
        response = client.get("/api/workouts", headers=AUTH)

        assert response.status_code == 403

    def test_mcp_without_a_configured_key_is_closed(self, tmp_path: Path) -> None:
        app = create_app(Settings(tmp_path / "trainer.db", "owner@example.com"))

        assert (
            TestClient(app).post("/mcp", json={"method": "ping"}, headers=AUTH).status_code == 401
        )


class TestTransport:
    def test_initialize_lists_the_remote_tools(self, client: TestClient) -> None:
        response = request(
            client,
            "initialize",
            {"protocolVersion": "2025-11-25"},
        )

        assert response["result"]["protocolVersion"] == "2025-11-25"
        assert response["result"]["serverInfo"]["name"] == "trainer"
        listed = request(client, "tools/list")["result"]["tools"]
        assert {tool["name"] for tool in listed} >= {
            "recent_workouts",
            "propose_program",
            "record_workout",
            "record_set",
        }
        by_name = {tool["name"]: tool for tool in listed}
        assert by_name["record_workout"]["inputSchema"] == _WorkoutInput.model_json_schema()
        assert by_name["record_set"]["inputSchema"] == _SetInput.model_json_schema()
        assert by_name["record_workout"]["description"] == (
            "Create or correct a workout entry. It returns client_id for logging sets; supply a "
            "previously returned client_id to correct an existing workout. "
            "started_at and ended_at must be timezone-aware ISO 8601 timestamps. For a "
            "program workout, provide both program_day_id (the id of a gym or a travel day) and "
            "program_week (the week of next or next_travel) from current_program. A travel "
            "workout does not advance the gym days, and the reverse."
        )
        assert by_name["record_set"]["description"] == (
            "Log a set to a workout in the app. Provide exercise_id from list_exercises and the "
            "workout client_id from record_workout. Omit client_id to create a set; provide an "
            "existing set client_id to correct it. For a program workout, set block_exercise_id "
            "to that exercise's id on the day from current_program."
        )

    def test_notifications_are_accepted_without_a_response(self, client: TestClient) -> None:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=AUTH,
        )

        assert response.status_code == 202
        assert response.content == b""

    def test_content_type_and_parse_errors(self, client: TestClient) -> None:
        unsupported = client.post(
            "/mcp", content="{}", headers={**AUTH, "content-type": "text/plain"}
        )
        missing = client.post("/mcp", content=b"{}", headers=AUTH)
        invalid = client.post(
            "/mcp", content=b"\xff", headers={**AUTH, "content-type": "application/json"}
        )

        assert unsupported.status_code == 415
        assert missing.status_code == 415
        assert invalid.status_code == 200
        assert invalid.headers["content-type"] == "application/json"
        assert (
            invalid.content
            == b'{"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"not JSON"}}'
        )


class TestWrites:
    def test_workout_and_set_can_be_created_and_corrected(self, client: TestClient) -> None:
        exercise_id = next(
            exercise["id"]
            for exercise in client.get("/api/exercises", headers=OWNER).json()
            if exercise["name"] == "Barbell Bench Press"
        )
        workout = tool_call(
            client,
            "record_workout",
            {"started_at": "2026-11-18T09:00:00Z", "notes": "push day"},
        )
        workout_id = workout["value"]["client_id"]
        assert workout["isError"] is False
        assert workout["value"]["notes"] == "push day"

        set_result = tool_call(
            client,
            "record_set",
            {
                "workout_client_id": workout_id,
                "exercise_id": exercise_id,
                "reps": 8,
                "load_kg": 60,
                "rpe": 8,
                "notes": "pause",
            },
        )
        set_record = set_result["value"]["exercises"][0]["sets"][0]
        assert set_record["load_kg"] == 60
        assert set_record["rpe"] == 8
        assert set_record["notes"] == "pause"
        corrected = tool_call(
            client,
            "record_set",
            {
                "workout_client_id": workout_id,
                "exercise_id": exercise_id,
                "client_id": set_record["client_id"],
                "reps": 9,
                "load_kg": 60,
            },
        )
        assert corrected["isError"] is False
        assert corrected["value"]["exercises"][0]["sets"][0]["reps"] == 9

    def test_invalid_workout_and_set_are_reported_to_the_caller(self, client: TestClient) -> None:
        workout = tool_call(
            client,
            "record_workout",
            {
                "started_at": "2026-11-18T10:00:00Z",
                "ended_at": "2026-11-18T09:00:00Z",
            },
        )
        set_result = tool_call(
            client,
            "record_set",
            {
                "workout_client_id": "11111111-1111-4111-8111-111111111111",
                "exercise_id": 999,
                "reps": 1,
            },
        )

        assert workout["isError"] is True
        assert "before it starts" in workout["value"]
        assert set_result["isError"] is True
        assert "workout not found" in set_result["value"]

    def test_program_proposal_is_saved_and_invalid_program_is_refused(
        self, client: TestClient
    ) -> None:
        exercise_id = next(
            exercise["id"]
            for exercise in client.get("/api/exercises", headers=OWNER).json()
            if exercise["name"] == "Barbell Bench Press"
        )
        program = {
            "name": "Remote proposal",
            "days": [
                {
                    "name": "A",
                    "blocks": [
                        {
                            "exercises": [
                                {"exercise_id": exercise_id, "sets": 3, "rep_min": 8, "rep_max": 10}
                            ]
                        }
                    ],
                }
            ],
        }
        saved = tool_call(client, "propose_program", program)
        refused = tool_call(
            client,
            "propose_program",
            {
                **program,
                "days": [
                    {
                        "name": "A",
                        "blocks": [
                            {
                                "exercises": [
                                    {"exercise_id": 999, "sets": 3, "rep_min": 8, "rep_max": 10}
                                ]
                            }
                        ],
                    }
                ],
            },
        )

        assert saved["isError"] is False
        assert saved["value"]["proposed"]["status"] == "proposed"
        assert refused["isError"] is True
        assert refused["value"] == "no exercise has id 999"

    def test_invalid_tool_arguments_are_rejected(self, client: TestClient) -> None:
        result = tool_call(
            client,
            "record_workout",
            {"started_at": "2026-11-18T09:00:00Z", "program_day_id": 2},
        )
        invalid_set = tool_call(
            client,
            "record_set",
            {
                "workout_client_id": "11111111-1111-4111-8111-111111111111",
                "exercise_id": 1,
            },
        )

        assert result["isError"] is True
        assert "provided together" in result["value"]
        assert invalid_set["isError"] is True
        assert "a set needs reps or a duration" in invalid_set["value"]


def _exercise_id(client: TestClient, name: str) -> int:
    found = next(
        exercise["id"]
        for exercise in client.get("/api/exercises", headers=OWNER).json()
        if exercise["name"] == name
    )
    assert isinstance(found, int)
    return found


def _proposal(exercise_id: int) -> dict[str, Any]:
    exercise = {"exercise_id": exercise_id, "sets": 3, "rep_min": 8, "rep_max": 10}
    return {
        "name": "Remote proposal",
        "days": [{"name": "A", "blocks": [{"exercises": [exercise]}]}],
    }


def _ping(client: TestClient, headers: dict[str, str]) -> httpx2.Response:
    return client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=headers)


class TestCredentialHandling:
    def test_the_owner_still_needs_the_bearer_key(self, client: TestClient) -> None:
        refused = _ping(client, OWNER)
        allowed = _ping(client, {**OWNER, **AUTH})

        assert refused.status_code == 401
        assert allowed.status_code == 200

    def test_scheme_case_and_malformed_headers_do_not_error(self, client: TestClient) -> None:
        assert _ping(client, {"Authorization": f"bearer {KEY}"}).status_code == 200
        for header in (f"Bearer{KEY}", "Bearer short", f"Basic {KEY}"):
            assert _ping(client, {"Authorization": header}).status_code == 401
        assert _bearer_matches("Bearer " + "é" * len(KEY), KEY) is False

    def test_a_missing_or_empty_key_rejects_bearer_none(self, tmp_path: Path) -> None:
        for mcp_key in (None, ""):
            app = create_app(
                Settings(
                    database=tmp_path / "trainer.db",
                    owner_login="owner@example.com",
                    mcp_key=mcp_key,
                )
            )
            http = TestClient(app)
            for headers in ({}, {"Authorization": "Bearer None"}, {"Authorization": "Bearer "}):
                response = http.post("/mcp", json={"method": "ping"}, headers=headers)
                assert response.status_code == 401, headers
                assert "None" not in response.text

    def test_the_token_is_not_echoed_or_logged(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        presented = "distinct-credential-value"
        response = _ping(client, {"Authorization": f"Bearer {presented}"})

        assert response.status_code == 401
        assert response.json() == {"error": "unauthorized"}
        assert presented not in response.text
        assert KEY not in response.text
        assert presented not in caplog.text
        assert KEY not in caplog.text

    def test_origin_is_rejected_without_checking_the_token(self, client: TestClient) -> None:
        response = _ping(client, {**AUTH, "Origin": "https://evil.example"})

        assert response.status_code == 403
        assert response.json() == {"error": "forbidden"}
        assert "www-authenticate" not in response.headers
        assert KEY not in response.text
        assert "evil.example" not in response.text

    def test_the_web_api_still_allows_the_owner_browser_origin(self, client: TestClient) -> None:
        response = client.get("/api/workouts", headers={**OWNER, "Origin": "https://phone.example"})

        assert response.status_code == 200

    def test_the_key_does_not_travel_in_the_query_or_a_nearby_path(
        self, client: TestClient
    ) -> None:
        query = client.post(f"/mcp?access_token={KEY}", json={"method": "ping"})
        slash = client.post(
            "/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=AUTH
        )
        nested = client.get("/foo/mcp", headers=AUTH)

        assert query.status_code == 401
        assert KEY not in query.text
        assert slash.status_code == 403
        assert nested.status_code == 403

    def test_the_coach_key_and_the_mcp_key_are_not_interchangeable(self, tmp_path: Path) -> None:
        app = create_app(
            Settings(
                database=tmp_path / "closed.db",
                owner_login="owner@example.com",
                mcp_key=KEY,
                coach=CoachSettings("http://127.0.0.1:9", "coach-secret"),
            )
        )
        http = TestClient(app)

        as_coach = http.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Authorization": "Bearer coach-secret"},
        )
        as_mcp = http.put(
            "/api/programs/proposal", json={}, headers={"Authorization": f"Bearer {KEY}"}
        )

        assert as_coach.status_code == 401
        assert "coach-secret" not in as_coach.text
        assert as_mcp.status_code == 403

    def test_a_key_containing_a_space_matches_the_remainder(self, tmp_path: Path) -> None:
        app = create_app(
            Settings(tmp_path / "spaced.db", "owner@example.com", mcp_key="mcp secret")
        )
        http = TestClient(app)

        ok = _ping(http, {"Authorization": "Bearer mcp secret"})
        short = _ping(http, {"Authorization": "Bearer mcp"})

        assert ok.status_code == 200
        assert short.status_code == 401

    def test_content_type_parameters_and_case_are_accepted(self, client: TestClient) -> None:
        body = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
        upper = client.post(
            "/mcp", content=body, headers={**AUTH, "content-type": "Application/JSON"}
        )
        charset = client.post(
            "/mcp",
            content=body,
            headers={**AUTH, "content-type": "application/json ; charset=utf-8"},
        )
        tight = client.post(
            "/mcp",
            content=body,
            headers={**AUTH, "content-type": "application/json;charset=utf-8"},
        )

        assert upper.status_code == 200
        assert charset.status_code == 200
        assert tight.status_code == 200

    def test_handled_requests_log_a_fixed_line_without_the_notes(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="trainer.mcp.http")

        result = tool_call(
            client, "record_workout", {"started_at": "2026-11-18T08:00:00Z", "notes": "secret-note"}
        )

        assert result["isError"] is False
        logged = [record for record in caplog.records if record.name == "trainer.mcp.http"]
        assert [record.levelno for record in logged] == [logging.INFO]
        assert logged[0].getMessage() == "MCP request handled"
        assert "secret-note" not in caplog.text
        assert KEY not in caplog.text


class TestProgramWrites:
    def test_a_program_workout_uses_ids_from_current_program(self, client: TestClient) -> None:
        bench = _exercise_id(client, "Barbell Bench Press")
        proposed = tool_call(client, "propose_program", _proposal(bench))
        program_id = proposed["value"]["proposed"]["id"]
        accepted = client.post(f"/api/programs/{program_id}/accept", headers=OWNER)
        assert accepted.status_code == 200

        day = tool_call(client, "current_program", {})["value"]["active"]["days"][0]
        recorded = tool_call(
            client,
            "record_workout",
            {"started_at": "2026-11-18T09:00:00Z", "program_day_id": day["id"], "program_week": 1},
        )
        logged = tool_call(
            client,
            "record_set",
            {
                "workout_client_id": recorded["value"]["client_id"],
                "exercise_id": bench,
                "reps": 8,
                "load_kg": 60,
                "block_exercise_id": day["blocks"][0]["exercises"][0]["id"],
            },
        )
        again = tool_call(
            client,
            "record_workout",
            {"started_at": "2026-11-18T10:00:00Z", "program_day_id": day["id"], "program_week": 1},
        )
        wrong_slot = tool_call(
            client,
            "record_set",
            {
                "workout_client_id": recorded["value"]["client_id"],
                "exercise_id": bench,
                "reps": 5,
                "block_exercise_id": 999,
            },
        )

        assert recorded["isError"] is False
        assert logged["isError"] is False
        assert again["isError"] is True
        assert "already has a workout" in again["value"]
        assert wrong_slot["isError"] is True
        assert "not on this workout" in wrong_slot["value"]

    def test_a_travel_program_is_proposed_edited_and_trained_remotely(
        self, client: TestClient
    ) -> None:
        bench = _exercise_id(client, "Barbell Bench Press")
        body = _proposal(bench)
        pushup = {
            "exercise_id": bench,
            "sets": 3,
            "rep_min": 10,
            "rep_max": 15,
            "progression": "reps",
            "variations": "push-up -> archer",
        }
        body["travel_days"] = [{"name": "Travel", "blocks": [{"exercises": [pushup]}]}]
        proposed = tool_call(client, "propose_program", body)
        client.post(f"/api/programs/{proposed['value']['proposed']['id']}/accept", headers=OWNER)

        current = tool_call(client, "current_program", {})["value"]
        travel = current["active"]["travel_days"][0]
        assert (travel["name"], travel["blocks"][0]["exercises"][0]["progression"]) == (
            "Travel",
            "reps",
        )
        assert current["next"] == {"week": 1, "day": 1}
        assert current["next_travel"]["day"] == 1
        recorded = tool_call(
            client,
            "record_workout",
            {
                "started_at": "2026-11-18T09:00:00Z",
                "program_day_id": travel["id"],
                "program_week": current["next_travel"]["week"],
            },
        )
        assert recorded["isError"] is False
        assert recorded["value"]["program_day_id"] == travel["id"]

        # An edit through the tool: a proposal the owner approves in the app.
        gym = current["active"]["days"][0]
        slot = gym["blocks"][0]["exercises"][0]
        edit = {
            "name": "Remote proposal",
            "days": [
                {
                    "id": gym["id"],
                    "name": "A",
                    "blocks": [
                        {
                            "exercises": [
                                {
                                    "id": slot["id"],
                                    "exercise_id": bench,
                                    "sets": 5,
                                    "rep_min": 3,
                                    "rep_max": 5,
                                }
                            ]
                        }
                    ],
                }
            ],
        }
        edited = tool_call(client, "propose_program_edit", edit)
        assert edited["isError"] is False
        assert edited["value"]["proposed"]["change"] == "edit"
        after = tool_call(client, "current_program", {})["value"]
        assert after["proposed"]["edit"] is True
        assert after["active"]["version"] == 1

    def test_an_edit_without_a_program_is_an_error_not_a_crash(self, client: TestClient) -> None:
        bench = _exercise_id(client, "Barbell Bench Press")
        edit = _proposal(bench)

        answer = tool_call(client, "propose_program_edit", edit)

        assert answer["isError"] is True
        assert answer["value"] == "there is no program being trained"

    def test_an_unwritable_edit_does_not_name_its_path(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken(*_arguments: object) -> object:
            message = "unable to open database file /srv/secret/trainer.db"
            raise sqlite3.OperationalError(message)

        monkeypatch.setattr("trainer.mcp.http.propose_edit", broken)
        edit = _proposal(_exercise_id(client, "Barbell Bench Press"))

        answer = tool_call(client, "propose_program_edit", edit)

        assert answer == {"isError": True, "value": "the edit was not saved"}

    def test_a_set_client_id_cannot_move_to_another_exercise(self, client: TestClient) -> None:
        bench = _exercise_id(client, "Barbell Bench Press")
        row = _exercise_id(client, "Lat Pulldown")
        workout = tool_call(client, "record_workout", {"started_at": "2026-11-18T11:00:00Z"})
        set_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        first = tool_call(
            client,
            "record_set",
            {
                "workout_client_id": workout["value"]["client_id"],
                "exercise_id": bench,
                "client_id": set_id,
                "reps": 5,
            },
        )
        moved = tool_call(
            client,
            "record_set",
            {
                "workout_client_id": workout["value"]["client_id"],
                "exercise_id": row,
                "client_id": set_id,
                "reps": 5,
            },
        )

        assert first["isError"] is False
        assert moved["isError"] is True
        assert "another workout or exercise" in moved["value"]

    def test_a_timed_set_does_not_need_reps(self, client: TestClient) -> None:
        hang = _exercise_id(client, "Dead Hang")
        workout = tool_call(client, "record_workout", {"started_at": "2026-11-18T12:00:00Z"})
        logged = tool_call(
            client,
            "record_set",
            {
                "workout_client_id": workout["value"]["client_id"],
                "exercise_id": hang,
                "duration_s": 30,
            },
        )

        assert logged["isError"] is False
        assert logged["value"]["exercises"][0]["sets"][0]["duration_s"] == 30

    def test_a_database_that_cannot_be_written_does_not_name_its_path(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        database = tmp_path / "trainer.db"
        bench = _exercise_id(client, "Barbell Bench Press")
        client.get("/healthz")
        for path in (database, Path(f"{database}-wal"), Path(f"{database}-shm")):
            path.chmod(0o444)

        refused = tool_call(client, "propose_program", _proposal(bench))

        assert refused["isError"] is True
        assert refused["value"] == "the program was not saved"
        assert str(database) not in refused["value"]
        assert KEY not in refused["value"]

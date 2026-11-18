"""The authenticated Streamable HTTP MCP endpoint for remote bots."""

import json
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from trainer.api.app import create_app
from trainer.api.settings import Settings

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
        invalid = client.post(
            "/mcp", content=b"\xff", headers={**AUTH, "content-type": "application/json"}
        )

        assert unsupported.status_code == 415
        assert invalid.json()["error"]["code"] == -32700


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

        set_result = tool_call(
            client,
            "record_set",
            {"workout_client_id": workout_id, "exercise_id": exercise_id, "reps": 8, "load_kg": 60},
        )
        set_record = set_result["value"]["exercises"][0]["sets"][0]
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

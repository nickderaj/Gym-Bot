"""``propose_program``: the coach's one write, through the API (no sockets)."""

import io
import json
import sqlite3
import urllib.error
import urllib.request
import urllib.response
from email.message import Message
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from trainer.api.app import create_app
from trainer.api.settings import CoachSettings, Settings
from trainer.domain.exercises import Equipment
from trainer.mcp.__main__ import programs_api, serve
from trainer.mcp.proposals import TIMEOUT_S, ProgramsApi
from trainer.mcp.protocol import ToolError
from trainer.mcp.tools import (
    ADD_EXERCISE_DESCRIPTION,
    EDIT_DESCRIPTION,
    PROPOSE_DESCRIPTION,
    _adder,
    duplicate_refusal,
    inline_refs,
    tools,
)
from trainer.services.journal import ExerciseIn
from trainer.services.programs import ProgramEdit, ProgramIn

OWNER = {"Tailscale-User-Login": "owner@example.com"}
KEY = "gateway-key"
URL = "http://127.0.0.1:8000"


class FakeResponse(urllib.response.addinfourl):
    """A response as urllib's handlers expect one (they read ``msg``)."""

    msg = "fake"


class ApiBridge(urllib.request.BaseHandler):
    """Hands each http:// request to the app in-process; records them."""

    handler_order = 10  # before urllib's own proxy and HTTP handlers

    def __init__(self, client: TestClient) -> None:
        """Answer requests with ``client``'s app."""
        self.client = client
        self.requests: list[urllib.request.Request] = []

    def http_open(self, request: urllib.request.Request) -> urllib.response.addinfourl:
        self.requests.append(request)
        response = self.client.request(
            request.get_method(),
            request.selector,
            content=request.data if isinstance(request.data, bytes) else None,
            headers=dict(request.header_items()),
        )
        return FakeResponse(
            io.BytesIO(response.content), Message(), request.full_url, response.status_code
        )


class Raising(urllib.request.BaseHandler):
    """Fails every http:// request with ``error``, or answers with ``body``."""

    handler_order = 10

    def __init__(self, error: Exception | None = None, body: bytes = b"") -> None:
        """Raise ``error`` if given, else answer 200 with ``body``."""
        self.error = error
        self.body = body
        self.timeouts: list[float | None] = []

    def http_open(self, request: urllib.request.Request) -> urllib.response.addinfourl:
        self.timeouts.append(request.timeout)
        if self.error is not None:
            raise self.error
        return FakeResponse(io.BytesIO(self.body), Message(), request.full_url, 200)


@pytest.fixture
def app(imported: sqlite3.Connection, tmp_path: Path) -> TestClient:
    """The API over the fixture log, with the coach's key configured."""
    imported.commit()
    settings = Settings(
        database=tmp_path / "trainer.db",
        owner_login="owner@example.com",
        coach=CoachSettings("http://127.0.0.1:1", KEY),
    )
    return TestClient(create_app(settings))


def ids(app: TestClient) -> dict[str, int]:
    return {e["name"]: e["id"] for e in app.get("/api/exercises", headers=OWNER).json()}


def program(exercise_id: int, **change: object) -> dict[str, Any]:
    exercise = {"exercise_id": exercise_id, "sets": 3, "rep_min": 8, "rep_max": 10} | change
    return {"name": "Push", "days": [{"name": "A", "blocks": [{"exercises": [exercise]}]}]}


def propose_tool(app: TestClient, key: str = KEY) -> tuple[Any, ApiBridge]:
    bridge = ApiBridge(app)
    api = ProgramsApi(URL, key, opener=urllib.request.build_opener(bridge))
    return tools(Path(), api)["propose_program"].call, bridge


def test_a_proposal_is_saved_through_the_api(app: TestClient) -> None:
    bench = ids(app)["Barbell Bench Press"]
    call, bridge = propose_tool(app)

    answer = call(program(bench, start_load_kg=60))

    assert isinstance(answer, dict)
    assert answer["next"] == (
        "The owner reviews it in the app's Program screen and accepts it there."
    )
    proposed = answer["proposed"]
    assert (proposed["name"], proposed["status"]) == ("Push", "proposed")
    assert proposed["days"][0]["blocks"][0]["exercises"][0]["start_load_kg"] == 60.0
    assert app.get("/api/programs", headers=OWNER).json()["proposed"] == proposed
    (request,) = bridge.requests
    assert (request.get_method(), request.full_url) == ("PUT", f"{URL}/api/programs/proposal")
    assert request.get_header("Authorization") == f"Bearer {KEY}"
    assert request.get_header("Content-type") == "application/json"
    assert request.get_header("Accept") == "application/json"
    assert request.timeout == TIMEOUT_S == 20.0
    assert isinstance(request.data, bytes)
    assert json.loads(request.data) == ProgramIn.model_validate(
        program(bench, start_load_kg=60)
    ).model_dump(mode="json")


def test_the_apis_reason_reaches_the_coach(app: TestClient) -> None:
    call, _ = propose_tool(app)

    with pytest.raises(ToolError, match=r"^the program was not saved: no exercise has id 999$"):
        call(program(999))


def test_a_wrong_key_is_refused(app: TestClient) -> None:
    call, _ = propose_tool(app, key="guess")

    with pytest.raises(ToolError, match=r"^the program was not saved: forbidden$"):
        call(program(ids(app)["Barbell Bench Press"]))

    assert app.get("/api/programs", headers=OWNER).json()["proposed"] is None


def test_bad_arguments_never_reach_the_api(app: TestClient) -> None:
    call, bridge = propose_tool(app)

    with pytest.raises(ToolError) as raised:
        call(program(1, rep_min=12))

    assert str(raised.value) == (
        "invalid arguments: days.0.blocks.0.exercises.0: Value error, rep_max is below rep_min"
    )
    assert bridge.requests == []


@pytest.mark.parametrize(
    ("status", "payload", "message"),
    [
        (422, b'{"detail": "nope"}', "the program was not saved: nope"),
        (422, b'{"detail": [{"loc": []}]}', "the program was not saved: the app answered 422"),
        (500, b"oops", "the program was not saved: the app answered 500"),
        (500, b"[]", "the program was not saved: the app answered 500"),
        (500, b"{}", "the program was not saved: the app answered 500"),
    ],
)
def test_refusals_are_told_to_the_coach(status: int, payload: bytes, message: str) -> None:
    # Built here: an HTTPError's body can be read only once.
    error = urllib.error.HTTPError(URL, status, "no", Message(), io.BytesIO(payload))
    api = ProgramsApi(URL, KEY, opener=urllib.request.build_opener(Raising(error)))

    with pytest.raises(ToolError) as raised:
        api.propose(ProgramIn.model_validate(program(1)))

    assert str(raised.value) == message


def test_an_unreachable_app_is_told_to_the_coach() -> None:
    refused = Raising(urllib.error.URLError("refused"))
    api = ProgramsApi(URL, KEY, opener=urllib.request.build_opener(refused))

    with pytest.raises(ToolError) as raised:
        api.propose(ProgramIn.model_validate(program(1)))

    assert str(raised.value) == (
        "the app cannot be reached to save the program (<urlopen error refused>)"
    )


def test_an_answer_that_is_not_json() -> None:
    api = ProgramsApi(URL, KEY, opener=urllib.request.build_opener(Raising(body=b"<html>")))

    with pytest.raises(ToolError, match=r"^the app cannot be reached to save the program \("):
        api.propose(ProgramIn.model_validate(program(1)))


def test_the_default_opener_uses_no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("http_proxy", "http://proxy.example.com:3128")
    fake = Raising(body=b'{"id": 1}')
    build = urllib.request.build_opener
    given: list[urllib.request.BaseHandler] = []

    def build_with_fake(*handlers: urllib.request.BaseHandler) -> urllib.request.OpenerDirector:
        given.extend(handlers)
        return build(fake, *handlers)

    monkeypatch.setattr(urllib.request, "build_opener", build_with_fake)

    assert ProgramsApi(URL, KEY).propose(ProgramIn.model_validate(program(1))) == {"id": 1}
    (proxies,) = given
    assert isinstance(proxies, urllib.request.ProxyHandler)
    assert proxies.proxies == {}  # type: ignore[attr-defined]  # why: set by ProxyHandler.__init__
    assert fake.timeouts == [20.0]


class TestTheTool:
    def test_offered_only_with_a_way_to_save(self) -> None:
        assert "propose_program" not in tools(Path())
        offered = tools(Path(), ProgramsApi(URL, KEY))

        assert list(offered)[-3:] == ["propose_program", "propose_program_edit", "add_exercise"]
        tool = offered["propose_program"]
        assert (tool.name, tool.description) == ("propose_program", PROPOSE_DESCRIPTION)
        assert tool.description.endswith(".")

    def test_its_schema_is_plain_nested_json_schema(self) -> None:
        schema = tools(Path(), ProgramsApi(URL, KEY))["propose_program"].input_schema
        text = json.dumps(schema)

        assert "$ref" not in text
        assert "$defs" not in text
        assert schema["additionalProperties"] is False
        assert schema["required"] == ["name", "days"]
        day = schema["properties"]["days"]["items"]
        block = day["properties"]["blocks"]["items"]
        exercise = block["properties"]["exercises"]["items"]
        assert exercise["additionalProperties"] is False
        assert exercise["properties"]["exercise_id"]["minimum"] == 1
        assert block["properties"]["exercises"]["maxItems"] == 3


def test_inline_refs_needs_definitions() -> None:
    with pytest.raises(KeyError):
        inline_refs({"type": "object"})


def test_inline_refs() -> None:
    schema = {
        "type": "object",
        "properties": {
            "a": {"$ref": "#/$defs/A"},
            "list": {"type": "array", "items": [{"$ref": "#/$defs/B"}, 3]},
        },
        "$defs": {
            "A": {"type": "object", "properties": {"b": {"$ref": "#/$defs/B"}}},
            "B": {"type": "integer"},
        },
    }

    assert inline_refs(schema) == {
        "type": "object",
        "properties": {
            "a": {"type": "object", "properties": {"b": {"type": "integer"}}},
            "list": {"type": "array", "items": [{"type": "integer"}, 3]},
        },
    }


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"TRAINER_API_URL": URL},
        {"TRAINER_COACH_KEY": KEY},
        {"TRAINER_API_URL": "", "TRAINER_COACH_KEY": KEY},
        {"TRAINER_API_URL": URL, "TRAINER_COACH_KEY": ""},
    ],
)
def test_proposing_needs_the_api_and_the_key(env: dict[str, str]) -> None:
    assert programs_api(env) is None


def test_the_server_offers_proposing_when_set_up(app: TestClient) -> None:
    bridge = ApiBridge(app)
    api = programs_api({"TRAINER_API_URL": URL, "TRAINER_COACH_KEY": KEY})
    assert api is not None
    api._opener = urllib.request.build_opener(bridge)  # noqa: SLF001  # why: no sockets in tests
    out = io.StringIO()
    listing = '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
    call = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "propose_program",
                "arguments": program(ids(app)["Barbell Bench Press"]),
            },
        }
    )

    serve([listing, call], out, Path(), api)

    listed, called = (json.loads(line) for line in out.getvalue().splitlines())
    names = [tool["name"] for tool in listed["result"]["tools"]]
    assert names[-3:] == ["propose_program", "propose_program_edit", "add_exercise"]
    assert called["result"]["isError"] is False
    assert bridge.requests[0].get_header("Authorization") == f"Bearer {KEY}"


class TestProposingAnEdit:
    """``propose_program_edit``: the same bridge, to the edit-proposal route."""

    @pytest.fixture
    def live(self, app: TestClient) -> dict[str, Any]:
        """The active program: a one-day Push, with the owner having accepted it."""
        bench = ids(app)["Barbell Bench Press"]
        proposed = app.put(
            "/api/programs/proposal", json=program(bench, start_load_kg=60), headers=OWNER
        )
        accepted = app.post(f"/api/programs/{proposed.json()['id']}/accept", headers=OWNER)
        assert accepted.status_code == 200
        body: dict[str, Any] = accepted.json()
        return body

    def edit_tool(self, app: TestClient) -> tuple[Any, ApiBridge]:
        bridge = ApiBridge(app)
        api = ProgramsApi(URL, KEY, opener=urllib.request.build_opener(bridge))
        return tools(Path(), api)["propose_program_edit"].call, bridge

    def edit(self, live: dict[str, Any], **change: object) -> dict[str, Any]:
        day = live["days"][0]
        slot = day["blocks"][0]["exercises"][0]
        exercise = {
            "id": slot["id"],
            "exercise_id": slot["exercise_id"],
            "sets": 4,
            "rep_min": 6,
            "rep_max": 8,
        }
        body = {
            "name": "Push",
            "days": [{"id": day["id"], "name": "A", "blocks": [{"exercises": [exercise]}]}],
        }
        return body | change

    def test_it_is_offered_with_its_own_description_and_schema(self) -> None:
        offered = tools(Path(), ProgramsApi(URL, KEY))
        tool = offered["propose_program_edit"]
        text = json.dumps(tool.input_schema)

        assert (tool.name, tool.description) == ("propose_program_edit", EDIT_DESCRIPTION)
        assert "$ref" not in text
        assert "$defs" not in text
        assert tool.input_schema["required"] == ["name", "days"]
        exercise = tool.input_schema["properties"]["days"]["items"]["properties"]["blocks"][
            "items"
        ]["properties"]["exercises"]["items"]
        assert "id" in exercise["properties"]
        assert (
            "id"
            not in tools(Path(), ProgramsApi(URL, KEY))["propose_program"].input_schema[
                "properties"
            ]["days"]["items"]["properties"]
        )

    def test_a_writer_alone_offers_only_what_it_writes(self) -> None:
        def write(_edit: ProgramEdit) -> object:
            return {"ok": True}

        assert "propose_program" not in tools(Path(), edit_writer=write)
        assert "propose_program_edit" in tools(Path(), edit_writer=write)
        assert "propose_program_edit" not in tools(Path(), program_writer=lambda _p: {})
        call = tools(Path(), edit_writer=write)["propose_program_edit"].call
        slot = {"exercise_id": 1, "sets": 1, "rep_min": 1, "rep_max": 1}
        answer = call({"name": "x", "days": [{"name": "A", "blocks": [{"exercises": [slot]}]}]})
        assert isinstance(answer, dict)
        assert answer["proposed"] == {"ok": True}

    def test_an_edit_is_saved_through_the_api_and_waits_for_the_owner(
        self, app: TestClient, live: dict[str, Any]
    ) -> None:
        call, bridge = self.edit_tool(app)

        answer = call(self.edit(live))

        proposed = answer["proposed"]
        assert (proposed["status"], proposed["change"], proposed["previous_id"]) == (
            "proposed",
            "edit",
            live["id"],
        )
        found = app.get("/api/programs", headers=OWNER).json()
        assert found["active"]["id"] == live["id"]  # untouched until approved
        assert found["proposed"] == proposed
        (request,) = bridge.requests
        assert (request.get_method(), request.full_url) == (
            "PUT",
            f"{URL}/api/programs/edit-proposal",
        )
        assert request.get_header("Authorization") == f"Bearer {KEY}"
        assert isinstance(request.data, bytes)
        assert json.loads(request.data) == ProgramEdit.model_validate(self.edit(live)).model_dump(
            mode="json"
        )

    def test_no_active_program_is_told_to_the_coach(self, app: TestClient) -> None:
        call, _ = self.edit_tool(app)
        bench = ids(app)["Barbell Bench Press"]
        body = program(bench)

        with pytest.raises(ToolError, match=r"^the program was not saved: there is no program"):
            call(body)

    def test_an_id_that_is_not_the_live_programs_is_told_to_the_coach(
        self, app: TestClient, live: dict[str, Any]
    ) -> None:
        call, _ = self.edit_tool(app)
        body = self.edit(live)
        body["days"][0]["id"] = 424242

        with pytest.raises(ToolError, match="the live program has no day with id 424242"):
            call(body)

    def test_bad_arguments_never_reach_the_api(self, app: TestClient, live: dict[str, Any]) -> None:
        call, bridge = self.edit_tool(app)
        body = self.edit(live)
        body["days"][0]["id"] = 0

        with pytest.raises(ToolError, match=r"^invalid arguments: days\.0\.id: "):
            call(body)
        assert bridge.requests == []


class TestAddingAnExercise:
    """``add_exercise``: the coach adds to the catalogue through the API."""

    def add_tool(self, app: TestClient, key: str = KEY) -> tuple[Any, ApiBridge]:
        bridge = ApiBridge(app)
        api = ProgramsApi(URL, key, opener=urllib.request.build_opener(bridge))
        return tools(Path(), api)["add_exercise"].call, bridge

    def test_it_is_offered_with_a_plain_schema(self) -> None:
        tool = tools(Path(), ProgramsApi(URL, KEY))["add_exercise"]
        text = json.dumps(tool.input_schema)

        assert (tool.name, tool.description) == ("add_exercise", ADD_EXERCISE_DESCRIPTION)
        assert "$ref" not in text
        assert "$defs" not in text
        assert tool.input_schema["required"] == ["name", "equipment"]
        assert tool.input_schema["additionalProperties"] is False
        assert "barbell" in tool.input_schema["properties"]["equipment"]["enum"]
        assert tool.input_schema["properties"]["measure"]["enum"] == ["reps", "seconds"]

    def test_a_writer_alone_offers_it(self) -> None:
        assert "add_exercise" not in tools(Path())
        call = tools(Path(), exercise_writer=lambda exercise: exercise.name)["add_exercise"].call

        assert call({"name": " Sled  Push ", "equipment": "other"}) == "Sled  Push"

    def test_an_exercise_is_added_through_the_api(self, app: TestClient) -> None:
        call, bridge = self.add_tool(app)

        answer = call({"name": "Cossack Squat", "equipment": "bodyweight", "measure": "seconds"})

        assert answer == {
            "id": ids(app)["Cossack Squat"],
            "name": "Cossack Squat",
            "equipment": "bodyweight",
            "muscle_groups": None,
            "measure": "seconds",
            "workouts": 0,
            "last_done": None,
            "best_load_kg": None,
        }
        (request,) = bridge.requests
        assert (request.get_method(), request.full_url) == ("POST", f"{URL}/api/exercises")
        assert request.get_header("Authorization") == f"Bearer {KEY}"
        assert isinstance(request.data, bytes)
        assert json.loads(request.data) == {
            "name": "Cossack Squat",
            "equipment": "bodyweight",
            "muscle_groups": None,
            "measure": "seconds",
            "allow_similar": False,
        }

    def test_a_taken_name_is_refused_with_its_id(self, app: TestClient) -> None:
        call, _ = self.add_tool(app)
        bench = ids(app)["Barbell Bench Press"]

        with pytest.raises(ToolError) as raised:
            call({"name": "Bench Press", "equipment": "barbell"})

        assert str(raised.value) == (
            "the exercise was not added: the catalogue already has it as "
            f"Barbell Bench Press (id {bench}, barbell)"
        )

    def test_a_similar_name_needs_allow_similar(self, app: TestClient) -> None:
        call, _ = self.add_tool(app)
        arguments = {"name": "Bench Presses", "equipment": "dumbbell"}

        with pytest.raises(ToolError, match=r"^the exercise was not added: it looks like "):
            call(arguments)
        answer = call({**arguments, "allow_similar": True})

        assert answer["name"] == "Bench Presses"

    def test_a_wrong_key_is_refused(self, app: TestClient) -> None:
        call, _ = self.add_tool(app, key="guess")

        with pytest.raises(ToolError, match=r"^the exercise was not saved: forbidden$"):
            call({"name": "Cossack Squat", "equipment": "bodyweight"})

        assert "Cossack Squat" not in ids(app)

    def test_bad_arguments_never_reach_the_api(self, app: TestClient) -> None:
        call, bridge = self.add_tool(app)

        with pytest.raises(ToolError) as raised:
            call({"name": "Row", "equipment": "boat"})

        assert str(raised.value).startswith("invalid arguments: equipment: ")
        assert bridge.requests == []

    def test_without_a_writer_it_says_so(self) -> None:
        with pytest.raises(ToolError, match=r"^adding exercises is not configured$"):
            _adder(None)({"name": "Sled Push", "equipment": "other"})

    @pytest.mark.parametrize(
        "payload",
        [
            b'{"detail": {"reason": "exists"}}',
            b'{"detail": {"matches": []}}',
            b'{"detail": {"reason": "exists", "matches": [{"id": 1}]}}',
            b'{"detail": {"reason": "exists", "matches": 3}}',
        ],
    )
    def test_a_conflict_without_matches(self, payload: bytes) -> None:
        error = urllib.error.HTTPError(URL, 409, "no", Message(), io.BytesIO(payload))
        api = ProgramsApi(URL, KEY, opener=urllib.request.build_opener(Raising(error)))

        with pytest.raises(ToolError) as raised:
            api.add_exercise(ExerciseIn(name="Sled Push", equipment=Equipment.OTHER))

        assert str(raised.value) == "the exercise was not saved: the app answered 409"

    def test_an_unreachable_app_is_told_to_the_coach(self) -> None:
        refused = Raising(urllib.error.URLError("refused"))
        api = ProgramsApi(URL, KEY, opener=urllib.request.build_opener(refused))

        with pytest.raises(ToolError) as raised:
            api.add_exercise(ExerciseIn(name="Sled Push", equipment=Equipment.OTHER))

        assert str(raised.value) == (
            "the app cannot be reached to save the exercise (<urlopen error refused>)"
        )


def test_duplicate_refusals_name_every_match() -> None:
    matches = [(3, "Bench Press", "barbell"), (7, "Floor Press", None)]

    assert duplicate_refusal(exact=True, matches=matches[:1]) == (
        "the exercise was not added: the catalogue already has it as Bench Press (id 3, barbell)"
    )
    assert duplicate_refusal(exact=False, matches=matches) == (
        "the exercise was not added: it looks like Bench Press (id 3, barbell); "
        "Floor Press (id 7). Use one of those ids, or call add_exercise again with "
        "allow_similar true if it really is a different exercise"
    )

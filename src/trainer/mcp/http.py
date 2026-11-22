"""Streamable HTTP transport for remote MCP clients, including tailnet bots."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Self, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from trainer.mcp.protocol import Json, Tool, ToolError, handle_line
from trainer.mcp.tools import tools
from trainer.services.journal import (
    InvalidSetError,
    InvalidWorkoutError,
    NotFoundError,
    StaleProgramDayError,
    record_set,
    record_workout,
)
from trainer.services.programs import (
    NoActiveProgramError,
    ProgramEdit,
    ProgramError,
    ProgramIn,
    propose_edit,
    propose_program,
)
from trainer.storage.database import connect
from trainer.storage.journal import JournalConflictError, ProgramLink, SetValues

if TYPE_CHECKING:
    from pathlib import Path

router = APIRouter()
logger = logging.getLogger(__name__)
Notes = Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)]


class _WorkoutInput(BaseModel):
    """Values required to create or correct a logged workout."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    started_at: AwareDatetime
    ended_at: AwareDatetime | None = None
    notes: Notes | None = None
    client_id: UUID | None = None
    program_day_id: Annotated[int, Field(ge=1)] | None = None
    program_week: Annotated[int, Field(ge=1, le=52)] | None = None

    @model_validator(mode="after")
    def program_link_is_complete(self) -> Self:
        """A program day and week must be provided together."""
        if (self.program_day_id is None) != (self.program_week is None):
            message = "program_day_id and program_week must be provided together"
            raise ValueError(message)
        return self


class _SetInput(BaseModel):
    """Values for one new or corrected set."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workout_client_id: UUID
    exercise_id: Annotated[int, Field(ge=1)]
    client_id: UUID | None = None
    reps: Annotated[int, Field(ge=0, le=10_000)] | None = None
    load_kg: Annotated[float, Field(ge=0, le=1_000)] | None = None
    duration_s: Annotated[float, Field(ge=0, le=86_400)] | None = None
    rpe: Annotated[int, Field(ge=1, le=10)] | None = None
    notes: Notes | None = None
    block_exercise_id: Annotated[int, Field(ge=1)] | None = None

    @model_validator(mode="after")
    def reps_or_duration(self) -> Self:
        """A set needs reps or a duration."""
        if self.reps is None and self.duration_s is None:
            message = "a set needs reps or a duration"
            raise ValueError(message)
        return self


def _arguments[Model: BaseModel](model: type[Model], value: Json) -> Model:
    try:
        return model.model_validate(value)
    except ValueError as error:
        message = f"invalid arguments: {error}"
        raise ToolError(message) from error


def _journal_tools(database: Path) -> dict[str, Tool]:
    def save_workout(arguments: Json) -> object:
        parsed = _arguments(_WorkoutInput, arguments)
        client_id = str(parsed.client_id or uuid4())
        day_id = parsed.program_day_id
        # why: cast is erased at runtime; mutating its type argument changes nothing
        week = cast("int", parsed.program_week)  # pragma: no mutate
        link = None if day_id is None else ProgramLink(day_id, week)
        try:
            with closing(connect(database)) as conn:
                result = record_workout(
                    conn,
                    client_id,
                    (parsed.started_at, parsed.ended_at),
                    parsed.notes,
                    link,
                )
        except (InvalidWorkoutError, StaleProgramDayError) as error:
            raise ToolError(str(error)) from error
        return asdict(result)

    def save_set(arguments: Json) -> object:
        parsed = _arguments(_SetInput, arguments)
        client_id = str(parsed.client_id or uuid4())
        try:
            with closing(connect(database)) as conn:
                result = record_set(
                    conn,
                    client_id,
                    (str(parsed.workout_client_id), parsed.exercise_id),
                    SetValues(
                        parsed.reps,
                        parsed.load_kg,
                        parsed.duration_s,
                        parsed.rpe,
                        parsed.notes,
                    ),
                    parsed.block_exercise_id,
                )
        except (NotFoundError, InvalidSetError, JournalConflictError) as error:
            raise ToolError(str(error)) from error
        return asdict(result)

    return {
        "record_workout": Tool(
            "record_workout",
            "Create or correct a workout entry. It returns client_id for logging sets; supply a "
            "previously returned client_id to correct an existing workout. "
            "started_at and ended_at must be timezone-aware ISO 8601 timestamps. For a "
            "program workout, provide both program_day_id (the id of a gym or a travel day) and "
            "program_week (the week of next or next_travel) from current_program. A travel "
            "workout does not advance the gym days, and the reverse.",
            _WorkoutInput.model_json_schema(),
            save_workout,
        ),
        "record_set": Tool(
            "record_set",
            "Log a set to a workout in the app. Provide exercise_id from list_exercises and the "
            "workout client_id from record_workout. Omit client_id to create a set; provide an "
            "existing set client_id to correct it. For a program workout, set block_exercise_id "
            "to that exercise's id on the day from current_program.",
            _SetInput.model_json_schema(),
            save_set,
        ),
    }


def _media_type(request: Request) -> str:
    """The request's media type, without parameters, in lower case."""
    # why: header names are case-insensitive, and a missing header is not JSON
    header = request.headers.get("content-type", "")  # pragma: no mutate
    return header.split(";")[0].strip().casefold()


def _parse_error() -> Response:
    """A JSON-RPC parse error that does not echo the body."""
    return Response(
        content='{"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"not JSON"}}',
        media_type="application/json",
    )


def _remote_tools(database: Path) -> dict[str, Tool]:
    """Read tools, program proposals and workout writes for one database."""

    def write_program(program: ProgramIn) -> object:
        try:
            with closing(connect(database)) as conn:
                return asdict(propose_program(conn, program, datetime.now(UTC)))
        except ProgramError as error:
            raise ToolError(str(error)) from error
        except sqlite3.Error as error:
            # The driver message can name the database path. The bot does not need it.
            message = "the program was not saved"
            raise ToolError(message) from error

    def write_edit(edit: ProgramEdit) -> object:
        try:
            with closing(connect(database)) as conn:
                return asdict(propose_edit(conn, edit, datetime.now(UTC)))
        except (NoActiveProgramError, ProgramError) as error:
            raise ToolError(str(error)) from error
        except sqlite3.Error as error:
            message = "the edit was not saved"
            raise ToolError(message) from error

    return {
        **tools(database, program_writer=write_program, edit_writer=write_edit),
        **_journal_tools(database),
    }


@router.post("/mcp")
async def mcp(request: Request) -> Response:
    """Handle one stateless MCP JSON-RPC request over Streamable HTTP.

    The response is a single JSON document, not a stream. Nothing about the
    call, including its arguments, is written to the log.
    """
    if _media_type(request) != "application/json":
        return Response(status_code=415)
    try:
        raw = (await request.body()).decode("utf-8")
    except UnicodeDecodeError:
        return _parse_error()
    answer = handle_line(raw, _remote_tools(request.app.state.settings.database))
    if answer is None:
        return Response(status_code=202)
    logger.info("MCP request handled")
    return Response(content=answer, media_type="application/json")

"""Streamable HTTP transport for remote MCP clients, including tailnet bots."""

from __future__ import annotations

import logging
from contextlib import closing
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Self
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
from trainer.services.programs import ProgramError, ProgramIn, propose_program
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
        try:
            with closing(connect(database)) as conn:
                result = record_workout(
                    conn,
                    client_id,
                    (parsed.started_at, parsed.ended_at),
                    parsed.notes,
                    None
                    if parsed.program_day_id is None or parsed.program_week is None
                    else ProgramLink(parsed.program_day_id, parsed.program_week),
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
            "program workout, provide both program_day_id and program_week from current_program.",
            _WorkoutInput.model_json_schema(),
            save_workout,
        ),
        "record_set": Tool(
            "record_set",
            "Log a set to a workout in the app. Provide exercise_id from list_exercises and the "
            "workout client_id from record_workout. Omit client_id to create a set; provide an "
            "existing set client_id to correct it. For a program workout, provide the matching "
            "block_exercise_id from the program day.",
            _SetInput.model_json_schema(),
            save_set,
        ),
    }


@router.post("/mcp")
async def mcp(request: Request) -> Response:
    """Handle a stateless MCP JSON-RPC request over Streamable HTTP."""
    if request.headers.get("content-type", "").split(";", maxsplit=1)[0] != "application/json":
        return Response(status_code=415)
    try:
        raw = (await request.body()).decode("utf-8")
    except UnicodeDecodeError:
        return Response(
            content='{"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"not JSON"}}',
            media_type="application/json",
        )
    database = request.app.state.settings.database

    def write_program(program: ProgramIn) -> object:
        try:
            with closing(connect(database)) as conn:
                return asdict(propose_program(conn, program, datetime.now(UTC)))
        except ProgramError as error:
            raise ToolError(str(error)) from error

    available = {**tools(database, program_writer=write_program), **_journal_tools(database)}
    answer = handle_line(raw, available)
    if answer is None:
        return Response(status_code=202)
    logger.info("MCP tool request handled")
    return Response(content=answer, media_type="application/json")

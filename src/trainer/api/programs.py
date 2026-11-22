"""Program endpoints: the active and proposed programs, edits and restarts, and today's days."""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request, Response

from trainer.services.programs import (
    MAX_ROW_ID,
    NoActiveProgramError,
    NoProposalError,
    ProgramEdit,
    ProgramError,
    ProgramIn,
    ProgramInUseError,
    Programs,
    Today,
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
from trainer.storage.database import connect
from trainer.storage.programs import (
    Program,  # why: FastAPI reads the annotation at runtime
    ProgramNotFoundError,
    VersionSummary,  # why: FastAPI reads the annotation at runtime
    get_program,
)

router = APIRouter(prefix="/api")


def _database(request: Request) -> str:
    return str(request.app.state.settings.database)


@router.get("/programs")
def get_programs(request: Request) -> Programs:
    """The active program and its next day, and the proposed program."""
    with closing(connect(_database(request))) as conn:
        return programs(conn, datetime.now(UTC))


@router.put("/programs/proposal")
def put_proposal(request: Request, body: ProgramIn) -> Program:
    """Propose a program, replacing any earlier proposal; 422 if an exercise cannot be in it."""
    try:
        with closing(connect(_database(request))) as conn:
            return propose_program(conn, body, datetime.now(UTC))
    except ProgramError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.put("/programs/edit-proposal")
def put_edit_proposal(request: Request, body: ProgramEdit) -> Program:
    """Propose an edit of the active program, replacing any earlier proposal.

    The active program is unchanged until the owner accepts the proposal.
    409 if there is no active program; 422 if an exercise or id cannot be used.
    """
    try:
        with closing(connect(_database(request))) as conn:
            return propose_edit(conn, body, datetime.now(UTC))
    except NoActiveProgramError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ProgramError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.put("/programs/active")
def put_active(request: Request, body: ProgramEdit) -> Program:
    """Edit the active program now, as a new version that keeps its place and history.

    409 if there is no active program or a workout of it is in progress; 422 if an
    exercise or id cannot be used.
    """
    try:
        with closing(connect(_database(request))) as conn:
            return edit_program(conn, body, datetime.now(UTC))
    except (NoActiveProgramError, ProgramInUseError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ProgramError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/programs/active/restart")
def restart(request: Request) -> Program:
    """Restart the active program: week 1 day 1 of both sets, history kept.

    409 if there is no active program or a workout of it is in progress.
    """
    try:
        with closing(connect(_database(request))) as conn:
            return restart_program(conn, datetime.now(UTC))
    except (NoActiveProgramError, ProgramInUseError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/programs/versions")
def get_versions(request: Request) -> list[VersionSummary]:
    """The versions of the active program, newest first."""
    with closing(connect(_database(request))) as conn:
        return versions(conn)


@router.get("/programs/{program_id}")
def get_version(request: Request, program_id: Annotated[int, Path(ge=1, le=MAX_ROW_ID)]) -> Program:
    """One version of a program, as it was (an old version for its history); 404 if none."""
    try:
        with closing(connect(_database(request))) as conn:
            return get_program(conn, program_id)
    except ProgramNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/programs/{program_id}/decline", status_code=204)
def decline(request: Request, program_id: Annotated[int, Path(ge=1, le=MAX_ROW_ID)]) -> Response:
    """Turn down the proposal the owner was shown; 409 if it is no longer the proposal."""
    try:
        with closing(connect(_database(request))) as conn:
            decline_proposal(conn, program_id)
    except NoProposalError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return Response(status_code=204)


@router.post("/programs/{program_id}/accept")
def accept(request: Request, program_id: Annotated[int, Path(ge=1, le=MAX_ROW_ID)]) -> Program:
    """Start the proposed program, or apply a proposed edit of the active program.

    409 if it is no longer the proposal, or the active program has a workout in progress.
    """
    try:
        with closing(connect(_database(request))) as conn:
            return accept_proposal(conn, program_id, datetime.now(UTC))
    except (NoProposalError, ProgramInUseError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/today")
def get_today(request: Request) -> Today | None:
    """The active program's next days (gym and travel) with their targets; null without one."""
    with closing(connect(_database(request))) as conn:
        return today(conn, datetime.now(UTC))

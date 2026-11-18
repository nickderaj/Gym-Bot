"""FastAPI application factory."""

from __future__ import annotations

import hmac
import os
import threading
from contextlib import asynccontextmanager, closing
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from trainer.api.coach import router as coach_router
from trainer.api.history import router as history_router
from trainer.api.journal import router as journal_router
from trainer.api.notices import router as notices_router
from trainer.api.profile import router as profile_router
from trainer.api.programs import router as programs_router
from trainer.api.settings import Settings, settings_from_env
from trainer.mcp.http import router as mcp_router
from trainer.services.hermes import HermesGateway
from trainer.storage.database import connect, migrate

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from starlette.responses import Response

    from trainer.services.coach import Gateway

# Set by `tailscale serve` for requests from user-owned devices on the tailnet.
IDENTITY_HEADER = "Tailscale-User-Login"
PUBLIC_PATHS = frozenset({"/healthz"})
# What the coach's tool server may do with the gateway's key instead of the
# owner's login: propose a program (it never touches the active one).
COACH_ROUTES = frozenset({("PUT", "/api/programs/proposal")})
MCP_PATH = "/mcp"


def is_coach(request: Request, settings: Settings) -> bool:
    """Whether ``request`` is the coach's, on a route the coach may use.

    The coach proves itself with the gateway's key, which only the API and the
    coach hold, compared in constant time.
    """
    if settings.coach is None or (request.method, request.url.path) not in COACH_ROUTES:
        return False
    # why: header names are case-insensitive, so a mutant changing the name's case is equal
    given = request.headers.get("Authorization")  # pragma: no mutate
    expected = f"Bearer {settings.coach.key}"
    return given is not None and hmac.compare_digest(given.encode(), expected.encode())


def create_app(settings: Settings | None = None, coach_gateway: Gateway | None = None) -> FastAPI:
    """Build the API application.

    Args:
        settings: explicit settings; by default they are read from the environment.
        coach_gateway: the coach's Hermes gateway; by default the one in the
            settings, if any.

    Returns:
        The app: the database is migrated, every route except the liveness check
        and ``/mcp`` answers only the owner's Tailscale identity, and the built
        web app (if configured) is served at ``/``. ``/mcp`` requires its own
        bearer key and does not accept the owner's identity header.
    """
    settings = settings or settings_from_env(os.environ)
    with closing(connect(settings.database)) as conn:
        migrate(conn)

    @asynccontextmanager
    async def hold_the_database_open(_app: FastAPI) -> AsyncIterator[None]:
        # SQLite removes the -wal and -shm files when its last connection closes.
        # The coach's tool server reads from a read-only mount, where it cannot
        # create them, so one idle connection keeps them for as long as the API runs.
        with closing(connect(settings.database)):
            yield

    # No schema, so no /docs or /redoc either.
    app = FastAPI(openapi_url=None, lifespan=hold_the_database_open)
    app.state.settings = settings
    if coach_gateway is None and settings.coach is not None:
        coach_gateway = HermesGateway(settings.coach.url, settings.coach.key)
    app.state.coach_gateway = coach_gateway
    app.state.coach_turn = threading.Lock()

    @app.middleware("http")
    async def owner_only(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        denied = access_denial(request, settings)
        if denied is not None:
            return denied
        return await call_next(request)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # The journal router first: its /api/workouts/current must win over /{workout_id}.
    app.include_router(journal_router)
    app.include_router(history_router)
    app.include_router(profile_router)
    app.include_router(coach_router)
    app.include_router(programs_router)
    app.include_router(notices_router)
    app.include_router(mcp_router)
    if settings.web_dir is not None:
        app.mount("/", StaticFiles(directory=settings.web_dir, html=True))
    return app


def access_denial(request: Request, settings: Settings) -> JSONResponse | None:
    """Return the denial response for an unauthorized request, or ``None``.

    ``/mcp`` is not owner-authenticated: the Tailscale identity header is how the
    phone proves itself, and a bot on the tailnet must not be able to borrow it.
    """
    if request.url.path in PUBLIC_PATHS or is_coach(request, settings):
        return None
    if request.url.path == MCP_PATH:
        return _mcp_denial(request, settings)
    if request.headers.get(IDENTITY_HEADER) == settings.owner_login:
        return None
    return JSONResponse({"detail": "forbidden"}, status_code=403)


def _mcp_denial(request: Request, settings: Settings) -> JSONResponse | None:
    """Deny ``/mcp`` unless it presents the bearer key and no ``Origin``.

    Browsers send ``Origin``; a DNS-rebinding page would too, and remote bots do
    not. The key is compared in constant time and is never copied into the
    response. With no key configured, or an empty one, every request is refused.
    """
    # why: header names are case-insensitive, so a mutant changing only the name's case is equal
    origin = request.headers.get("origin")  # pragma: no mutate
    if origin is not None:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    key = settings.mcp_key
    # why: header names are case-insensitive, and a missing header is not a bearer token
    header = request.headers.get("Authorization", "")  # pragma: no mutate
    if not key or not _bearer_matches(header, key):
        return _unauthorized()
    return None


def _bearer_matches(header: str, key: str) -> bool:
    """Whether ``header`` is ``Bearer`` plus ``key``. The scheme is case-insensitive."""
    scheme, separator, token = header.partition(" ")
    if separator != " " or scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(token.encode(), key.encode())


def _unauthorized() -> JSONResponse:
    """401 with a bearer challenge and a body that cannot echo a credential."""
    # why: header names are case-insensitive, so a mutant changing only the name's case is equal
    challenge = "WWW-Authenticate"  # pragma: no mutate
    return JSONResponse({"error": "unauthorized"}, status_code=401, headers={challenge: "Bearer"})

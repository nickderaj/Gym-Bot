"""Proposing a program and adding an exercise: the coach's writes, made through the API.

The tool server runs in the coach's sandbox, where the training log is
read-only. A proposal or a new exercise is sent to the API on loopback, which
checks it and saves it through services, authenticated with the gateway's key.
The owner accepts a proposal in the app; the coach never changes the active
program (D12).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from trainer.mcp.protocol import ToolError
from trainer.mcp.tools import duplicate_refusal

if TYPE_CHECKING:
    from pydantic import BaseModel

# Saving a program is one quick write; the coach's tool call times out at 30 s.
TIMEOUT_S = 20.0
PROPOSAL_PATH = "/api/programs/proposal"
EDIT_PATH = "/api/programs/edit-proposal"
EXERCISES_PATH = "/api/exercises"
JSON = "application/json"


class ProgramsApi:
    """The API's coach endpoints, on loopback, authenticated with the gateway's key."""

    def __init__(
        self,
        base_url: str,
        key: str,
        *,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        """Send proposals to the API at ``base_url`` (``http://host:port``) with ``key``."""
        self._base_url = base_url
        self._key = key
        # No proxies, whatever the environment says: the key goes to the API alone.
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def propose(self, program: BaseModel) -> object:
        """Save ``program`` as the proposal; return it as the API saved it.

        Raises:
            ToolError: with the API's reason if it refused the program, or if it
                could not be reached.
        """
        return self._send(PROPOSAL_PATH, program)

    def propose_edit(self, edit: BaseModel) -> object:
        """Save ``edit`` of the active program as the proposal; return it as the API saved it.

        Raises:
            ToolError: with the API's reason if it refused the edit, or if it
                could not be reached.
        """
        return self._send(EDIT_PATH, edit)

    def add_exercise(self, exercise: BaseModel) -> object:
        """Add ``exercise`` to the catalogue; return it as the API saved it.

        Raises:
            ToolError: naming the matches if it exists or looks like an exercise
                that does, with the API's reason if it refused it otherwise, or
                if it could not be reached.
        """
        # pragma: no mutate start  # why: urllib POSTs a request with a body, so method=None is POST
        return self._send(EXERCISES_PATH, exercise, method="POST", what="exercise")
        # pragma: no mutate end  # why: closes the block above

    def _send(
        self, path: str, body: BaseModel, *, method: str = "PUT", what: str = "program"
    ) -> object:
        # pragma: no mutate start  # why: urllib capitalises header names; case mutants are equal
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": JSON, "Accept": JSON}
        # pragma: no mutate end  # why: closes the block above
        request = urllib.request.Request(  # noqa: S310  # why: the URL is the configured loopback API
            self._base_url + path,
            data=body.model_dump_json().encode(),
            headers=headers,
            method=method,
        )
        try:
            with self._opener.open(request, timeout=TIMEOUT_S) as response:
                saved: object = json.load(response)
        except urllib.error.HTTPError as error:
            raise ToolError(_refusal(error, what)) from error
        except (OSError, json.JSONDecodeError) as error:
            message = f"the app cannot be reached to save the {what} ({error})"
            raise ToolError(message) from error
        return saved


def _refusal(error: urllib.error.HTTPError, what: str) -> str:
    """The API's reason: text (an exercise that cannot be used), or a duplicate's matches."""
    try:
        body: Any = json.load(error)
        detail = body["detail"]
    except (OSError, ValueError, TypeError, KeyError):
        detail = None
    if isinstance(detail, str):
        return f"the {what} was not saved: {detail}"
    if isinstance(detail, dict):
        with suppress(TypeError, KeyError):
            return _duplicate(detail)
    return f"the {what} was not saved: the app answered {error.code}"


def _duplicate(detail: dict[str, Any]) -> str:
    """The refusal for a 409 that names the exercises a new one duplicates."""
    matches = [(match["id"], match["name"], match["equipment"]) for match in detail["matches"]]
    return duplicate_refusal(exact=detail["reason"] == "exists", matches=matches)

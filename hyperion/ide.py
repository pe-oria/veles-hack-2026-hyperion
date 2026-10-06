"""Read-only access to the IDE backend (files are written by the IDE when it runs our actions)."""

import asyncio
import os
from dataclasses import dataclass

import httpx

from helpers import IDE_BACKEND_URL, ValidateFileError, validate_file

# how long we wait for the IDE to apply an action before giving up on validating it
APPLY_TIMEOUT = float(os.environ.get("IDE_APPLY_TIMEOUT", "4"))
POLL_INTERVAL = 0.25


@dataclass(frozen=True)
class File:
    path: str  # where the file actually lives
    content: str


class IdeError(Exception):
    """`kind` is not_found | ambiguous | unreachable | error; the message is safe to show."""

    def __init__(self, kind: str, message: str, matches: list[str] | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.matches = matches or []


async def read(path: str) -> File:
    """Read a workspace file by full path or bare name."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{IDE_BACKEND_URL}/agent/file", params={"path": path})
    except httpx.HTTPError as exc:
        raise IdeError("unreachable", "I cannot reach the IDE backend right now.") from exc
    if response.status_code == 404:
        raise IdeError("not_found", f"I could not find `{path}` in the workspace.")
    if response.status_code == 409:
        matches = response.json().get("matches", [])
        listed = ", ".join(f"`{match}`" for match in matches)
        raise IdeError("ambiguous", f"Several files are named `{path}`: {listed}. Which one do you mean?", matches)
    if response.status_code != 200:
        raise IdeError("error", f"The IDE could not read `{path}`.")
    body = response.json()
    return File(path=body.get("path", path), content=body.get("content", ""))


async def find(path: str) -> File | None:
    """The file, or None when it does not exist. Other failures still raise."""
    try:
        return await read(path)
    except IdeError as exc:
        if exc.kind == "not_found":
            return None
        raise


async def wait_for_content(path: str, content: str, timeout: float | None = None) -> bool:
    """Wait until the IDE has written `content` to `path`. False if it never shows up."""
    deadline = asyncio.get_running_loop().time() + (APPLY_TIMEOUT if timeout is None else timeout)
    while True:
        try:
            if (await read(path)).content.strip() == content.strip():
                return True
        except IdeError as exc:
            if exc.kind == "unreachable":
                return False
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(POLL_INTERVAL)


async def validate(path: str) -> dict:
    """The backend's validation report: {path, type, valid, errors[], warnings[]}."""
    try:
        return await validate_file(path)
    except ValidateFileError as exc:
        raise IdeError("error", f"I could not validate `{path}`: {exc}.") from exc

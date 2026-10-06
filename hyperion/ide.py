"""Read-only access to the IDE backend (files are written by the IDE when it runs our actions)."""

import asyncio
import logging
import os
import socket
import struct
from dataclasses import dataclass

import httpx

import helpers
from helpers import ValidateFileError, validate_file

log = logging.getLogger("hyperion")

# how long we wait for the IDE to apply an action before giving up on validating it
APPLY_TIMEOUT = float(os.environ.get("IDE_APPLY_TIMEOUT", "4"))
POLL_INTERVAL = 0.25


UNREACHABLE = (
    "I cannot reach the IDE backend, so I cannot read or validate files. If Hyperion runs in Docker "
    "on Linux, start it with --add-host host.docker.internal:host-gateway or set IDE_BACKEND_URL."
)


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


def default_gateway() -> str | None:
    """The container's default gateway, which is the Docker host on Linux."""
    try:
        with open("/proc/net/route") as routes:
            for line in routes.readlines()[1:]:
                fields = line.split()
                if len(fields) > 2 and fields[1] == "00000000":
                    return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
    except (OSError, ValueError):
        pass
    return None


def candidate_urls() -> list[str]:
    """Where the IDE backend may be, most specific first.

    `host.docker.internal` exists on Docker Desktop but on Linux only with
    `--add-host host.docker.internal:host-gateway`; the default gateway covers a plain
    `docker run` there, and localhost covers `--network host` and running outside Docker.
    """
    port_path = ":3001/api"
    urls = [helpers.IDE_BACKEND_URL, f"http://host.docker.internal{port_path}"]
    gateway = default_gateway()
    if gateway:
        urls.append(f"http://{gateway}{port_path}")
    urls += [f"http://localhost{port_path}", f"http://172.17.0.1{port_path}"]
    return list(dict.fromkeys(url.rstrip("/") for url in urls if url))


async def _answers(client: httpx.AsyncClient, url: str) -> bool:
    try:
        response = await client.get(f"{url}/agent/file", params={"path": "__hyperion_probe__"})
    except httpx.HTTPError:
        return False
    # the backend answers a missing file with a JSON 404; some other web server would not
    return response.status_code in (200, 404, 409) and "application/json" in response.headers.get("content-type", "")


_discovery = asyncio.Lock()


async def discover() -> str | None:
    """Point the helpers at the first backend address that answers. None if none does."""
    async with _discovery:
        urls = candidate_urls()
        async with httpx.AsyncClient(timeout=2) as client:
            alive = await asyncio.gather(*(_answers(client, url) for url in urls))
        for url, ok in zip(urls, alive):
            if ok:
                if url != helpers.IDE_BACKEND_URL:
                    log.info("IDE backend found at %s (configured: %s)", url, helpers.IDE_BACKEND_URL)
                    helpers.IDE_BACKEND_URL = url
                return url
        log.warning("IDE backend not reachable at any of: %s", ", ".join(urls))
        return None


async def _get_file(path: str) -> httpx.Response:
    async with httpx.AsyncClient(timeout=10) as client:
        return await client.get(f"{helpers.IDE_BACKEND_URL}/agent/file", params={"path": path})


async def read(path: str) -> File:
    """Read a workspace file by full path or bare name."""
    try:
        response = await _get_file(path)
    except httpx.HTTPError:
        # the backend may have started after us, or live at another address: look again once
        if await discover() is None:
            raise IdeError("unreachable", UNREACHABLE) from None
        try:
            response = await _get_file(path)
        except httpx.HTTPError as exc:
            raise IdeError("unreachable", UNREACHABLE) from exc
    if response.status_code == 404:
        raise IdeError("not_found", f"I could not find {path} in the workspace.")
    if response.status_code == 409:
        matches = response.json().get("matches", [])
        listed = ", ".join(f"{match}" for match in matches)
        raise IdeError("ambiguous", f"Several files are named {path}: {listed}. Which one do you mean?", matches)
    if response.status_code != 200:
        raise IdeError("error", f"The IDE could not read {path}.")
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
        if "cannot reach" in str(exc):
            raise IdeError("unreachable", UNREACHABLE) from exc
        raise IdeError("error", f"I could not validate {path}: {exc}.") from exc

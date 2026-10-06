"""IDE action events and workspace path sanitisation."""

import re
from pathlib import PurePosixPath

_ALLOWED = re.compile(r"^[\w .\-/]+$")


class PathError(ValueError):
    """The path cannot be used in the workspace; the message is safe to show the user."""


def clean_path(raw: str | None) -> str:
    """Normalise a user/LLM supplied path to a safe workspace-relative one."""
    path = (raw or "").strip().strip("`'\"").replace("\\", "/")
    if not path:
        raise PathError("I need a file or folder name for that.")
    if path.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", path):
        raise PathError(f"{path} is an absolute path - paths must be relative to the workspace.")
    parts = [part for part in path.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise PathError(f"{path} leaves the workspace - '..' is not allowed.")
    cleaned = "/".join(parts)
    if not cleaned or not _ALLOWED.match(cleaned):
        raise PathError(f"{path} is not a valid workspace path.")
    return cleaned


def has_extension(path: str) -> bool:
    return bool(PurePosixPath(path).suffix)


def is_yaml(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in (".yaml", ".yml")


def create_folder(path: str) -> dict:
    return {"action": "create_folder", "path": path}


def delete_folder(path: str) -> dict:
    return {"action": "delete_folder", "path": path}


def create_file(path: str, content: str) -> dict:
    return {"action": "create_file", "path": path, "content": content}


def edit_file(path: str, content: str) -> dict:
    return {"action": "edit_file", "path": path, "content": content}


def delete_file(path: str) -> dict:
    return {"action": "delete_file", "path": path}

"""Handlers for the file intents. Each yields text increments (str) and IDE actions (dict)."""

import difflib
import logging
import re
from collections.abc import AsyncIterator

import yaml

from hyperion import actions, ide, llm, prompts, yamlgen
from hyperion.confirm import Pending
from hyperion.actions import PathError
from hyperion.ide import IdeError
from hyperion.router import Route
from hyperion.session import Session

log = logging.getLogger("hyperion")

Event = str | dict
MAX_REPAIRS = 2
MAX_SHOWN_CHARS = 3000
_FIX = re.compile(r"\b(fix|repair|correct|make (it|this|that|the file) valid|resolve)\b", re.IGNORECASE)
_EXPLAIN = re.compile(r"\b(explain|describe|summar|what does|what is in|what's in|tell me about)", re.IGNORECASE)


def grounded_path(route: Route, text: str, session: Session) -> str | None:
    """The router's path, but only if the user wrote it or it is a file/folder we know.

    The router sometimes invents a path from earlier turns; acting on a made-up name is
    harmless for a question and dangerous for a delete.
    """
    raw = (route.path or "").strip().strip("`'\"")
    if not raw:
        return None
    lowered, name = text.lower(), raw.rstrip("/").rsplit("/", 1)[-1]
    if raw.lower() in lowered or name.lower() in lowered:
        return raw
    known = [*session.files, *session.folders]
    if raw in known or any(item.rsplit("/", 1)[-1] == name for item in known):
        return raw
    parent = raw.rpartition("/")[0]
    if parent and re.search(rf"\b{re.escape(parent.lower())}\b", lowered):
        return f"{parent}/"  # the folder was named, the file name was made up
    return None


def target_file(route: Route, text: str, session: Session) -> str:
    """The file a request is about: the one named, else the one we last touched."""
    path = grounded_path(route, text, session)
    if path and not path.endswith("/"):
        return actions.clean_path(path)
    if session.last_file:
        return session.last_file
    raise PathError("Which file do you mean? Give me its name or path.")


def is_folder_reference(raw: str, text: str, session: Session) -> bool:
    """Whether a path without extension names a folder to put the file in (vs. a file name)."""
    if raw.endswith("/") or raw.strip("/") in session.folders:
        return True
    inside = rf"\b(in|into|inside|under)\s+(the\s+|a\s+)?(folder\s+|directory\s+)?[`'\"]?{re.escape(raw.strip('/'))}\b"
    named = rf"\b(folder|directory)\s+(called\s+|named\s+)?[`'\"]?{re.escape(raw.strip('/'))}\b"
    return bool(re.search(inside, text, re.IGNORECASE) or re.search(named, text, re.IGNORECASE))


def format_report(report: dict) -> str:
    """Human-readable summary of an IDE validation report."""
    path, kind = report.get("path", "the file"), report.get("type") or "unknown"
    errors, warnings = report.get("errors", []), report.get("warnings", [])
    lines = [
        f"{path} is a valid {kind} application descriptor."
        if report.get("valid")
        else f"{path} ({kind}) has {len(errors)} error(s):"
    ]
    lines += [f"- line {e.get('line')}: {e.get('field') or 'file'} {e.get('message')}" for e in errors[:10]]
    if warnings:
        lines.append(f"{len(warnings)} warning(s):")
        lines += [f"- line {w.get('line')}: {w.get('field')} {w.get('message')}" for w in warnings[:10]]
    return "\n".join(lines)


def diff_summary(before: str, after: str, limit: int = 16) -> str:
    """Changed lines as plain text ("- old" / "+ new"); the chat does not render Markdown."""
    changed = [
        f"  {line[0]} {line[1:].strip()}"
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
        if line[:1] in "+-" and not line.startswith(("+++", "---")) and line[1:].strip()
    ]
    return "\n".join(changed[:limit] + (["  ..."] if len(changed) > limit else []))


def parses(content: str) -> bool:
    try:
        return isinstance(yaml.safe_load(content), dict)
    except yaml.YAMLError:
        return False


async def validate_and_repair(path: str, content: str) -> AsyncIterator[Event]:
    """Wait for the IDE to write the file, validate it, and let the LLM fix reported errors."""
    yield "\n\nValidating with the IDE... "
    for attempt in range(MAX_REPAIRS + 1):
        if not await ide.wait_for_content(path, content):
            yield "skipped: I could not confirm that the IDE saved the file."
            return
        try:
            report = await ide.validate(path)
        except IdeError as exc:
            yield f"skipped. {exc}"
            return
        if report.get("valid"):
            warnings = len(report.get("warnings", []))
            yield "passed" + (f" with {warnings} warning(s)." if warnings else ".")
            return
        errors = report.get("errors", [])
        fixed = await yamlgen.repair(content, errors) if attempt < MAX_REPAIRS else content
        if fixed.strip() == content.strip() or not parses(fixed):
            yield "failed.\n\n" + format_report(report)
            return
        yield f"found {len(errors)} problem(s), fixing them.\n\nValidating again... "
        content = fixed
        yield actions.edit_file(path, content)


async def create_folder(route: Route, text: str, session: Session) -> AsyncIterator[Event]:
    path = actions.clean_path(grounded_path(route, text, session))
    yield actions.create_folder(path)
    session.remember_folder(path)
    yield f"Created the folder {path}."


async def delete_folder(route: Route, text: str, session: Session) -> AsyncIterator[Event]:
    named = grounded_path(route, text, session)
    if not named and not session.last_folder:
        raise PathError("Which folder do you mean? Give me its name or path.")
    path = actions.clean_path(named or session.last_folder)
    inside = [file for file in session.files if file.startswith(f"{path}/")]
    contents = f" It contains {', '.join(inside)}." if inside else ""
    question = f"Delete the folder {path} and everything in it?{contents}"
    session.pending_action = Pending(actions.delete_folder(path), question, f"Deleted the folder {path}.")
    yield f"{question} {prompts.CONFIRM_HINT}"


async def create_file(route: Route, text: str, session: Session) -> AsyncIterator[Event]:
    raw = grounded_path(route, text, session) or ""
    if raw and actions.has_extension(raw) and not actions.is_yaml(raw):
        async for event in create_plain_file(actions.clean_path(raw), text, session):
            yield event
        return

    params = await yamlgen.extract_params(text, route.app_kind, route.image)
    if not raw:
        path = f"{params.name}.yaml"
    elif actions.has_extension(raw):
        path = actions.clean_path(raw)
    elif is_folder_reference(raw, text, session):
        path = actions.clean_path(f"{raw}/{params.name}.yaml")
    else:
        path = actions.clean_path(f"{raw}.yaml")

    content = yamlgen.render(params)
    what = "device application manifest" if params.kind == "device" else "native application profile"
    subject = f"{params.image}:{params.tag}" if params.workload_kind == "DockerImage" else f"{params.name}"
    note = ""
    if params.placeholders:
        note = "\n\nI used placeholder values you need to replace: " + ", ".join(params.placeholders) + "."

    if await exists(path):
        question = f"{path} already exists. Overwrite it with a new {what} for {subject}?"
        done = f"Overwrote {path} with a new {what} for {subject}.{note}"
        # create_file fails on an existing file: replacing it is an edit_file
        session.pending_action = Pending(actions.edit_file(path, content), question, done, validate=True)
        yield f"{question} {prompts.CONFIRM_HINT}"
        return

    yield f"Creating {path} - a {what} for {subject}."
    yield actions.create_file(path, content)
    session.remember_file(path)
    if note:
        yield note
    async for event in validate_and_repair(path, content):
        yield event


async def create_plain_file(path: str, text: str, session: Session) -> AsyncIterator[Event]:
    content = await yamlgen.write_plain(path, text)
    if await exists(path):
        question = f"{path} already exists. Overwrite it with new content?"
        session.pending_action = Pending(actions.edit_file(path, content), question, f"Overwrote {path}.")
        yield f"{question} {prompts.CONFIRM_HINT}"
        return
    yield f"Creating {path}."
    yield actions.create_file(path, content)
    session.remember_file(path)


async def exists(path: str) -> bool:
    """Whether exactly this path exists (a bare name must not match a file elsewhere)."""
    try:
        found = await ide.find(path)
    except IdeError:
        return False  # backend unreachable or name ambiguous elsewhere: let the IDE decide
    return found is not None and found.path == path


async def edit_file(route: Route, text: str, session: Session) -> AsyncIterator[Event]:
    file = await ide.read(target_file(route, text, session))
    session.remember_file(file.path)
    structured = actions.is_yaml(file.path)
    yield f"Working on {file.path}... "  # the rewrite takes a couple of seconds

    errors = await reported_errors(file.path) if _FIX.search(text) else []
    if _FIX.search(text) and not errors and yamlgen.detect_kind(file.content):
        yield "it already passes validation, so there is nothing to fix."
        return
    # "fix it" says nothing about what is wrong: the validator's report is the instruction
    updated = await (yamlgen.repair(file.content, errors) if errors else yamlgen.edit(file.content, text))
    if structured and not parses(updated):
        log.warning("edit of %s produced invalid YAML - retrying once", file.path)
        updated = await yamlgen.edit(file.content, text)
    if structured and not parses(updated):
        yield "I could not produce a valid update, so I left the file unchanged."
        return
    if updated.strip() == file.content.strip():
        yield "I could not work out what to change. Can you rephrase the change?"
        return

    yield f"done:\n{diff_summary(file.content, updated)}"
    yield actions.edit_file(file.path, updated)
    if yamlgen.detect_kind(updated):
        async for event in validate_and_repair(file.path, updated):
            yield event


async def reported_errors(path: str) -> list[dict]:
    try:
        return (await ide.validate(path)).get("errors", [])
    except IdeError:
        return []


async def delete_file(route: Route, text: str, session: Session) -> AsyncIterator[Event]:
    path = target_file(route, text, session)
    try:
        path = (await ide.read(path)).path
    except IdeError as exc:
        if exc.kind != "unreachable":
            raise
    question = f"Delete {path}?"
    session.pending_action = Pending(actions.delete_file(path), question, f"Deleted {path}.")
    yield f"{question} {prompts.CONFIRM_HINT}"


async def execute_pending(pending: Pending, session: Session) -> AsyncIterator[Event]:
    """Carry out an action the user has just confirmed."""
    action, path = pending.action["action"], pending.action["path"]
    yield pending.action
    if action == "delete_file":
        session.forget_file(path)
    elif action == "delete_folder":
        session.forget_folder(path)
    else:
        session.remember_file(path)
    yield pending.done
    if pending.validate:
        async for event in validate_and_repair(path, pending.action["content"]):
            yield event


async def validate_file(route: Route, text: str, session: Session) -> AsyncIterator[Event]:
    path = target_file(route, text, session)
    await ide.read(path)  # finds the backend if needed and gives the clearer "not found" message
    report = await ide.validate(path)
    if report.get("path"):
        session.remember_file(report["path"])
    yield format_report(report)


async def read_file(route: Route, text: str, session: Session) -> AsyncIterator[Event]:
    file = await ide.read(target_file(route, text, session))
    session.remember_file(file.path)
    shown = file.content[:MAX_SHOWN_CHARS]
    if _EXPLAIN.search(text):
        messages = [("system", prompts.EXPLAIN_FILE_SYSTEM), ("human", f"File {file.path}:\n\n{shown}")]
        async for piece in llm.stream_text(messages):
            yield piece
        return
    truncated = "\n(truncated)" if len(file.content) > MAX_SHOWN_CHARS else ""
    yield f"{file.path}:\n\n{shown.rstrip()}{truncated}"


HANDLERS = {
    "create_folder": create_folder,
    "delete_folder": delete_folder,
    "create_file": create_file,
    "edit_file": edit_file,
    "delete_file": delete_file,
    "validate_file": validate_file,
    "read_file": read_file,
}


async def handle(route: Route, text: str, session: Session) -> AsyncIterator[Event]:
    """Run a file intent, turning expected failures into a plain message for the user."""
    try:
        async for event in HANDLERS[route.intent](route, text, session):
            yield event
    except (PathError, IdeError) as exc:
        yield str(exc)

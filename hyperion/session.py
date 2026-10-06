"""In-memory per-user session store (keyed by the IDE's user_id)."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hyperion.confirm import Pending

MAX_TURNS = 6  # user+assistant pairs kept; the model only has an 8k context
MAX_CHARS = 1200  # per stored message


@dataclass
class Session:
    history: list[tuple[str, str]] = field(default_factory=list)  # (role, text)
    pending_action: "Pending | None" = None  # a destructive action awaiting the user's "yes"
    pending_create: object | None = None  # fileops.PendingCreate: a create waiting for an image name
    # the rest of a multi-step plan, paused by a question to the user: (step number, text)
    pending_steps: list[tuple[int, str]] = field(default_factory=list)
    plan_total: int = 0
    last_file: str | None = None
    files: list[str] = field(default_factory=list)  # there is no "list files" endpoint: we keep track
    last_folder: str | None = None
    folders: list[str] = field(default_factory=list)

    def remember_file(self, path: str) -> None:
        if path not in self.files:
            self.files.append(path)
        self.last_file = path

    def forget_file(self, path: str) -> None:
        self.files = [known for known in self.files if known != path]
        if self.last_file == path:
            self.last_file = self.files[-1] if self.files else None

    def remember_folder(self, path: str) -> None:
        if path not in self.folders:
            self.folders.append(path)
        self.last_folder = path

    def forget_folder(self, path: str) -> None:
        self.folders = [known for known in self.folders if known != path and not known.startswith(f"{path}/")]
        if self.last_folder not in self.folders:
            self.last_folder = self.folders[-1] if self.folders else None
        for known in [file for file in self.files if file.startswith(f"{path}/")]:
            self.forget_file(known)

    def add_turn(self, user_text: str, assistant_text: str) -> None:
        self.history.append(("user", user_text[:MAX_CHARS]))
        self.history.append(("assistant", assistant_text[:MAX_CHARS]))
        del self.history[: -2 * MAX_TURNS]

    def last_user_text(self) -> str | None:
        return next((text for role, text in reversed(self.history) if role == "user"), None)

    def facts(self) -> str:
        """What we know we did in the workspace, for answers about the conversation."""
        files = ", ".join(self.files) or "none"
        folders = ", ".join(self.folders) or "none"
        return f"- Files created or worked on: {files}\n- Folders created: {folders}"

    def transcript(self, max_chars: int = 300) -> str:
        """Compact text form of the history, for the router prompt."""
        if not self.history:
            return "(none)"
        return "\n".join(f"{role}: {text[:max_chars]}" for role, text in self.history)


_sessions: dict[str, Session] = {}


def get_session(user_id: str) -> Session:
    return _sessions.setdefault(user_id, Session())

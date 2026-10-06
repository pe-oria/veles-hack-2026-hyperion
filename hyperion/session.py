"""In-memory per-user session store (keyed by the IDE's user_id)."""

from dataclasses import dataclass, field

MAX_TURNS = 6  # user+assistant pairs kept; the model only has an 8k context
MAX_CHARS = 1200  # per stored message


@dataclass
class Session:
    history: list[tuple[str, str]] = field(default_factory=list)  # (role, text)
    pending_action: dict | None = None
    last_file: str | None = None
    files: list[str] = field(default_factory=list)

    def add_turn(self, user_text: str, assistant_text: str) -> None:
        self.history.append(("user", user_text[:MAX_CHARS]))
        self.history.append(("assistant", assistant_text[:MAX_CHARS]))
        del self.history[: -2 * MAX_TURNS]

    def last_user_text(self) -> str | None:
        return next((text for role, text in reversed(self.history) if role == "user"), None)

    def transcript(self, max_chars: int = 300) -> str:
        """Compact text form of the history, for the router prompt."""
        if not self.history:
            return "(none)"
        return "\n".join(f"{role}: {text[:max_chars]}" for role, text in self.history)


_sessions: dict[str, Session] = {}


def get_session(user_id: str) -> Session:
    return _sessions.setdefault(user_id, Session())

"""Intent + slot extraction: one narrow JSON-mode LLM call."""

from typing import Literal

from pydantic import BaseModel, ValidationError, field_validator

from hyperion import llm, prompts
from hyperion.session import Session

Intent = Literal[
    "question",
    "create_file",
    "edit_file",
    "delete_file",
    "create_folder",
    "delete_folder",
    "validate_file",
    "read_file",
    "smalltalk",
    "off_topic",
]


class Route(BaseModel):
    intent: Intent = "question"
    path: str | None = None
    app_kind: Literal["native", "device"] | None = None
    image: str | None = None
    description: str | None = None

    @field_validator("path", "app_kind", "image", "description", mode="before")
    @classmethod
    def blank_to_none(cls, value: object) -> object:
        if isinstance(value, str) and value.strip().lower() in ("", "null", "none"):
            return None
        return value


def build_messages(text: str, session: Session) -> list[tuple[str, str]]:
    messages = [("system", prompts.ROUTER_SYSTEM)]
    for conversation, example, answer in prompts.ROUTER_EXAMPLES:
        messages.append(("human", prompts.ROUTER_USER.format(conversation=conversation, text=example)))
        messages.append(("ai", answer))
    messages.append(
        ("human", prompts.ROUTER_USER.format(conversation=session.transcript(), text=text[:1000]))
    )
    return messages


def parse_route(data: dict) -> Route:
    """Be lenient: a bad slot must not throw away a good intent."""
    try:
        return Route.model_validate(data)
    except ValidationError:
        pass
    try:
        return Route.model_validate({"intent": data.get("intent")})
    except ValidationError:
        return Route()


async def route(text: str, session: Session) -> Route:
    return parse_route(await llm.ask_json(build_messages(text, session)))

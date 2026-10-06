"""Off-topic detection on top of the router's verdict."""

import re

from hyperion.router import Route

# An 8B router sometimes calls unfamiliar HyperAI jargon off-topic; these terms veto that.
_DOMAIN = re.compile(
    r"hyper[\s-]?ai|hyperion|device\s?node|open connectors?|swarm|edge[\s-]to[\s-]cloud|"
    r"continuum|application profile|native app|device app|descriptor|\bdsl\b|\bya?ml\b|"
    r"\bD[3-5]\.\d\b|this ide|the ide|workspace",
    re.IGNORECASE,
)


def mentions_domain(text: str) -> bool:
    return bool(_DOMAIN.search(text))


def apply(route: Route, text: str) -> Route:
    """Return the route to act on, overriding a mistaken off_topic verdict."""
    if route.intent == "off_topic" and mentions_domain(text):
        return route.model_copy(update={"intent": "question"})
    return route

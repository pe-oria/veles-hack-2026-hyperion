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


RESCUE_MIN = 0.70  # knowledge-base similarity that overrides an off_topic verdict
REFUSE_BELOW = 0.55  # a "question" this far from the knowledge base is not about HyperAI


def apply(
    route: Route, text: str, similarity: float | None = None, follow_up_similarity: float | None = None
) -> Route:
    """Return the route to act on, correcting the router with cheap deterministic checks.

    `similarity` is the best cosine score of the raw message against the knowledge base and
    `follow_up_similarity` that of the history-aware query (None when retrieval was unavailable).
    Only the raw score may rescue an off_topic verdict: "tell me a joke" must not ride on the
    previous question. Either score may keep a question alive: "why?" is nothing on its own.
    """
    if route.intent == "off_topic":
        if mentions_domain(text) or (similarity is not None and similarity >= RESCUE_MIN):
            return route.model_copy(update={"intent": "question"})
    elif route.intent == "question" and not route.about_conversation:
        scores = [score for score in (similarity, follow_up_similarity) if score is not None]
        if scores and max(scores) < REFUSE_BELOW and not mentions_domain(text):
            return route.model_copy(update={"intent": "off_topic"})
    return route

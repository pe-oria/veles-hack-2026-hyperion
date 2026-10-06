"""Off-topic detection on top of the router's verdict."""

import re

from hyperion import yamlgen
from hyperion.router import Route
from hyperion.session import Session

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
        if is_injection(text):
            return route
        if mentions_domain(text) or (similarity is not None and similarity >= RESCUE_MIN):
            return route.model_copy(update={"intent": "question"})
    elif route.intent == "question" and not route.about_conversation:
        scores = [score for score in (similarity, follow_up_similarity) if score is not None]
        if scores and max(scores) < REFUSE_BELOW and not mentions_domain(text):
            return route.model_copy(update={"intent": "off_topic"})
    return route


_DEVICE_WORDS = re.compile(r"\b(device|edge|android|apk|esp[\s-]?32\w*|firmware|microcontroller)\b", re.IGNORECASE)


_FILE_NAME = re.compile(r"[\w./-]+\.(ya?ml|json|md|txt|toml|py|sh|conf|cfg|ini|xml|env)\b", re.IGNORECASE)
_READ_VERB = re.compile(r"\b(show|open|print|display|read|view|cat|explain|describe|summari[sz]e|contents?|look at|what(?:'s| is) in)\b", re.IGNORECASE)
_DESCRIPTOR = re.compile(r"\b(descriptors?|profiles?|manifests?|ya?ml|deployment)\b", re.IGNORECASE)
_CREATE_CUE = re.compile(r"\b(for|need|want|give me|write|generate|make|create|new|draft|prepare|set up)\b", re.IGNORECASE)


# Whether Hyperion writes files that are not HyperAI descriptors (README, hello.py, notes.txt).
# Kept on until the mentor answers the scope question; set to False to refuse them as off-topic.
ALLOW_GENERIC_FILES = True

# Words that also have a technical meaning are left out or guarded: "user stories", "speech
# recognition", "resume the workflow", "cv" (computer vision), and the docs' own "cookbook recipes".
_NON_TECHNICAL = re.compile(
    r"\b(poems?|poetry|haikus?|limericks?|sonnets?|(?<!user )stor(?:y|ies)|fairy ?tales?|novels?|songs?|lyrics|"
    r"(?<!cookbook )recipes?|jokes?|riddles?|essays?|(?:love |cover )letters?|letter to|horoscopes?|homework|"
    r"tweets?|blog posts?|diary|journal entry|shopping list|grocery list|biography|trivia|quiz)\b",
    re.IGNORECASE,
)


_INJECTION = re.compile(
    r"\b(?:ignore|disregard|forget|override|bypass)\b.{0,40}\b(?:instructions?|rules?|prompts?|guidelines|restrictions|guardrails)\b"
    r"|\b(?:system|initial|hidden|developer) prompt\b"
    r"|\b(?:pretend|act|behave|role-?play)\b.{0,20}\b(?:as|you are|to be|like)\b"
    r"|\byou are now\b|\bjailbreak\b|\bDAN mode\b",
    re.IGNORECASE,
)


def is_injection(text: str) -> bool:
    """An attempt to change what Hyperion is: "ignore your instructions", "pretend you are ..."."""
    return bool(_INJECTION.search(text))


def writes_off_topic_content(route: Route, text: str) -> bool:
    """A file request whose content has nothing to do with HyperAI: "a file with a poem about cats"."""
    if route.intent not in ("create_file", "edit_file"):
        return False
    if _NON_TECHNICAL.search(f"{text} {route.description or ''}") and "cookbook" not in text.lower():
        return True
    if not ALLOW_GENERIC_FILES and route.intent == "create_file":
        named = _FILE_NAME.search(text)
        return bool(named) and not named.group(0).lower().endswith((".yaml", ".yml"))
    return False


def asks_for_a_new_descriptor(route: Route, text: str, session: Session) -> bool:
    """A read_file verdict for a file that cannot exist: "I need a descriptor for X"."""
    if _FILE_NAME.search(text) or _READ_VERB.search(text):
        return False  # a file is named, or the wording really is about looking at one
    known = {item.rsplit("/", 1)[-1] for item in session.files}
    if route.path and (route.path in session.files or route.path.rsplit("/", 1)[-1] in known):
        return False
    return bool(_DESCRIPTOR.search(text) and _CREATE_CUE.search(text))


def correct(route: Route, text: str, session: Session) -> Route:
    """Deterministic repairs of the router's verdict, applied before anything acts on it."""
    if route.intent == "read_file" and asks_for_a_new_descriptor(route, text, session):
        route = route.model_copy(update={"intent": "create_file", "path": None})
    if writes_off_topic_content(route, text):
        return route.model_copy(update={"intent": "off_topic"})
    if route.intent in ("smalltalk", "question") and is_injection(text):
        # the router is unsure whether these are chit-chat; they get the refusal, never an answer
        return route.model_copy(update={"intent": "off_topic"})
    if route.intent == "create_file":
        updates: dict = {}
        if not route.image:
            # the 8B router only fills `image` when the word "image" is there; names are easy to read
            guess = yamlgen.guess_image(text)
            if guess and guess[1]:
                updates["image"] = guess[0]
        if not route.app_kind:
            # the documented default: a device app only when the user says so
            updates["app_kind"] = "device" if _DEVICE_WORDS.search(text) else "native"
        if updates:
            route = route.model_copy(update=updates)
    return route

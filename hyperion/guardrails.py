"""Off-topic detection on top of the router's verdict."""

import re

from hyperion import yamlgen
from hyperion.router import Route
from hyperion.session import Session

# Terms that only HyperAI uses: on their own they make a message on-topic.
_SPECIFIC = re.compile(
    r"hyper[\s-]?ai|hyperion|device\s?nodes?|open connectors?|swarms?|edge[\s-]to[\s-]cloud|"
    r"application profiles?|native app(?:lication)?s?|device app(?:lication)?s?|descriptors?|"
    r"profile manager|\bapm\b|\bD[3-5]\.\d\b|deliverables?",
    re.IGNORECASE,
)
# Words HyperAI shares with all of IT. They count only next to a file or an IDE action.
_GENERIC = re.compile(r"\bdsl\b|\bya?ml\b|this ide|the ide|workspace|continuum|manifests?|profiles?", re.IGNORECASE)
_WORKSPACE_ACTION = re.compile(
    r"[\w./-]+\.ya?ml\b|\b(create|make|generate|write|edit|change|update|delete|remove|validate|open|show)\b"
    r".{0,40}\b(files?|folders?|director(?:y|ies)|ya?mls?|profiles?|manifests?|descriptors?|deployments?)\b",
    re.IGNORECASE,
)

_TECH = (
    r"kubernetes|k8s|docker|containers?|containeri[sz]ation|rest(?:ful)?(?: apis?)?|apis?|ya?ml|json|xml|tls|ssl|"
    r"https?|tcp(?:/ip)?|udp|dns|linux|unix|windows|android|ios|python|java(?:script)?|typescript|golang|rust|sql|"
    r"nosql|databases?|cloud computing|edge computing|fog computing|the cloud|iot|internet of things|"
    r"microservices?|devops|ci/?cd|git(?:hub|lab)?|helm|terraform|ansible|machine learning|deep learning|"
    r"artificial intelligence|ai|llms?|neural networks?|blockchain|virtual machines?|vms?|hypervisors?|serverless|"
    r"load balanc(?:er|ers|ing)|mqtt|grpc|oauth|esp32|arduino|raspberry pi|5g|wi-?fi|bluetooth|nginx|redis|"
    r"postgres(?:ql)?|kafka|encryption|virtuali[sz]ation|orchestration|operating systems?|networking"
)
# "What is Docker?", "explain REST APIs", "how does TLS work?", "what is edge computing in general?"
_GENERAL_QUESTION = re.compile(
    rf"(?:please\s+)?(?:can you\s+|could you\s+)?"
    rf"(?:what(?:'s| is| are| does)|explain|define|describe|tell me (?:about|what (?:is|are))|how do(?:es)?|"
    rf"give me an (?:overview|introduction) (?:of|to))\s+"
    rf"(?:an?\s+|the\s+)?(?:{_TECH})"
    rf"(?:\s+(?:work|works|mean|means|do|does|in general|exactly|used for|to me|briefly|and how (?:does|do) (?:it|they) work))*"
    rf"\s*[?.!]*",
    re.IGNORECASE,
)


def mentions_hyperai(text: str) -> bool:
    return bool(_SPECIFIC.search(text))


def mentions_domain(text: str) -> bool:
    """A HyperAI-specific term, or a generic IT word together with a file or an IDE action."""
    return mentions_hyperai(text) or bool(_GENERIC.search(text) and _WORKSPACE_ACTION.search(text))


def is_general_question(text: str) -> bool:
    """A general-knowledge question about technology, with nothing tying it to HyperAI."""
    return not mentions_hyperai(text) and bool(_GENERAL_QUESTION.fullmatch(text.strip()))


# Knowledge-base similarity that overrides an off_topic verdict. General technology questions
# score up to 0.73 ("What is Docker?") but are caught by is_general_question before this applies.
RESCUE_MIN = 0.70
REFUSE_BELOW = 0.55  # a "question" this far from the knowledge base is not about HyperAI
# A question that names nothing HyperAI-specific must be close to the documentation to be answered.
# Generic technology questions sit at 0.60-0.68 ("What is Kubernetes?"), HyperAI ones at 0.73+.
GENERAL_MIN = 0.70


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
        if is_injection(text) or is_general_question(text):
            return route
        if mentions_domain(text) or (similarity is not None and similarity >= RESCUE_MIN):
            return route.model_copy(update={"intent": "question"})
    elif route.intent == "question" and not route.about_conversation:
        if is_general_question(text):
            return route.model_copy(update={"intent": "off_topic"})
        scores = [score for score in (similarity, follow_up_similarity) if score is not None]
        floor = REFUSE_BELOW if mentions_domain(text) else GENERAL_MIN
        if scores and max(scores) < floor and not mentions_hyperai(text):
            return route.model_copy(update={"intent": "off_topic"})
    return route


_DEVICE_WORDS = re.compile(r"\b(device|edge|android|apk|esp[\s-]?32\w*|firmware|microcontroller)\b", re.IGNORECASE)
_FILE_NAME = re.compile(
    r"[\w./-]+\.(ya?ml|json|md|txt|toml|py|sh|bash|js|ts|jsx|tsx|go|rs|java|c|cpp|h|rb|php|pl|sql|html|css|csv|"
    r"conf|cfg|ini|xml|env|ipynb|bat|ps1|lua|kt|swift|log|rst|tex)\b",
    re.IGNORECASE,
)
_READ_VERB = re.compile(
    r"\b(show|open|print|display|read|view|cat|explain|describe|summari[sz]e|contents?|look at|what(?:'s| is) in)\b",
    re.IGNORECASE,
)
_DESCRIPTOR = re.compile(r"\b(descriptors?|profiles?|manifests?|ya?ml|deployment)\b", re.IGNORECASE)
_CREATE_CUE = re.compile(
    r"\b(for|need|want|give me|write|generate|make|create|new|draft|prepare|set up)\b", re.IGNORECASE
)

# Whether Hyperion writes files that are not HyperAI descriptors (README, hello.py, notes.txt).
# The mentor's answer (6 Oct): no - only application descriptors (YAML) and folders.
ALLOW_GENERIC_FILES = False

# Words that also have a technical meaning are left out or guarded: "user stories", "speech
# recognition", "resume the workflow", "cv" (computer vision), and the docs' own "cookbook recipes".
_NON_TECHNICAL = re.compile(
    r"\b(poems?|poetry|haikus?|limericks?|sonnets?|(?<!user )stor(?:y|ies)|fairy ?tales?|novels?|songs?|lyrics|"
    r"(?<!cookbook )recipes?|jokes?|riddles?|essays?|(?:love |cover )letters?|letter to|horoscopes?|homework|"
    r"tweets?|blog posts?|diary|journal entry|shopping list|grocery list|biography|trivia|quiz)\b",
    re.IGNORECASE,
)
_YAML_NAME = re.compile(r"\.ya?ml$", re.IGNORECASE)
# general-purpose files and code, named by kind rather than by file name
_GENERIC_ARTIFACT = re.compile(
    r"\b(?:python|bash|shell|powershell|javascript|typescript|java|golang|rust|ruby|php|perl|sql|c\+\+|html|css)\b"
    r"|\b(?:scripts?|functions?|class(?:es)?|programs?|snippets?|algorithms?|readme|dockerfiles?|makefiles?|"
    r"web ?pages?|unit tests?|spreadsheets?|csv|text files?|todo lists?|notes?|cron ?jobs?|regex(?:es)?)\b",
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


def named_files(route: Route, text: str) -> list[str]:
    names = [match.group(0) for match in _FILE_NAME.finditer(text)]
    if route.path and "." in route.path.rsplit("/", 1)[-1] and route.path.lower() in text.lower():
        names.append(route.path)
    return names


def writes_off_topic_content(route: Route, text: str) -> bool:
    """A file request that is not about a HyperAI descriptor.

    Always: content with nothing technical in it ("a file with a poem about cats"). Unless
    ALLOW_GENERIC_FILES: any file that is not YAML, and code or documents named by kind
    ("a bash script", "a README about cooking"). Reading, validating and deleting existing
    files are IDE operations and are not judged here.
    """
    if route.intent not in ("create_file", "edit_file"):
        return False
    if _NON_TECHNICAL.search(f"{text} {route.description or ''}") and "cookbook" not in text.lower():
        return True
    if ALLOW_GENERIC_FILES:
        return False
    if any(not _YAML_NAME.search(name) for name in named_files(route, text)):
        return True
    return bool(_GENERIC_ARTIFACT.search(text)) and not mentions_hyperai(text)


_WHOLE_FOLDER = re.compile(
    r"\b(everything|all (?:the )?(?:files|content|contents)|whole|entire|contents? of)\b.{0,30}\b(in|inside|under|of|from)\b"
    r"|\b(folder|directory|dir)\b",
    re.IGNORECASE,
)


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
    if route.intent == "delete_file" and not _FILE_NAME.search(text) and _WHOLE_FOLDER.search(text):
        # "remove everything in demo": there is no file called demo
        route = route.model_copy(update={"intent": "delete_folder"})
    named = _FILE_NAME.search(text)
    if route.intent in ("question", "off_topic") and named and _READ_VERB.search(text) and not is_injection(text):
        # "explain what demo/redis.yaml does" is about a file in the workspace, not the documentation
        route = route.model_copy(update={"intent": "read_file", "path": route.path or named.group(0)})
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

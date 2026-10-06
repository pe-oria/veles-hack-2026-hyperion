"""Multi-step requests: a cheap gate, then one LLM call that splits the message into steps.

Still a deterministic pipeline: the model only rewrites the message into self-contained
sub-requests; Python runs them one after another through the normal single-request path.
"""

import logging
import re

from hyperion import llm, prompts
from hyperion.session import Session
from hyperion.yamlgen import IMAGE_ALIASES

log = logging.getLogger("hyperion")

MAX_STEPS = 5

_CONNECTOR = re.compile(
    r"\b(and|then|also|plus|after that|afterwards|as well as|followed by|next|finally|once (?:that|it) is done|"
    r"with|holding|containing|that contains|which contains)\b"
    r"|[,;]|\?\s+\S|^\s*\d+[.)]\s",
    re.IGNORECASE | re.MULTILINE,
)
_CREATE = re.compile(
    r"\b(create|make(?! (?:it|this|that|them|sure)\b)|add|generate|write|put(?! (?:it|them|that|this)\b)|build|draft|"
    r"drop|set up|spin up|whip up|scaffold|produce|compose|prepare|give me|get me|"
    r"(?<!n't )(?<!not )(?<!longer )(?:need|want)|(?:i'd|i would|we'd|we would|would) like(?! to\b))\b",
    re.IGNORECASE,
)
_DELETE = re.compile(r"\b(delete|remove|erase|trash|wipe|purge|destroy|clear out|get rid of)\b", re.IGNORECASE)
# several verbs of one of these kinds are still one request: "open it and print it", "set cpu and change memory"
_EDIT = re.compile(
    r"\b(edit|change|update|set(?! up)|modify|fix|bump|switch|rename|adjust|tweak|replace|lower|raise|increase|"
    r"decrease|reduce|double|halve|expose|make (?:it|this|that|them))\b",
    re.IGNORECASE,
)
_VALIDATE = re.compile(r"\b(validate|validates|valid|check|verify)\b", re.IGNORECASE)
_READ = re.compile(r"\b(show|open(?! connectors?)|read|print|display|view|list)\b", re.IGNORECASE)
_MID_QUESTION = re.compile(r"\?\s+\S")  # "Is it valid? If so ..."
# a bare "and" joins nouns too often ("a native app and a device app"): only sequencing words count
_JOINED = re.compile(r"\b(then|also|plus|after that|afterwards|by the way|while you'?re at it)\b", re.IGNORECASE)
# "do the same for mysql": the request only makes sense together with the previous one
_SAME = re.compile(r"\b(the same|same again|same thing|likewise|again for|one more for|another one for|ditto)\b", re.IGNORECASE)
_ASK = re.compile(r"\b(what|how|why|who|which|explain|describe|tell me|summari[sz]e)\b", re.IGNORECASE)
_SEVERAL = re.compile(
    r"\b(two|three|four|five|both|several|multiple|each|a couple of|a few|another|a second|one more|"
    r"ya?mls|files|folders|directories|profiles|descriptors|manifests|deployments|apps|applications)\b(?![/.])",
    re.IGNORECASE,
)
# a folder and a file in one create request are two things: "a folder svc and a yaml in it"
# ... but "put it under the cache directory" only says where the one file goes
_FOLDER_NOUN = re.compile(
    r"(?<![.\w/])(?<!in )(?<!into )(?<!inside )(?<!under )(?<!to )"
    r"(?:(?:the|a|an|my|that|this)\s+)?(?<!in the )(?<!into the )(?<!inside the )(?<!under the )(?<!to the )"
    r"(?:new\s+)?(folder|directory|dir)\b",
    re.IGNORECASE,
)
_DESTINATION = re.compile(r"\b(?:in|into|inside|under|to)\s+(?:(?:the|a|an|my|that|this)\s+)?(?:[\w./-]+\s+)?(?:folder|directory|dir)\b", re.IGNORECASE)
_FILE_NOUN = re.compile(r"(?<![.\w/])(file|ya?ml|profile|descriptor|manifest|deployment)\b", re.IGNORECASE)
_PHASE = re.compile(r"\b(production|testing|development)\b", re.IGNORECASE)
_NUMBER = re.compile(r"\d+(?:\.\d+)*")
_FILE = re.compile(r"[\w./-]+\.(?:ya?ml|json|md|txt|toml|py|sh|conf|cfg|ini|xml)\b", re.IGNORECASE)


def _images_named(text: str) -> int:
    return sum(bool(re.search(rf"(?<![\w./-]){re.escape(alias)}(?![\w/-])", text, re.IGNORECASE)) for alias in IMAGE_ALIASES)


def count_actions(text: str) -> int:
    """How many separate things the wording asks to be done."""
    creating = len(_CREATE.findall(text))
    deleting = len(_DELETE.findall(text))
    return creating + deleting + sum(bool(kind.search(text)) for kind in (_EDIT, _VALIDATE, _READ))


def refers_back(text: str) -> bool:
    """"now do the same for memcached": needs the previous request to mean anything."""
    return bool(_SAME.search(text))


def looks_multi(text: str) -> bool:
    """Whether a message may ask for several things. Cheap, so normal turns pay no extra LLM call.

    True needs a connector AND one of: two actions; an action plus a question; or one create
    with several things to create ("yamls for redis and postgres", "a folder and a yaml in it").
    One file with two settings ("cpu 500m and memory 1Gi") has none of these. A false positive
    only costs one LLM call: `split` then returns the message as a single step.
    """
    if refers_back(text):
        return True
    return needs_splitting(text)


def needs_splitting(text: str) -> bool:
    """The gate proper, without the "refers to the previous request" shortcut."""
    if not _CONNECTOR.search(text):
        return False
    actions = count_actions(text)
    asking = bool(_ASK.search(text) or _MID_QUESTION.search(text))
    if actions >= 2 or (actions >= 1 and asking):
        return True
    if asking and _JOINED.search(text):
        # "explain X and then sing a song": a question with something else attached. The planner
        # separates the two, so the attached part is judged (and refused) on its own.
        return True
    if not (_CREATE.search(text) or _DELETE.search(text)):
        return False
    # one verb, several objects: "yamls for redis and postgres", "remove the folder and the file"
    folder_and_file = _FOLDER_NOUN.search(_DESTINATION.sub(" ", text)) and _FILE_NOUN.search(text)
    return bool(_SEVERAL.search(text) or folder_and_file or _images_named(text) >= 2)


def faithful(steps: list[str], text: str, session: Session) -> bool:
    """Reject a plan that states numbers or file names the user never gave.

    The steps are what the rest of the pipeline trusts as "the user's words", so the model
    must not be able to smuggle in a tag, a port, a path or an image while rewriting.
    """
    previous = (session.last_user_text() or "") if refers_back(text) else ""
    known = " ".join(
        [text, previous, session.last_file or "", session.last_folder or "", *session.files, *session.folders]
    ).lower()
    for step in steps:
        for number in _NUMBER.findall(step):
            if not re.search(rf"(?<![\d.]){re.escape(number)}(?!\d)", known):
                return False
        for name in _FILE.findall(step):
            if name.lower() not in known and name.rsplit("/", 1)[-1].lower() not in known:
                return False
        for alias in IMAGE_ALIASES:
            named = re.search(rf"(?<![\w./-]){re.escape(alias)}(?![\w/-])", step, re.IGNORECASE)
            if named and alias not in known:
                return False
    return True


def without_unstated_phase(step: str, text: str) -> str:
    """Drop "production"/"testing"/"development" the model added on its own: the step is fine without it."""
    def keep(match: re.Match) -> str:
        return match.group(0) if match.group(1).lower() in text.lower() else ""

    return re.sub(r"\b(production|testing|development)\b ?", keep, step, flags=re.IGNORECASE).strip()


def defer_references(steps: list[str], text: str) -> list[str]:
    """In later steps, turn a file name the user did not write back into "the file".

    The model resolves "it" from the context it was given, which is the file from BEFORE this
    message. By the time step 2 runs, step 1 has usually touched the file that is meant, and
    the session resolves "the file" to that one.
    """
    written = text.lower()

    def restore(match: re.Match) -> str:
        name = match.group(0)
        return name if name.lower() in written or name.rsplit("/", 1)[-1].lower() in written else "the file"

    return [steps[0], *(_FILE.sub(restore, step) for step in steps[1:])]


def context_line(session: Session, text: str = "") -> str:
    # the previous request is shown only when this message leans on it; otherwise the model
    # folds it into the plan and would repeat a create or a delete
    previous = (session.last_user_text() or "none")[:300] if refers_back(text) else "none"
    return (
        f"Last file: {session.last_file or 'none'}. Last folder: {session.last_folder or 'none'}.\n"
        f"Previous request: {previous}"
    )


async def split(text: str, session: Session) -> list[str]:
    """Self-contained sub-requests in execution order, or [text] when it is really one request.

    A message that refers back ("the same for mysql") may come back as ONE rewritten step.
    """
    messages = [("system", prompts.PLANNER_SYSTEM)]
    for context, example, answer in prompts.PLANNER_EXAMPLES:
        messages += [("human", prompts.PLANNER_USER.format(context=context, text=example)), ("ai", answer)]
    messages.append(("human", prompts.PLANNER_USER.format(context=context_line(session, text), text=text[:1500])))
    data = await llm.ask_json(messages, max_tokens=400)

    steps = data.get("steps")
    if not isinstance(steps, list) or not all(isinstance(step, str) for step in steps):
        return [text]
    steps = [without_unstated_phase(step, text) for step in steps if step.strip()][:MAX_STEPS]
    steps = [step for step in steps if step]
    # the same step twice is the model stuttering, not the user asking twice
    steps = [step for index, step in enumerate(steps) if step.lower() not in {s.lower() for s in steps[:index]}]
    # one rewritten step is only wanted when the message leans on the previous request
    if not steps or (len(steps) < 2 and not refers_back(text)):
        return [text]
    steps = defer_references(steps, text)
    if not faithful(steps, text, session):
        log.warning("planner added details the user did not give - running the message as one request: %s", steps)
        return [text]
    return steps

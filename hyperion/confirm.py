"""Human-in-the-loop confirmation of destructive actions. Deliberately rule-based: no LLM decides."""

import re
from dataclasses import dataclass

_POLITE = r"(\s*,?\s*(please|thanks|thank you|go ahead|do it|delete it|overwrite it|i am sure|i'm sure|sure))*"
_YES = re.compile(
    r"(yes|y|yeah|yep|yup|sure|ok|okay|confirm|confirmed|i confirm|go ahead|do it|proceed|please do|"
    r"affirmative|absolutely|delete it|overwrite it|si|sí|oui|ja)" + _POLITE,
    re.IGNORECASE,
)
_NO = re.compile(
    r"(no|n|nope|nah|cancel|stop|abort|don'?t|do not|never ?mind|keep it|leave it|wait|negative|non|nein)\b",
    re.IGNORECASE,
)


# With nothing pending only these are unmistakably a stray answer. "delete it" or "do it" count
# as agreement to a question we asked, but on their own they are requests.
_BARE = re.compile(r"(yes|y|yeah|yep|yup|no|n|nope|nah|confirm|confirmed|cancel)(\s+please)?", re.IGNORECASE)


@dataclass(frozen=True)
class Pending:
    """An action waiting for the user's "yes"."""

    action: dict  # the IDE action to emit once confirmed
    question: str  # what we asked, e.g. "Delete `nginx.yaml`?"
    done: str  # what to say after doing it
    validate: bool = False  # whether the written file should go through validation


def classify(text: str) -> str:
    """yes | no | other. Only an unambiguous, whole-message agreement counts as yes."""
    message = text.strip().strip(".!").strip()
    if _YES.fullmatch(message):
        return "yes"
    if _NO.match(message):
        return "no"
    return "other"


def is_bare_answer(text: str) -> bool:
    """Whether the message is nothing but a yes/no, i.e. meaningless without a question."""
    return bool(_BARE.fullmatch(text.strip().strip(".!").strip()))

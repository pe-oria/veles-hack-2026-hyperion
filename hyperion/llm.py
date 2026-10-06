"""LLM clients for the legion1 server (OpenAI-compatible)."""

import json
import logging
import os

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

load_dotenv()
log = logging.getLogger("hyperion")

BASE_URL = "https://legion1.di.uoa.gr/v1"
MODEL = "llama3.1"

API_KEY = os.environ.get("API_KEY", "")
if not API_KEY:
    log.warning("API_KEY is not set - the service starts, but LLM calls will fail")
# the client refuses to be built with an empty key
_KEY = API_KEY or "missing"

chat_llm = ChatOpenAI(
    model=MODEL,
    base_url=BASE_URL,
    api_key=_KEY,
    temperature=0.2,
    max_completion_tokens=1024,
)

json_llm = ChatOpenAI(
    model=MODEL,
    base_url=BASE_URL,
    api_key=_KEY,
    temperature=0,
    max_completion_tokens=200,
    model_kwargs={"response_format": {"type": "json_object"}},
)


async def ask_json(messages: list[tuple[str, str]]) -> dict:
    """Run a JSON-mode completion and return the parsed object ({} if unparsable)."""
    reply = await json_llm.ainvoke(messages)
    try:
        data = json.loads(reply.text)
    except json.JSONDecodeError:
        log.warning("router returned non-JSON: %r", reply.text[:200])
        return {}
    return data if isinstance(data, dict) else {}

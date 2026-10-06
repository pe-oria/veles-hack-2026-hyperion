"""LLM clients for the legion1 server (OpenAI-compatible)."""

import json
import logging
import os

import httpx
import numpy as np
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

load_dotenv()
log = logging.getLogger("hyperion")

BASE_URL = "https://legion1.di.uoa.gr/v1"
MODEL = "llama3.1"
EMBED_MODEL = "nomic-embed-text"
EMBED_BATCH = 32

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

# whole-file rewrites: deterministic, room for a full descriptor
edit_llm = ChatOpenAI(
    model=MODEL,
    base_url=BASE_URL,
    api_key=_KEY,
    temperature=0,
    max_completion_tokens=1500,
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


async def embed(texts: list[str], kind: str) -> np.ndarray:
    """Embed texts as unit vectors. `kind` is the nomic task prefix: search_document | search_query.

    Plain HTTP: the OpenAI SDK always sends `encoding_format`, which legion1 rejects.
    """
    vectors: list[list[float]] = []
    async with httpx.AsyncClient(timeout=60) as client:
        for start in range(0, len(texts), EMBED_BATCH):
            batch = [f"{kind}: {text}" for text in texts[start : start + EMBED_BATCH]]
            response = await client.post(
                f"{BASE_URL}/embeddings",
                headers={"Authorization": f"Bearer {_KEY}"},
                json={"model": EMBED_MODEL, "input": batch},
            )
            response.raise_for_status()
            data = sorted(response.json()["data"], key=lambda item: item["index"])
            vectors.extend(item["embedding"] for item in data)
    matrix = np.asarray(vectors, dtype=np.float32)
    return matrix / np.linalg.norm(matrix, axis=1, keepdims=True)

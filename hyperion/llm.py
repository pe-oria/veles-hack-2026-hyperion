"""LLM clients for the legion1 server (OpenAI-compatible)."""

import asyncio
import json
import logging
import os
import re
from collections.abc import AsyncIterator

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
TIMEOUT = 90  # seconds; the shared server queues requests under load

chat_llm = ChatOpenAI(
    model=MODEL,
    base_url=BASE_URL,
    api_key=_KEY,
    timeout=TIMEOUT,
    max_retries=2,
    temperature=0.2,
    max_completion_tokens=1024,
)

# whole-file rewrites: deterministic, room for a full descriptor
edit_llm = ChatOpenAI(
    model=MODEL,
    base_url=BASE_URL,
    api_key=_KEY,
    timeout=TIMEOUT,
    max_retries=2,
    temperature=0,
    max_completion_tokens=1500,
)

json_llm = ChatOpenAI(
    model=MODEL,
    base_url=BASE_URL,
    api_key=_KEY,
    timeout=TIMEOUT,
    max_retries=2,
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
            data = await _post_embeddings(client, batch)
            vectors.extend(item["embedding"] for item in sorted(data, key=lambda item: item["index"]))
    matrix = np.asarray(vectors, dtype=np.float32)
    return matrix / np.linalg.norm(matrix, axis=1, keepdims=True)


async def _post_embeddings(client: httpx.AsyncClient, batch: list[str], attempts: int = 3) -> list[dict]:
    """One embeddings request, retried on rate limits and transient server errors."""
    for attempt in range(attempts):
        try:
            response = await client.post(
                f"{BASE_URL}/embeddings",
                headers={"Authorization": f"Bearer {_KEY}"},
                json={"model": EMBED_MODEL, "input": batch},
            )
            if response.status_code not in (429, 500, 502, 503, 504):
                response.raise_for_status()
                return response.json()["data"]
            log.warning("embeddings returned %s (attempt %d)", response.status_code, attempt + 1)
        except httpx.TransportError as exc:
            log.warning("embeddings request failed: %s (attempt %d)", exc, attempt + 1)
        if attempt < attempts - 1:
            await asyncio.sleep(0.5 * 2**attempt)
    raise RuntimeError("the embedding server is not responding")


SOURCES_MARK = "Sources:"
_BULLET = re.compile(r"(^|\n)([ \t]*)[*+] ")
_HOLD = len(SOURCES_MARK) - 1  # enough to catch a marker or "**" split across two chunks


def plain(text: str) -> str:
    """The IDE chat shows raw text, so Markdown markup would appear as stray symbols."""
    return _BULLET.sub(r"\1\2- ", text.replace("**", "").replace("`", ""))


async def stream_text(messages: list[tuple[str, str]]) -> AsyncIterator[str]:
    """Stream a chat completion as plain text, dropping a "Sources:" line the model adds itself.

    A few characters are held back so markup split across chunks is still caught.
    """
    held = ""
    async for chunk in chat_llm.astream(messages):
        if not chunk.text:
            continue
        held = plain(held + chunk.text)
        cut = held.find(SOURCES_MARK)
        if cut != -1:
            if held[:cut].rstrip():
                yield held[:cut].rstrip()
            return
        if len(held) > _HOLD:
            yield held[:-_HOLD]
            held = held[-_HOLD:]
    if held.rstrip():
        yield held.rstrip()

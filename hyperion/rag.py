"""RAG over knowledge/*.md: chunk, embed, cache to .cache/index.npz, cosine top-k.

Build the index ahead of time (it is baked into the Docker image):

    uv run python -m hyperion.rag
"""

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from hyperion import llm

log = logging.getLogger("hyperion")

ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = ROOT / "knowledge"
INDEX_PATH = ROOT / ".cache" / "index.npz"

MAX_CHARS = 1100  # target chunk size (~275 tokens); 4 chunks fit easily in the 8k context
MAX_CODE_CHARS = 2400  # YAML examples are only useful whole
TOP_K = 4
CONTEXT_MIN = 0.58  # chunks scoring lower are not shown to the model
CITE_MIN = 0.65  # chunks scoring lower are not listed as sources ...
CITE_MARGIN = 0.06  # ... nor are chunks this far behind the best one
ID_BOOST = 0.15  # embeddings cannot tell "D4.2" from "D4.3"; an exact id match can

_DOC_ID = re.compile(r"\bD\d\.\d\b", re.IGNORECASE)
_FOLLOW_UP = re.compile(r"\b(it|its|they|them|their|that|this|those|these|one|more)\b", re.IGNORECASE)

_HEADING = re.compile(r"^(#{1,4})\s+(.*)$")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class Chunk:
    title: str  # document title, cited in answers
    section: str  # heading path inside the document
    text: str

    def for_embedding(self) -> str:
        return f"{self.title} - {self.section}\n{self.text}" if self.section else f"{self.title}\n{self.text}"


def parse_front_matter(raw: str) -> tuple[dict[str, str], str]:
    if not raw.startswith("---"):
        return {}, raw
    head, _, body = raw[3:].partition("\n---")
    meta = dict(line.split(":", 1) for line in head.strip().splitlines() if ":" in line)
    return {key.strip(): value.strip() for key, value in meta.items()}, body


def split_blocks(body: str, reflow: bool) -> list[tuple[str, str]]:
    """Split a section body into ("code" | "text", content) blocks.

    Outside code fences, the lines of a paragraph are joined (table rows come one cell per
    line). With `reflow` every line is joined: PDF-extracted text has a blank line after each.
    """
    blocks: list[tuple[str, str]] = []
    paragraph: list[str] = []
    code: list[str] | None = None

    def flush() -> None:
        if paragraph:
            blocks.append(("text", " ".join(paragraph)))
            paragraph.clear()

    for line in body.splitlines():
        if line.strip().startswith("```"):
            if code is None:
                flush()
                code = []
            else:
                if any(part.strip() for part in code):
                    blocks.append(("code", "```\n" + "\n".join(code).strip("\n") + "\n```"))
                code = None
        elif code is not None:
            code.append(line)
        elif line.strip():
            paragraph.append(line.strip())
        elif not reflow:
            flush()
    flush()
    return blocks


def split_long(kind: str, content: str) -> list[str]:
    limit = MAX_CODE_CHARS if kind == "code" else MAX_CHARS
    if len(content) <= limit:
        return [content]
    pieces = content.splitlines(keepends=True) if kind == "code" else _SENTENCE_END.split(content)
    joiner = "" if kind == "code" else " "
    parts, current = [], ""
    for piece in pieces:
        if current and len(current) + len(piece) > limit:
            parts.append(current.strip())
            current = ""
        current += piece + joiner
    if current.strip():
        parts.append(current.strip())
    return parts


def pack(blocks: list[tuple[str, str]]) -> list[str]:
    """Greedily pack blocks into chunks of about MAX_CHARS."""
    chunks, current = [], ""
    for kind, content in blocks:
        for part in split_long(kind, content):
            if current and len(current) + len(part) > MAX_CHARS:
                chunks.append(current)
                current = ""
            current = f"{current}\n\n{part}" if current else part
    if current:
        chunks.append(current)
    return chunks


def chunk_document(raw: str, fallback_title: str) -> list[Chunk]:
    meta, body = parse_front_matter(raw)
    title = meta.get("title", fallback_title)
    reflow = "```" not in body and meta.get("source", "").startswith("HYPER-AI deliverable")

    sections: list[tuple[str, list[str]]] = [("", [])]
    path: list[str] = []
    in_code = False
    for line in body.splitlines():
        in_code ^= line.strip().startswith("```")
        match = None if in_code else _HEADING.match(line)
        if match:
            level, heading = len(match.group(1)), match.group(2).strip()
            path = path[: level - 1] + [heading]
            # the H1 repeats the document title
            sections.append((" > ".join(path[1:]), []))
        else:
            sections[-1][1].append(line)

    return [
        Chunk(title=title, section=section, text=text)
        for section, lines in sections
        for text in pack(split_blocks("\n".join(lines), reflow))
    ]


def load_chunks(directory: Path = KNOWLEDGE_DIR) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in sorted(directory.glob("*.md")):
        chunks.extend(chunk_document(path.read_text(encoding="utf-8"), path.stem))
    return chunks


def fingerprint(chunks: list[Chunk]) -> str:
    payload = json.dumps([llm.EMBED_MODEL, [asdict(chunk) for chunk in chunks]], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


class Index:
    def __init__(self, chunks: list[Chunk], vectors: np.ndarray) -> None:
        self.chunks = chunks
        self.vectors = vectors

    def top_k(self, query: np.ndarray, text: str = "", k: int = TOP_K) -> list[tuple[Chunk, float]]:
        scores = self.vectors @ query
        ids = {match.upper() for match in _DOC_ID.findall(text)}
        if ids:
            boost = [ID_BOOST if any(i in chunk.title.upper() for i in ids) else 0.0 for chunk in self.chunks]
            scores = scores + np.asarray(boost, dtype=np.float32)
        best = np.argsort(scores)[::-1][:k]
        return [(self.chunks[i], float(scores[i])) for i in best]


async def build_index() -> Index:
    """Load the cached index, or embed the corpus if it changed (or was never built)."""
    chunks = load_chunks()
    if not chunks:
        raise RuntimeError(f"no documents found in {KNOWLEDGE_DIR}")
    digest = fingerprint(chunks)
    if INDEX_PATH.exists():
        cached = np.load(INDEX_PATH)
        if str(cached["fingerprint"]) == digest:
            return Index(chunks, cached["vectors"])
        log.info("knowledge base changed - rebuilding the index")
    vectors = await llm.embed([chunk.for_embedding() for chunk in chunks], "search_document")
    INDEX_PATH.parent.mkdir(exist_ok=True)
    np.savez_compressed(INDEX_PATH, vectors=vectors, fingerprint=digest)
    log.info("embedded %d chunks -> %s", len(chunks), INDEX_PATH)
    return Index(chunks, vectors)


_index: Index | None = None
_lock = asyncio.Lock()


async def get_index() -> Index:
    global _index
    async with _lock:
        if _index is None:
            _index = await build_index()
    return _index


Hits = list[tuple[Chunk, float]]


async def search(queries: list[str], k: int = TOP_K) -> list[Hits]:
    """Top-k chunks for each query, embedded in one request."""
    index = await get_index()
    vectors = await llm.embed(queries, "search_query")
    return [index.top_k(vector, query, k) for vector, query in zip(vectors, queries)]


async def retrieve(query: str, k: int = TOP_K) -> Hits:
    return (await search([query], k))[0]


def contextual_query(text: str, previous_user_text: str | None) -> str:
    """A follow-up like "and who develops them?" only retrieves well with the previous question."""
    if previous_user_text and (len(text.split()) <= 6 or _FOLLOW_UP.search(text)):
        return f"{previous_user_text[:300]} {text}"
    return text


def format_context(hits: Hits) -> str:
    """Excerpts above CONTEXT_MIN, or "" when nothing is relevant enough.

    Unnumbered on purpose: given "[1]" labels the model echoes them into its answer.
    """
    kept = [chunk for chunk, score in hits if score >= CONTEXT_MIN]
    return "\n\n".join(
        f"### {chunk.title}" + (f" - {chunk.section}" if chunk.section else "") + f"\n{chunk.text}"
        for chunk in kept
    )


def source_titles(hits: Hits) -> list[str]:
    """Distinct document titles worth citing, best first."""
    titles: list[str] = []
    floor = max(CITE_MIN, hits[0][1] - CITE_MARGIN) if hits else CITE_MIN
    for chunk, score in hits:
        if score >= floor and chunk.title not in titles:
            titles.append(chunk.title)
    return titles


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    built = asyncio.run(build_index())
    print(f"index ready: {len(built.chunks)} chunks, {built.vectors.shape[1]}-d")

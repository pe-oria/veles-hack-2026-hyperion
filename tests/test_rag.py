import numpy as np

from hyperion import rag
from hyperion.rag import Chunk, Index

DOC = """---
source: https://example.org/dsl/
title: DSL - Native Apps
---

# Defining Native Applications

Intro paragraph
wrapped over two lines.

## Field Reference

### metadata

 | Field
 | Required

 | name
 | yes

## Example

```
applicationProfile:
  metadata:

    name: demo
```
"""


def test_chunk_document_tracks_title_and_heading_path():
    chunks = rag.chunk_document(DOC, "fallback")
    assert {chunk.title for chunk in chunks} == {"DSL - Native Apps"}
    assert [chunk.section for chunk in chunks] == ["", "Field Reference > metadata", "Example"]
    assert chunks[0].text == "Intro paragraph wrapped over two lines."


def test_table_rows_are_joined_and_code_is_kept_verbatim():
    _, table, code = rag.chunk_document(DOC, "fallback")
    assert table.text == "| Field | Required\n\n| name | yes"
    assert code.text == "```\napplicationProfile:\n  metadata:\n\n    name: demo\n```"


def test_pdf_text_is_reflowed_and_split_on_sentences():
    sentence = "The connectors manage edge devices as cloud\n\nnodes of the cluster. "
    raw = f"---\nsource: HYPER-AI deliverable D4.2\ntitle: D4.2\n---\n\n# D4.2\n\n{sentence * 40}"
    chunks = rag.chunk_document(raw, "fallback")
    assert len(chunks) > 1
    assert all(len(chunk.text) <= rag.MAX_CHARS for chunk in chunks)
    assert all(chunk.text.endswith("cluster.") and "\n" not in chunk.text for chunk in chunks)


def test_real_knowledge_base_chunks_are_sane():
    chunks = rag.load_chunks()
    assert len(chunks) > 50
    assert all(chunk.text.strip() and len(chunk.text) <= rag.MAX_CODE_CHARS + 100 for chunk in chunks)
    assert rag.fingerprint(chunks) == rag.fingerprint(rag.load_chunks())


def make_index() -> Index:
    chunks = [Chunk("Open Connectors (D4.2)", "", "a"), Chunk("Profile Manager (D4.3)", "", "b"), Chunk("Intro", "", "c")]
    return Index(chunks, np.array([[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]], dtype=np.float32))


def test_top_k_orders_by_cosine_score():
    hits = make_index().top_k(np.array([1.0, 0.0], dtype=np.float32), k=2)
    assert [chunk.text for chunk, _ in hits] == ["a", "b"]


def test_top_k_boosts_the_deliverable_named_in_the_question():
    hits = make_index().top_k(np.array([1.0, 0.0], dtype=np.float32), "What does d4.3 cover?", k=1)
    assert hits[0][0].text == "b"


def test_context_and_sources_respect_their_thresholds():
    a, b, c = make_index().chunks
    hits = [(a, 0.80), (b, 0.76), (a, 0.70), (c, 0.60), (c, 0.50)]
    context = rag.format_context(hits)
    assert context.startswith("### Open Connectors (D4.2)\na") and context.count("###") == 4
    # b is within the margin of the best hit; c is above CITE_MIN-ish but too far behind
    assert rag.source_titles(hits) == ["Open Connectors (D4.2)", "Profile Manager (D4.3)"]
    assert rag.source_titles([(c, 0.70), (a, 0.66)]) == ["Intro", "Open Connectors (D4.2)"]
    assert rag.source_titles([(c, 0.60)]) == []
    assert rag.format_context([(c, 0.5)]) == ""


def test_contextual_query_only_extends_follow_ups():
    previous = "What are Open Connectors?"
    assert rag.contextual_query("who develops them?", previous) == f"{previous} who develops them?"
    standalone = "Which fields are required in a native application profile?"
    assert rag.contextual_query(standalone, previous) == standalone
    assert rag.contextual_query("why?", None) == "why?"

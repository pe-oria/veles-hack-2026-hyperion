# Hyperion — agentic assistant for the HyperAI IDE

> Veles Hack 2026 · Challenge 1 · HYPER-AI — Eclipse Foundation · Riding the Edge-to-Cloud Continuum · Valencia, 6–8 Oct 2026

A FastAPI microservice that listens on **port 8000**, exposes `POST /chat` and streams its reply
to the HyperAI IDE as Server-Sent Events.

## Quick start

```bash
cp .env.example .env        # then paste the team API_KEY into .env (never commit it)

uv run python -m hyperion.rag   # embed knowledge/ into .cache/index.npz (once, and after editing knowledge/)

uv run main.py              # local, agent on :8000
docker compose up --build   # or in Docker (the image is the deliverable; it ships the index)

curl localhost:8000/health
curl -N -X POST localhost:8000/chat -H 'Content-Type: application/json' \
  -d '{"user_id":"123e4567-e89b-12d3-a456-426614174000","text":"What is HyperAI?"}'

uv run pytest
```

Multi-arch image for Docker Hub:

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t <dockerhub-user>/hyperion:latest --push .
docker run --rm -p 8000:8000 -e API_KEY=... --add-host host.docker.internal:host-gateway <dockerhub-user>/hyperion:latest
```

## Protocol

Request: `{"user_id": "<uuid>", "text": "<user message>"}`. Response: `text/event-stream`, each
event `data: <json>\n\n` — `{"response": "<incremental text>"}` or an IDE action — ending with
`data: [DONE]`.

## How a message is handled

1. **Router** – one JSON-mode call to `llama3.1` classifies the message (question, file action,
   smalltalk, off-topic) and extracts slots. Python owns the control flow from there.
2. **Guardrail** – off-topic messages get a fixed refusal. The router's verdict is cross-checked
   against the embedding similarity of the message to the knowledge base.
3. **RAG** – questions retrieve the top chunks of `knowledge/` (`nomic-embed-text`, cosine) and are
   answered only from those excerpts; the source documents are appended to the answer.
4. **File actions** – new descriptors are rendered by Python templates from a few parameters the
   LLM extracts, so they are valid by construction. Edits are whole-file rewrites by the LLM; after
   each write the agent waits for the IDE to save the file, runs `validate_file`, and lets the LLM
   repair reported errors (at most twice). Paths are sanitised and must come from the user's words.
5. **Human in the loop** – deleting a file or folder, or overwriting an existing file, is stored
   as a pending action and only runs after an explicit "yes". The reply is judged by rules, not by
   the LLM; anything that is not a clear yes cancels the action.
6. **Memory** – the last turns of each `user_id` are kept and given to the router and the answer.

`scripts/ide_sim.py "<message>" ...` plays the IDE from a terminal: it prints the reply and applies
the agent's actions to a local `ide-backend`, which is what makes validation testable end to end.

`scripts/eval_router.py` and `scripts/eval_rag.py` check the router and retrieval against the live
models; `scripts/fetch_docs.py` refreshes the tutorial pages in `knowledge/`.

## Status

- [x] M0 – starter running locally and as a Docker image (amd64 + arm64)
- [x] M1 – sessions + router + guardrail
- [x] M2 – RAG over `knowledge/`
- [x] M3 – file actions
- [x] M4 – human-in-the-loop confirmation
- [ ] M5 – polish

## License

[Apache 2.0](LICENCE)

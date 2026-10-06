# Hyperion — agentic assistant for the HyperAI IDE

> Veles Hack 2026 · Challenge 1 · HYPER-AI — Eclipse Foundation · Riding the Edge-to-Cloud Continuum · Valencia, 6–8 Oct 2026

A FastAPI microservice that listens on **port 8000**, exposes `POST /chat` and streams its reply
to the HyperAI IDE as Server-Sent Events.

## Quick start

```bash
cp .env.example .env        # then paste the team API_KEY into .env (never commit it)

uv run main.py              # local, agent on :8000
docker compose up --build   # or in Docker (the image is the deliverable)

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

## Status

- [x] M0 – starter running locally and as a Docker image (amd64 + arm64)
- [ ] M1 – sessions + router + guardrail
- [ ] M2 – RAG over `knowledge/`
- [ ] M3 – file actions
- [ ] M4 – human-in-the-loop confirmation
- [ ] M5 – polish

## License

[Apache 2.0](LICENCE)

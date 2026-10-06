# CLAUDE.md — Hyperion agent (Veles Hack 2026 · Challenge 1 · HYPER-AI)

You are the engineer on this project. Read this whole file before writing code.

## 0. Hard facts

- **Deadline: Wed 7 Oct 2026, 16:59 (Europe/Madrid).** Feature freeze Wed 13:00. Pitch is 5 min.
- **Deliverable:** a Docker image `<dockerhub-user>/hyperion:latest` that serves `POST :8000/chat`.
  The organisers run the image and test it against the criteria in §1. They want to confirm the
  image runs early, *before* we write business logic (send the tag to mentor Michael).
- **Repo:** public GitHub/GitLab, **Apache-2.0** licence file required (`LICENCE` from starter), all code
  written during the event.
- **Secrets:** `API_KEY` lives in `.env`. The starter repo *tracks* `.env` — make sure `.env` is in
  `.gitignore` and never pushed. Ship `.env.example` instead.
- Starter: https://gitlab.eclipse.org/eclipse-research-labs/hyper-ai-project/hyperion-starter
  (FastAPI, `uv`, Python ≥ 3.14, `langchain-openai`). Use `uv`, not the old `veles` conda env.

## 1. Evaluation criteria (from the challenge PDF — this is what we're scored on)

1. **Working microservice**: answers questions about HYPER-AI accurately ("What is HyperAI?") and
   turns natural language into IDE actions ("Create a deployment YAML for a service using the nginx
   Docker image" → writes the file and opens it in the editor).
2. **Guardrails**: reject irrelevant queries ("What is the weather today?").
3. **RAG**: answers grounded in HYPER-AI documentation (`knowledge/`).
4. **Memory**: keep context across turns within a session (keyed by `user_id`).
5. *(Optional, do it — cheap points)* **Human-in-the-loop**: confirm before state-changing destructive
   actions (delete file/folder, overwrite an existing file) and only execute after "yes".

## 2. Protocol (IDE ⇄ agent)

Request: `POST /chat` with `{"user_id": "<uuid>", "text": "<user message>"}`.

Response: `text/event-stream`. Each event is `data: <json>\n\n`. Two kinds:

```
data: {"response": "incremental text chunk"}          # appended to chat (increments only!)
data: {"action": "create_file", "path": "demo/nginx.yaml", "content": "..."}
data: [DONE]
```

The IDE prints nothing for actions — stream a text line saying what you did.
Actions and text can interleave; several actions per answer are allowed.

| action | payload | effect |
|---|---|---|
| `create_folder` | `path` | create folder |
| `delete_folder` | `path` | delete folder |
| `create_file` | `path`, `content` | create file **and open it in editor** |
| `edit_file` | `path`, `content` | replace whole file, open it |
| `delete_file` | `path` | delete file |

- `path` is relative to the workspace root; never absolute, never `..`.
- For `edit_file`/`delete_file`/`delete_folder` a bare file name is OK (IDE uses first match).

Backend helpers (in `helpers.py`, HTTP GET to the ide-backend):
- `await read_file(path) -> str` → `GET {IDE_BACKEND_URL}/agent/file?path=` (404 not found,
  409 ambiguous → `matches` list; ask the user which one).
- `await validate_file(path) -> dict` → `GET {IDE_BACKEND_URL}/agent/validation/file?path=` returns
  `{path, type: native|device, valid, errors[{line,column,field,message}], warnings[...]}`.
- `IDE_BACKEND_URL` = `http://localhost:3001/api` locally, `http://host.docker.internal:3001/api` in Docker.
- There is **no "list files" endpoint**. Track files we created per session ourselves.
- CORS middleware in `main.py` must stay (browser calls the agent directly).

## 3. LLM server (legion1)

- `https://legion1.di.uoa.gr/v1`, OpenAI-compatible, key in `.env` (`API_KEY=`), one key per team.
- Chat: `llama3.1` = Llama 3.1 **8B** Instruct Q4, **8192-token context**, tool calling, streaming.
- Embeddings: `nomic-embed-text` (768-d), `mxbai-embed-large` (1024-d).
- Rate limits may apply under load → keep prompts small, cache embeddings to disk.

**Design consequence:** an 8B model with 8k context is unreliable at free-form multi-step tool use.
Do NOT build an open-ended ReAct loop. Build a **deterministic pipeline** where the LLM does narrow,
well-prompted jobs (classify → retrieve → generate) and Python does the control flow.

## 4. Architecture

```
POST /chat ─► Session(user_id)  ──────────────────────────────────────────────┐
               │ history (last ~6 turns), pending_action, last_file, files[]  │
               ▼                                                              │
         1. pending confirmation?  yes/no → execute or cancel ───────────────┤
               ▼                                                              │
         2. Router (LLM, JSON mode, few-shot, temperature 0)                  │
            intent ∈ {question, create_file, edit_file, delete_file,          │
                      create_folder, delete_folder, validate_file,            │
                      read_file, smalltalk, off_topic}                        │
            + slots {path, app_kind: native|device, image, description}       │
               ▼                                                              │
         3. Guardrail: off_topic → fixed polite refusal (no LLM call)         │
            (router + cheap embedding-similarity check vs. knowledge base)    │
               ▼                                                              │
         4a. question → RAG: embed query → top-k chunks → grounded answer,    │
             streamed, cite source titles; "I don't know" if no context        │
         4b. create/edit file → generate YAML from DSL template + few-shot     │
             cookbook example → yaml.safe_load check → emit action →           │
             validate_file → if errors: LLM fix pass → edit_file (max 2 loops) │
         4c. delete / overwrite → store pending_action, ask "Confirm? (yes/no)"│
               ▼                                                              │
         5. stream text + actions as SSE; append turn to history ◄────────────┘
```

Module layout (keep it small):

```
main.py            FastAPI app, /chat, /health, SSE helpers, CORS
hyperion/
  llm.py           ChatOpenAI + embeddings clients (legion1), JSON-mode helper
  session.py       in-memory store keyed by user_id (history, pending_action, files)
  router.py        intent + slot extraction prompt (few-shot), pydantic schema
  guardrails.py    off-topic detection + refusal text
  rag.py           chunk knowledge/*.md, embed, cache to .cache/index.npz, cosine top-k
  yamlgen.py       native/device templates, generation, validate+repair loop
  actions.py       build action events, path sanitisation (relative, no '..')
  prompts.py       all prompt strings in one place
knowledge/         RAG corpus (deliverables + tutorial pages)
tests/             pytest: protocol, router fixtures, path sanitiser, SSE format
scripts/fetch_docs.py  downloads tutorial pages into knowledge/
```

## 5. HyperAI DSL — what generated YAML must look like

Full spec + examples are in `knowledge/dsl_native_apps.md`, `knowledge/dsl_device_apps.md`,
`knowledge/cookbook_examples.md` (run `scripts/fetch_docs.py` first). Summary:

**Native app (application profile)** — root `applicationProfile:` with
`metadata` {type: "native", schemaVersion: "1.1.0", name, version, owner, lifecyclePhase
(development|testing|production), description?, annotations?} and `specs` {runtime {executionType
container|vm, entryPoint, args[], baseOS{name,version}, containerImage{uri,tag}}, resources {cpu "2000m",
memory "1Gi", storage "1Gi"}, network {ports[{port, protocol, publicExposure?}], protocols?},
constraints {supportedArchitectures[]...}, qos {...}?}.

**Device app (K8s CR)** — `apiVersion: hyper.ai/v1`, `kind: Application`, `metadata{name, annotations}`,
`spec{device_name?, app{type: device, schemaVersion "1.0.0", name, version, owner, lifecyclePhase},
workload{kind: DockerImage|AndroidApk|esp32Binary + matching block}, exec?, network{ports[],
networkBandwidthMin{value,unit}}, qos{latencyToleranceMax, energyCost, monetaryCost, resilience,
availability, startupTime — all required, value/unit objects}, constraints{schedulingPriority,
supportedArchitectures, geoLocationRequirement, isHighlyAvailable, faultTolerance, dataClassification}}`.

Default rule: "deployment / service / web server / container" → **native** profile unless the user says
device / edge device / Android / ESP32. Always use the cookbook examples as few-shot templates and let
`validate_file` be the judge.

## 6. Milestones (do in order; commit after each)

1. **M0 – image verified (≤30 min).** Copy starter files into this repo root (`main.py`, `helpers.py`,
   `Dockerfile`, `docker-compose.yaml`, `pyproject.toml`, `uv.lock`, `.python-version`, `.dockerignore`,
   `LICENCE`). Fix `.gitignore` (`.env`). `uv run main.py`, curl test, `docker compose up --build`,
   tag + push `<user>/hyperion:latest`. On Apple Silicon build with `--platform linux/amd64` too
   (`docker buildx build --platform linux/amd64,linux/arm64 -t <user>/hyperion:latest --push .`).
2. **M1 – sessions + router + guardrail.** Off-topic refused; history kept per `user_id`.
3. **M2 – RAG.** Index `knowledge/`, grounded answers with source titles. "What is HyperAI?" good.
4. **M3 – file actions.** create_folder / create_file (YAML gen) / edit_file (read_file → modify →
   edit) / delete. nginx example works end-to-end in the IDE and passes `validate_file`.
5. **M4 – HITL.** Confirmation for delete + overwrite; "yes"/"no"/other handled.
6. **M5 – polish.** Streaming feels live (stream status lines while working), error messages friendly,
   README with architecture diagram + demo GIF, final image pushed.

## 7. Local dev

```bash
# IDE (two terminals; Apple Silicon needs --platform linux/amd64)
docker run --rm --platform linux/amd64 -p 3001:3001 -e AUTH_ENABLED=false --name ide-backend donmichael/ide-backend:latest
docker run --rm --platform linux/amd64 -p 5000:80 --name ide-gui donmichael/ide-gui:latest
# open http://localhost:5000 → Hyperion (robot icon)

uv run main.py                       # agent on :8000
curl -N -X POST localhost:8000/chat -H 'Content-Type: application/json' \
  -d '{"user_id":"123e4567-e89b-12d3-a456-426614174000","text":"What is HyperAI?"}'
uv run pytest
```

Acceptance prompts (keep them in `tests/acceptance.md` and run them before every push):
- "What is HyperAI?" / "What are Open Connectors?" / "What is a DeviceNode?" → grounded answers
- "What is the weather today?" / "Write me a poem about pizza" → refusal
- "Create a folder called demo" → create_folder
- "Create a deployment YAML for a service using the nginx Docker image" → create_file + valid
- "Change the memory to 2Gi" (follow-up, no file named) → uses session `last_file`, edit_file
- "Delete it" → asks confirmation → "yes" → delete_file
- "Validate nginx.yaml" → summarises validate_file report

## 8. Conventions

- Python, type hints, async everywhere in request path. Keep functions small; prompts in `prompts.py`.
- Never block the event loop (use `ainvoke`/`astream`, `httpx.AsyncClient`).
- Every SSE `response` chunk is an increment. Always end with `data: [DONE]`.
- Log each turn (intent, latency, actions) to stdout — handy for the pitch.
- Prefer working end-to-end over clever. Run tests before each commit.

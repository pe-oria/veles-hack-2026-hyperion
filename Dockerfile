FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen --no-install-project

COPY main.py helpers.py ./
COPY hyperion ./hyperion
COPY knowledge ./knowledge
# prebuilt embedding index (uv run python -m hyperion.rag); rebuilt at startup if stale
COPY .cache ./.cache

ENV IDE_BACKEND_URL=http://host.docker.internal:3001/api

EXPOSE 8000

CMD ["uv", "run", "--no-sync", "main.py"]

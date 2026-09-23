# FastAPI runtime image: python:3.12-slim + uv, dependencies installed from the
# committed uv.lock, the app served by uvicorn on $PORT (default 8000).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first so the layer is cached until the lock changes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY {{cookiecutter.agent_directory}} ./{{cookiecutter.agent_directory}}
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# The outbound API policy travels with the image: without it the API client
# refuses every call. The glob copies api-policy.yaml when the project has one
# and nothing otherwise (pyproject.toml is repeated only so the COPY always has
# a source).
COPY pyproject.toml api-policy.yam[l] ./

ENV PATH="/app/.venv/bin:$PATH" \
    HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000

CMD ["sh", "-c", "exec uvicorn {{cookiecutter.agent_directory}}.fast_api_app:app --host ${HOST:-0.0.0.0} --port ${PORT:-8000}"]

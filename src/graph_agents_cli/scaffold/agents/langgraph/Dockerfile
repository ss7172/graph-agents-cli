# FastAPI runtime image (base: the official python:3.12-slim line, pinned to an
# exact release). Two stages: the builder installs the dependencies from the
# committed uv.lock with a pinned uv; the final image holds only the
# virtualenv and the app (no uv), runs as uid/gid 1000 and writes nothing
# outside /tmp, so it runs with a read-only root filesystem.
#
# The tags below are exact releases. For fully reproducible builds pin them by
# digest as well, e.g. `python:3.12.14-slim-bookworm@sha256:<digest>`
# (`docker buildx imagetools inspect <image>` prints the digest); bump them
# deliberately, e.g. for security updates of the base image.
ARG PYTHON_IMAGE=python:3.12.14-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.18

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first so the layer is cached until the lock changes.
COPY pyproject.toml uv.lock ./
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY {{cookiecutter.agent_directory}} ./{{cookiecutter.agent_directory}}
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev \
 && python -m compileall -q {{cookiecutter.agent_directory}}

FROM ${PYTHON_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    PATH="/app/.venv/bin:$PATH" \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app
COPY --from=builder /app /app

# The outbound API policy travels with the image: without it the API client
# refuses every call. The glob copies api-policy.yaml when the project has one
# and nothing otherwise (pyproject.toml is repeated only so the COPY always has
# a source). --chmod makes it readable by uid 1000 whatever its mode in the
# working tree (an unreadable policy also refuses every call).
COPY --chmod=0644 pyproject.toml api-policy.yam[l] ./

USER 1000:1000

EXPOSE 8000

CMD ["sh", "-c", "exec uvicorn {{cookiecutter.agent_directory}}.fast_api_app:app --host ${HOST:-0.0.0.0} --port ${PORT:-8000}"]

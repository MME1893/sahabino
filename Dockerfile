FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /uvx /bin/

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

COPY pyproject.toml uv.lock ./

RUN --mount=type=cache,id=sahabino-uv-cache,target=/root/.cache/uv,sharing=locked \
    uv sync --frozen --no-dev --no-install-project

COPY README.md alembic.ini ./
COPY migrations ./migrations
COPY src ./src

RUN --mount=type=cache,id=sahabino-uv-cache,target=/root/.cache/uv,sharing=locked \
    uv sync --frozen --no-dev

EXPOSE 8000

CMD ["uv","run","--no-sync","uvicorn","sahabino.main:app","--host","0.0.0.0","--port","8000"]

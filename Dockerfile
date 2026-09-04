FROM python:3.12-slim

RUN python -m pip install --no-cache-dir uv==0.12.1

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY . .

RUN uv sync --frozen --no-dev

EXPOSE 8000

CMD ["uv", "run", "--no-sync", "uvicorn", "sahabino.main:app", "--host", "0.0.0.0", "--port", "8000"]

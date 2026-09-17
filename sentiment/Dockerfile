FROM python:3.11-slim

WORKDIR /worker

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_CACHE=/models/hf-hub \
    SENTIMENT_MODEL_CACHE=/models/hf-hub

COPY sentiment/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY sentiment/__init__.py sentiment/contracts.py sentiment/inference.py sentiment/worker.py ./sentiment/

RUN useradd --create-home --uid 10001 worker
USER worker

ENTRYPOINT ["python", "-m", "sentiment.worker"]
CMD ["--once"]

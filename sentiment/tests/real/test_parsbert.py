from __future__ import annotations

import os

import pytest

from sentiment.inference import SentimentInference

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_SENTIMENT_MODEL_SMOKE") != "1",
    reason="set RUN_SENTIMENT_MODEL_SMOKE=1 with the pinned model cache mounted",
)


def test_real_persian_and_english_inference() -> None:
    inference = SentimentInference(
        cache_dir=os.environ["SENTIMENT_MODEL_CACHE"],
        minimum_language_confidence=0.0,
    )

    results = inference.analyze_batch(
        ["این برنامه خیلی خوب و کاربردی است", "This application is excellent"]
    )

    assert [result.status for result in results] == ["done", "done"]  # type: ignore[union-attr]
    assert [result.language for result in results] == ["fa", "en"]  # type: ignore[union-attr]

from __future__ import annotations

from dataclasses import dataclass

from sentiment.inference import SentimentInference, detect_supported_language


@dataclass(frozen=True)
class Candidate:
    lang: str
    prob: float


class FakePersian:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def predict_batch(self, texts: list[str]) -> list[str]:
        self.calls.append(list(texts))
        return ["positive" if "خوب" in text else "negative" for text in texts]


class FakeEnglish:
    def polarity_scores(self, text: str) -> dict[str, float]:
        scores = {"great app": 0.05, "bad app": -0.05, "plain app": 0.0}
        return {"compound": scores[text]}


def _detector(text: str) -> list[Candidate]:
    if "خوب" in text or "افتضاح" in text:
        return [Candidate("fa", 0.99)]
    if text == "bonjour monde":
        return [Candidate("fr", 0.99)]
    return [Candidate("en", 0.99)]


def test_language_policy_skips_empty_emoji_short_unsupported_and_uncertain_text() -> None:
    def detector(_: str) -> list[Candidate]:
        return [Candidate("en", 0.84)]

    assert detect_supported_language(None, detector=detector) is None
    assert detect_supported_language("   ", detector=detector) is None
    assert detect_supported_language("🙂🎉", detector=detector) is None
    assert detect_supported_language("ok", detector=detector) is None
    assert (
        detect_supported_language("bonjour monde", detector=lambda _: [Candidate("fr", 0.99)])
        is None
    )
    assert detect_supported_language("uncertain words", detector=detector) is None


def test_language_policy_accepts_reliable_persian_english_and_mixed_text() -> None:
    assert detect_supported_language("برنامه خیلی خوب", detector=_detector) == "fa"
    assert detect_supported_language("great application", detector=_detector) == "en"
    assert detect_supported_language("این app خیلی خوب", detector=_detector) == "fa"


def test_seeded_langdetect_recognizes_clear_persian_and_english() -> None:
    assert (
        detect_supported_language("This application is really useful and works very well every day")
        == "en"
    )
    assert detect_supported_language("این برنامه بسیار خوب و کاربردی است و درست کار می‌کند") == "fa"


def test_inference_skips_empty_and_unsupported_without_fabricating_neutral() -> None:
    persian = FakePersian()
    inference = SentimentInference(
        cache_dir="unused",
        persian=persian,
        english=FakeEnglish(),  # type: ignore[arg-type]
        detector=_detector,
    )

    results = inference.analyze_batch(["", "🙂", "bonjour monde"])

    assert [(result.status, result.label) for result in results] == [  # type: ignore[union-attr]
        ("skipped", None),
        ("skipped", None),
        ("skipped", None),
    ]
    assert persian.calls == []


def test_vader_thresholds_and_bounded_persian_batches() -> None:
    persian = FakePersian()
    inference = SentimentInference(
        cache_dir="unused",
        model_batch_size=1,
        persian=persian,
        english=FakeEnglish(),  # type: ignore[arg-type]
        detector=_detector,
    )

    results = inference.analyze_batch(
        ["great app", "bad app", "plain app", "خیلی خوب", "واقعا افتضاح"]
    )

    assert [result.label for result in results] == [  # type: ignore[union-attr]
        "positive",
        "negative",
        "neutral",
        "positive",
        "negative",
    ]
    assert persian.calls == [["خیلی خوب"], ["واقعا افتضاح"]]


def test_detection_exception_is_skipped() -> None:
    def broken_detector(_: str) -> list[Candidate]:
        raise UnicodeError("bad input")

    assert detect_supported_language("valid length", detector=broken_detector) is None

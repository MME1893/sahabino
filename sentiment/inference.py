from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from typing import Any, Literal, Protocol, cast

from langdetect import DetectorFactory, detect_langs  # type: ignore[import-untyped]
from vaderSentiment.vaderSentiment import (  # type: ignore[import-untyped]
    SentimentIntensityAnalyzer,
)

from .contracts import AnalysisResult

DetectorFactory.seed = 0

MODEL_ID = "HooshvareLab/bert-fa-base-uncased-sentiment-deepsentipers-multi"
MODEL_REVISION = "345654b9c84217afec8742bbe1c1bf94e5ac2b5b"
MAX_LENGTH = 256
SUPPORTED_LANGUAGES = frozenset({"fa", "en"})


class ModelUnavailableError(RuntimeError):
    """The pinned model cannot be loaded from the configured offline cache."""


class PersianPredictor(Protocol):
    def predict_batch(self, texts: Sequence[str]) -> Sequence[str]: ...


def detect_supported_language(
    content: str | None,
    *,
    minimum_confidence: float = 0.85,
    detector: Callable[[str], Sequence[Any]] = detect_langs,
) -> Literal["fa", "en"] | None:
    if content is None:
        return None
    text = content.strip()
    if sum(character.isalpha() for character in text) < 3:
        return None
    try:
        candidates = list(detector(text))
    except Exception:
        return None
    if not candidates:
        return None
    candidate = max(candidates, key=lambda item: float(item.prob))
    language = str(candidate.lang)
    if language not in SUPPORTED_LANGUAGES or float(candidate.prob) < minimum_confidence:
        return None
    return cast(Literal["fa", "en"], language)


class PersianSentimentModel:
    def __init__(self, *, cache_dir: str) -> None:
        try:
            import torch  # type: ignore[import-not-found]
            from transformers import (  # type: ignore[import-not-found]
                AutoModelForSequenceClassification,
                AutoTokenizer,
            )
        except ImportError as error:  # pragma: no cover - packaging failure
            raise ModelUnavailableError("Persian model dependencies are not installed") from error

        try:
            self._tokenizer = AutoTokenizer.from_pretrained(
                MODEL_ID,
                revision=MODEL_REVISION,
                cache_dir=cache_dir,
                local_files_only=True,
                trust_remote_code=False,
            )
            self._model = AutoModelForSequenceClassification.from_pretrained(
                MODEL_ID,
                revision=MODEL_REVISION,
                cache_dir=cache_dir,
                local_files_only=True,
                trust_remote_code=False,
                use_safetensors=True,
            )
        except (OSError, ValueError) as error:
            raise ModelUnavailableError(
                f"pinned Persian model revision is missing or invalid in offline cache {cache_dir}"
            ) from error

        self._torch = torch
        self._model.to("cpu")
        self._model.eval()
        if int(self._model.config.num_labels) != 5:
            raise ModelUnavailableError(
                "pinned Persian model does not expose five sentiment labels"
            )
        id_to_label = {
            int(class_id): str(label).strip().lower()
            for class_id, label in self._model.config.id2label.items()
        }
        expected_labels = {"furious", "angry", "neutral", "happy", "delighted"}
        if set(id_to_label.values()) != expected_labels:
            raise ModelUnavailableError(
                "pinned Persian model sentiment label mapping is unexpected"
            )
        self._class_ids = {
            "negative": tuple(
                class_id for class_id, label in id_to_label.items() if label in {"furious", "angry"}
            ),
            "neutral": tuple(
                class_id for class_id, label in id_to_label.items() if label == "neutral"
            ),
            "positive": tuple(
                class_id
                for class_id, label in id_to_label.items()
                if label in {"happy", "delighted"}
            ),
        }

    def predict_batch(self, texts: Sequence[str]) -> list[str]:
        if not texts:
            return []
        encoded = self._tokenizer(
            list(texts),
            padding=True,
            truncation=True,
            max_length=MAX_LENGTH,
            return_tensors="pt",
        )
        encoded = {name: tensor.to("cpu") for name, tensor in encoded.items()}
        with self._torch.inference_mode():
            logits = self._model(**encoded).logits
            probabilities = self._torch.softmax(logits, dim=-1).tolist()

        labels: list[str] = []
        for scores in probabilities:
            three_class_scores = {
                label: sum(scores[class_id] for class_id in class_ids)
                for label, class_ids in self._class_ids.items()
            }
            labels.append(max(three_class_scores, key=three_class_scores.__getitem__))
        return labels


class SentimentInference:
    def __init__(
        self,
        *,
        cache_dir: str,
        model_batch_size: int = 8,
        minimum_language_confidence: float = 0.85,
        persian: PersianPredictor | None = None,
        english: SentimentIntensityAnalyzer | None = None,
        detector: Callable[[str], Sequence[Any]] = detect_langs,
    ) -> None:
        if model_batch_size < 1:
            raise ValueError("model_batch_size must be positive")
        if not 0.0 <= minimum_language_confidence <= 1.0:
            raise ValueError("minimum_language_confidence must be between zero and one")
        self._model_batch_size = model_batch_size
        self._minimum_language_confidence = minimum_language_confidence
        self._detector = detector
        self._persian = persian or PersianSentimentModel(cache_dir=cache_dir)
        self._english = english or SentimentIntensityAnalyzer()

    def analyze_batch(self, contents: Sequence[str | None]) -> list[AnalysisResult | Exception]:
        outcomes: list[AnalysisResult | Exception | None] = [None] * len(contents)
        persian_items: list[tuple[int, str]] = []

        for index, content in enumerate(contents):
            language = detect_supported_language(
                content,
                minimum_confidence=self._minimum_language_confidence,
                detector=self._detector,
            )
            if language is None:
                outcomes[index] = AnalysisResult(language=None, label=None, status="skipped")
                continue
            assert content is not None
            if language == "en":
                try:
                    compound = float(self._english.polarity_scores(content)["compound"])
                    english_label: Literal["positive", "neutral", "negative"]
                    if compound >= 0.05:
                        english_label = "positive"
                    elif compound <= -0.05:
                        english_label = "negative"
                    else:
                        english_label = "neutral"
                    outcomes[index] = AnalysisResult(
                        language="en", label=english_label, status="done"
                    )
                except Exception as error:  # one malformed item must not block its peers
                    outcomes[index] = error
                continue
            persian_items.append((index, content))

        for start in range(0, len(persian_items), self._model_batch_size):
            chunk = persian_items[start : start + self._model_batch_size]
            texts = [content for _, content in chunk]
            try:
                labels = list(self._persian.predict_batch(texts))
                if len(labels) != len(chunk):
                    raise RuntimeError("Persian model returned an unexpected batch size")
                for (index, _), raw_label in zip(chunk, labels, strict=True):
                    outcomes[index] = AnalysisResult(
                        language="fa",
                        label=cast(Literal["positive", "neutral", "negative"], raw_label),
                        status="done",
                    )
            except Exception:
                self._analyze_persian_individually(chunk, outcomes)

        return [
            outcome if outcome is not None else RuntimeError("missing inference outcome")
            for outcome in outcomes
        ]

    def _analyze_persian_individually(
        self,
        items: Sequence[tuple[int, str]],
        outcomes: list[AnalysisResult | Exception | None],
    ) -> None:
        for index, content in items:
            try:
                labels = list(self._persian.predict_batch([content]))
                if len(labels) != 1:
                    raise RuntimeError("Persian model returned an unexpected batch size")
                outcomes[index] = AnalysisResult(
                    language="fa",
                    label=cast(Literal["positive", "neutral", "negative"], labels[0]),
                    status="done",
                )
            except Exception as error:
                outcomes[index] = error


def build_inference_from_environment() -> SentimentInference:
    return SentimentInference(
        cache_dir=os.getenv("SENTIMENT_MODEL_CACHE", "/srv/sahabino-models/hf-hub"),
        model_batch_size=int(os.getenv("SENTIMENT_MODEL_BATCH_SIZE", "8")),
        minimum_language_confidence=float(os.getenv("SENTIMENT_MIN_LANGUAGE_CONFIDENCE", "0.85")),
    )

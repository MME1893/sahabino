from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class AnalysisResult:
    language: Literal["fa", "en"] | None
    label: Literal["positive", "neutral", "negative"] | None
    status: Literal["done", "skipped"]

    def __post_init__(self) -> None:
        if self.language not in {None, "fa", "en"}:
            raise ValueError("sentiment language must be fa, en, or null")
        if self.label not in {None, "positive", "neutral", "negative"}:
            raise ValueError("sentiment label is invalid")
        if self.status == "done" and (self.language is None or self.label is None):
            raise ValueError("done sentiment results require a supported language and label")
        if self.status == "skipped" and self.label is not None:
            raise ValueError("skipped sentiment results cannot have a label")


class SentimentAnalyzer(Protocol):
    def analyze_batch(
        self, contents: Sequence[str | None]
    ) -> Sequence[AnalysisResult | Exception]: ...

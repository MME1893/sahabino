from sahabino.crawler.domain.errors import AdapterFailure, ParseFailure, SchemaFailure


class AdapterFallbackPolicy:
    def __init__(self, *, secondary_enabled: bool = True) -> None:
        self._secondary_enabled = secondary_enabled

    def should_use_secondary(self, error: BaseException) -> bool:
        return self._secondary_enabled and isinstance(
            error, (ParseFailure, SchemaFailure, AdapterFailure)
        )

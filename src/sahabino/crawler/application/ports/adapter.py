from __future__ import annotations

from typing import Protocol

from sahabino.crawler.application.ports.proxy import ProxyLeasePort
from sahabino.crawler.domain.dto import AdapterCapabilities, AppDetailsDTO, ReviewsDTO


class PlayStoreAdapter(Protocol):
    @property
    def capabilities(self) -> AdapterCapabilities: ...

    def get_app(
        self,
        package_name: str,
        language_code: str,
        country_code: str,
    ) -> AppDetailsDTO: ...

    def get_reviews(
        self,
        package_name: str,
        language_code: str,
        country_code: str,
        limit: int,
    ) -> ReviewsDTO: ...

    def close(self) -> None: ...


class PrimaryAdapterFactoryPort(Protocol):
    def create(self, context_id: str, lease: ProxyLeasePort) -> PlayStoreAdapter: ...

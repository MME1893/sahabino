from typing import Protocol

from sahabino.crawler.domain.dto import ApplicationRef


class ApplicationRegistryPort(Protocol):
    def list_active_applications(self) -> list[ApplicationRef]: ...

    def close(self) -> None: ...

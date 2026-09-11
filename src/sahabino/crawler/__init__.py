"""Standalone Google Play crawler subsystem."""

from uuid import UUID

from sahabino.crawler.application.executor import CrawlerService
from sahabino.crawler.domain.results import TriggerType


def crawl_once(crawler: CrawlerService) -> UUID:
    """Run one manual crawl without coupling the use case to its scheduler."""
    return crawler.crawl_once(TriggerType.MANUAL)


__all__ = ["crawl_once"]

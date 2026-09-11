from uuid import UUID

from sahabino.crawler.application.executor import CrawlerService
from sahabino.crawler.domain.results import TriggerType


def crawl_once(crawler: CrawlerService) -> UUID:
    return crawler.crawl_once(TriggerType.MANUAL)

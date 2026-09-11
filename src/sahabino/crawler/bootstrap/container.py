from __future__ import annotations

from dataclasses import dataclass

from sahabino.common.config import Settings, get_settings
from sahabino.crawler.application.client import ResilientPlayStoreClient
from sahabino.crawler.application.executor import CrawlerService
from sahabino.crawler.application.policies.adapter import AdapterFallbackPolicy
from sahabino.crawler.application.policies.network import NetworkPolicy
from sahabino.crawler.application.policies.retry import RetryPolicy
from sahabino.crawler.application.ports.proxy import ProxyProvider
from sahabino.crawler.application.tasks import ApplicationCrawlCommand
from sahabino.crawler.infrastructure.adapters.classifier import ErrorClassifier
from sahabino.crawler.infrastructure.adapters.factory import PrimaryAdapterFactory
from sahabino.crawler.infrastructure.adapters.google_play import GooglePlayScraperAdapter
from sahabino.crawler.infrastructure.messaging.kafka import KafkaCollectedEventPublisher
from sahabino.crawler.infrastructure.persistence.repository import (
    SqlAlchemyLifecycleRepository,
)
from sahabino.crawler.infrastructure.proxy.pool import ProxyPool
from sahabino.crawler.infrastructure.proxy.providers import NoProxyProvider, PoolProxyProvider
from sahabino.crawler.infrastructure.registry.http import HttpApplicationRegistry
from sahabino.crawler.infrastructure.resilience.circuit_breaker import CircuitBreaker
from sahabino.crawler.infrastructure.resilience.clock import SystemClock
from sahabino.crawler.infrastructure.resilience.token_bucket import (
    NoOpRateLimiter,
    TokenBucketRateLimiter,
)
from sahabino.crawler.scheduler.scheduler import CrawlerScheduler
from sahabino.db.sync_session import create_sync_session_factory
from sahabino.messaging.admin import ensure_topics
from sahabino.messaging.producer import KafkaProducer
from sahabino.messaging.topics import SAHABINO_TOPICS, TopicTopology


@dataclass(slots=True)
class CrawlerContainer:
    crawler: CrawlerService
    scheduler: CrawlerScheduler
    registry: HttpApplicationRegistry
    publisher: KafkaCollectedEventPublisher

    def close(self) -> None:
        try:
            self.registry.close()
        finally:
            self.publisher.close()

    def __enter__(self) -> CrawlerContainer:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def build_container(
    settings: Settings | None = None,
    *,
    provision_topics: bool = False,
) -> CrawlerContainer:
    settings = settings or get_settings()
    clock = SystemClock()
    session_factory = create_sync_session_factory(settings.database_url)
    lifecycle = SqlAlchemyLifecycleRepository(session_factory)
    registry = HttpApplicationRegistry(
        settings.application_registry_base_url,
        clock=clock,
    )
    rate_limiter = (
        TokenBucketRateLimiter(
            settings.playstore_rate_limit_refill_per_second,
            settings.playstore_rate_limit_burst_capacity,
            clock=clock,
        )
        if settings.playstore_rate_limit_enabled
        else NoOpRateLimiter()
    )
    proxy_provider: ProxyProvider
    if settings.playstore_proxy_enabled:
        proxy_provider = PoolProxyProvider(
            ProxyPool(
                [value.get_secret_value() for value in settings.playstore_proxy_urls],
                failure_threshold=settings.playstore_proxy_failure_threshold,
                cooldown_seconds=settings.playstore_proxy_cooldown_seconds,
                direct_fallback=settings.playstore_proxy_direct_fallback,
                clock=clock,
            )
        )
    else:
        proxy_provider = NoProxyProvider()

    network_policy = NetworkPolicy(
        proxy_provider,
        rate_limit_rotate_after=settings.playstore_proxy_rate_limit_rotate_after,
    )
    retry_policy = RetryPolicy(
        settings.playstore_retry_max_attempts,
        settings.playstore_retry_max_delay_seconds,
        clock=clock,
    )
    circuit_breaker = CircuitBreaker(
        settings.playstore_circuit_breaker_failure_threshold,
        settings.playstore_circuit_breaker_cooldown_seconds,
        enabled=settings.playstore_circuit_breaker_enabled,
        clock=clock,
    )
    primary_factory = PrimaryAdapterFactory(
        rate_limiter,
        timeout_seconds=settings.playstore_request_timeout_seconds,
    )
    playstore = ResilientPlayStoreClient(
        proxy_provider=proxy_provider,
        primary_factory=primary_factory,
        secondary_adapter=GooglePlayScraperAdapter(),
        retry_policy=retry_policy,
        network_policy=network_policy,
        fallback_policy=AdapterFallbackPolicy(
            secondary_enabled=settings.playstore_secondary_adapter_enabled
        ),
        circuit_breaker=circuit_breaker,
        classifier=ErrorClassifier(),
    )
    if provision_topics:
        ensure_topics(
            settings.kafka_bootstrap_servers,
            SAHABINO_TOPICS,
            TopicTopology(
                partitions=settings.kafka_topic_partitions,
                replication_factor=settings.kafka_topic_replication_factor,
            ),
        )
    publisher = KafkaCollectedEventPublisher(KafkaProducer.from_settings(settings))
    command = ApplicationCrawlCommand(
        playstore=playstore,
        lifecycle=lifecycle,
        publisher=publisher,
        language_code=settings.playstore_language_code,
        country_code=settings.playstore_country_code,
    )
    crawler = CrawlerService(
        registry=registry,
        lifecycle=lifecycle,
        application_command=command,
        max_concurrent_apps=settings.playstore_max_concurrent_apps,
        language_code=settings.playstore_language_code,
        country_code=settings.playstore_country_code,
        crawler_version="0.1.0",
    )
    return CrawlerContainer(
        crawler=crawler,
        scheduler=CrawlerScheduler(
            crawler,
            interval_minutes=settings.playstore_crawl_interval_minutes,
        ),
        registry=registry,
        publisher=publisher,
    )

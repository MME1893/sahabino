from __future__ import annotations

import os

import pytest
from pydantic import SecretStr

from sahabino.crawler.infrastructure.adapters.factory import PrimaryAdapterFactory
from sahabino.crawler.infrastructure.proxy.models import ProxyEndpoint, ProxyLease
from sahabino.crawler.infrastructure.resilience.token_bucket import TokenBucketRateLimiter

pytestmark = [
    pytest.mark.external,
    pytest.mark.skipif(
        os.getenv("SAHABINO_RUN_EXTERNAL_PLAYSTORE_SMOKE") != "1",
        reason="set SAHABINO_RUN_EXTERNAL_PLAYSTORE_SMOKE=1 to contact Google Play",
    ),
]


class CountingRateLimiter:
    def __init__(self) -> None:
        self.calls = 0
        self._delegate = TokenBucketRateLimiter(1.0, 2)

    def acquire(self) -> None:
        self.calls += 1
        self._delegate.acquire()


def test_controlled_primary_stack_against_one_public_package() -> None:
    limiter = CountingRateLimiter()
    adapter = PrimaryAdapterFactory(limiter, timeout_seconds=20).create(
        "external-smoke",
        ProxyLease("external-smoke"),
    )
    try:
        details = adapter.get_app("com.whatsapp", "en", "us")
        reviews = adapter.get_reviews("com.whatsapp", "en", "us", 3)
    finally:
        adapter.close()

    assert adapter.capabilities.transport_controlled is True
    assert details.min_installs >= 0
    assert 0 <= details.score <= 5
    assert details.ratings_count >= 0
    assert details.reviews_count >= 0
    assert details.source_adapter == "gplay-scraper"
    assert 0 < len(reviews.reviews) <= 3
    assert all(review.source_adapter == "gplay-scraper" for review in reviews.reviews)
    assert limiter.calls >= 2


def test_controlled_primary_stack_through_configured_real_proxy() -> None:
    proxy_url = os.getenv("SAHABINO_EXTERNAL_PROXY_URL")
    if not proxy_url:
        pytest.skip("set SAHABINO_EXTERNAL_PROXY_URL to run the real-proxy smoke test")
    limiter = CountingRateLimiter()
    lease = ProxyLease(
        "external-proxy-smoke",
        endpoint=ProxyEndpoint("external-proxy", SecretStr(proxy_url)),
    )
    adapter = PrimaryAdapterFactory(limiter, timeout_seconds=20).create(
        "external-proxy-smoke",
        lease,
    )

    try:
        details = adapter.get_app("com.whatsapp", "en", "us")
        reviews = adapter.get_reviews("com.whatsapp", "en", "us", 3)
    finally:
        adapter.close()

    assert details.source_adapter == "gplay-scraper"
    assert 0 < len(reviews.reviews) <= 3
    assert limiter.calls >= 2

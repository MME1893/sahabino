from __future__ import annotations

from collections.abc import Callable

from sahabino.crawler.application.ports.adapter import PlayStoreAdapter
from sahabino.crawler.application.ports.proxy import ProxyLeasePort
from sahabino.crawler.application.ports.rate_limiter import GlobalRateLimiter
from sahabino.crawler.infrastructure.adapters.gplay import GPlayScraperAdapter
from sahabino.crawler.infrastructure.proxy.models import NetworkContext, ProxyLease
from sahabino.crawler.infrastructure.transport.controlled_gplay import ControlledGPlayHttpClient
from sahabino.crawler.infrastructure.transport.curl_cffi import CurlCffiTransport


class PrimaryAdapterFactory:
    def __init__(
        self,
        rate_limiter: GlobalRateLimiter,
        *,
        timeout_seconds: float,
        transport_factory: Callable[..., CurlCffiTransport] = CurlCffiTransport,
    ) -> None:
        self._rate_limiter = rate_limiter
        self._timeout = timeout_seconds
        self._transport_factory = transport_factory

    def create(self, context_id: str, lease: ProxyLeasePort) -> PlayStoreAdapter:
        if not isinstance(lease, ProxyLease):
            raise TypeError("unsupported proxy lease implementation")
        network_context = NetworkContext(context_id=context_id, lease=lease)
        transport = self._transport_factory(
            network_context,
            self._rate_limiter,
            timeout_seconds=self._timeout,
        )
        client = ControlledGPlayHttpClient.from_gplay_config(transport)
        return GPlayScraperAdapter(client)

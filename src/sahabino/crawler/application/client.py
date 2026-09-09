from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass

from sahabino.crawler.application.policies.adapter import AdapterFallbackPolicy
from sahabino.crawler.application.policies.network import NetworkPolicy
from sahabino.crawler.application.policies.retry import RetryPolicy
from sahabino.crawler.application.ports.adapter import (
    PlayStoreAdapter,
    PrimaryAdapterFactoryPort,
)
from sahabino.crawler.application.ports.proxy import ProxyLeasePort, ProxyProvider
from sahabino.crawler.application.ports.resilience import (
    CircuitBreakerPort,
    ErrorClassifierPort,
)
from sahabino.crawler.domain.dto import AppDetailsDTO, ReviewsDTO
from sahabino.crawler.domain.errors import AccessForbidden, CrawlerError, ProxyUnavailable


def _ignore_attempt(_: int) -> None:
    return


def _ignore_retry(_: BaseException) -> None:
    return


@dataclass(frozen=True, slots=True)
class OperationHooks:
    before_attempt: Callable[[int], None] = _ignore_attempt
    before_retry: Callable[[BaseException], None] = _ignore_retry


NO_OPERATION_HOOKS = OperationHooks()


class ResilientPlayStoreClient:
    """Shared facade whose opened application contexts own all task-local network state."""

    def __init__(
        self,
        *,
        proxy_provider: ProxyProvider,
        primary_factory: PrimaryAdapterFactoryPort,
        secondary_adapter: PlayStoreAdapter,
        retry_policy: RetryPolicy,
        network_policy: NetworkPolicy,
        fallback_policy: AdapterFallbackPolicy,
        circuit_breaker: CircuitBreakerPort,
        classifier: ErrorClassifierPort,
    ) -> None:
        self._proxy_provider = proxy_provider
        self._primary_factory = primary_factory
        self._secondary = secondary_adapter
        self._retry = retry_policy
        self._network = network_policy
        self._fallback = fallback_policy
        self._circuit = circuit_breaker
        self._classifier = classifier

    @contextmanager
    def open_application(self, context_id: str) -> Generator[ApplicationPlayStoreClient]:
        lease = self._proxy_provider.acquire(context_id)
        client = ApplicationPlayStoreClient(
            context_id=context_id,
            lease=lease,
            proxy_provider=self._proxy_provider,
            primary_factory=self._primary_factory,
            secondary_adapter=self._secondary,
            retry_policy=self._retry,
            network_policy=self._network,
            fallback_policy=self._fallback,
            circuit_breaker=self._circuit,
            classifier=self._classifier,
        )
        try:
            yield client
        finally:
            client.close()


class ApplicationPlayStoreClient:
    def __init__(
        self,
        *,
        context_id: str,
        lease: ProxyLeasePort,
        proxy_provider: ProxyProvider,
        primary_factory: PrimaryAdapterFactoryPort,
        secondary_adapter: PlayStoreAdapter,
        retry_policy: RetryPolicy,
        network_policy: NetworkPolicy,
        fallback_policy: AdapterFallbackPolicy,
        circuit_breaker: CircuitBreakerPort,
        classifier: ErrorClassifierPort,
    ) -> None:
        self._context_id = context_id
        self._lease = lease
        self._proxy_provider = proxy_provider
        self._primary_factory = primary_factory
        self._secondary = secondary_adapter
        self._retry = retry_policy
        self._network = network_policy
        self._fallback = fallback_policy
        self._circuit = circuit_breaker
        self._classifier = classifier
        self._primary: PlayStoreAdapter | None = None
        self._refresh_egress_before_operation = False

    def get_app(
        self,
        package_name: str,
        language_code: str,
        country_code: str,
        *,
        hooks: OperationHooks = NO_OPERATION_HOOKS,
    ) -> AppDetailsDTO:
        return self._execute(
            lambda adapter: adapter.get_app(package_name, language_code, country_code),
            lambda: self._secondary.get_app(package_name, language_code, country_code),
            hooks,
        )

    def get_reviews(
        self,
        package_name: str,
        language_code: str,
        country_code: str,
        limit: int,
        *,
        hooks: OperationHooks = NO_OPERATION_HOOKS,
    ) -> ReviewsDTO:
        return self._execute(
            lambda adapter: adapter.get_reviews(package_name, language_code, country_code, limit),
            lambda: self._secondary.get_reviews(package_name, language_code, country_code, limit),
            hooks,
        )

    def close(self) -> None:
        primary = self._primary
        self._primary = None
        try:
            if primary is not None:
                primary.close()
        finally:
            self._proxy_provider.release(self._lease)

    def _execute[ResultT](
        self,
        primary_operation: Callable[[PlayStoreAdapter], ResultT],
        secondary_operation: Callable[[], ResultT],
        hooks: OperationHooks,
    ) -> ResultT:
        self._refresh_egress_if_needed()
        circuit_call_token = self._circuit.before_call()
        last_primary_attempt = 0

        def attempt(attempt_number: int) -> ResultT:
            nonlocal last_primary_attempt
            last_primary_attempt = attempt_number
            hooks.before_attempt(attempt_number)
            adapter = self._get_primary()
            try:
                return primary_operation(adapter)
            except Exception as raw_error:
                error = self._classifier.classify(raw_error)
                try:
                    new_lease = self._network.handle_failure(
                        error,
                        self._lease,
                        self._context_id,
                        allow_rotation=(
                            attempt_number < self._retry.max_attempts
                            and self._retry.is_retryable(error)
                        ),
                    )
                except ProxyUnavailable:
                    if not isinstance(error, AccessForbidden):
                        raise
                    adapter.close()
                    self._primary = None
                    self._refresh_egress_before_operation = True
                    error.retry_with_new_egress = False
                    raise error from raw_error
                if new_lease is not self._lease:
                    adapter.close()
                    self._primary = None
                    self._lease = new_lease
                elif not self._proxy_provider.is_usable(self._lease):
                    self._refresh_egress_before_operation = True
                raise error from raw_error

        try:
            result = self._retry.execute(
                attempt,
                before_retry=hooks.before_retry,
            )
        except CrawlerError as error:
            proxied = not self._lease.is_direct
            self._circuit.record_failure(
                error,
                proxied=proxied,
                call_token=circuit_call_token,
            )
            if not self._fallback.should_use_secondary(error):
                raise
            hooks.before_retry(error)
            hooks.before_attempt(last_primary_attempt + 1)
            try:
                result = secondary_operation()
            except Exception as raw_secondary_error:
                secondary_error = self._classifier.classify(raw_secondary_error)
                self._circuit.record_failure(
                    secondary_error,
                    proxied=False,
                    call_token=circuit_call_token,
                )
                raise secondary_error from raw_secondary_error
            self._circuit.record_success(call_token=circuit_call_token)
            return result
        self._network.record_success(self._lease)
        self._circuit.record_success(call_token=circuit_call_token)
        return result

    def _get_primary(self) -> PlayStoreAdapter:
        if self._primary is None:
            self._primary = self._primary_factory.create(self._context_id, self._lease)
        return self._primary

    def _refresh_egress_if_needed(self) -> None:
        if not self._refresh_egress_before_operation:
            return
        if self._primary is not None:
            self._primary.close()
            self._primary = None
        self._lease = self._proxy_provider.rotate(self._lease, self._context_id)
        self._refresh_egress_before_operation = False

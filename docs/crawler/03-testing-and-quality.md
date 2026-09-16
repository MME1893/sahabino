# 03 - Crawler Testing and Quality Guide

## 1. Testing goals

The crawler test suite is designed around **behavioral contracts and state transitions**, not only line execution. This matters because the subsystem's correctness is mostly in decisions:

- retry or do not retry;
- rotate or keep the current egress;
- direct or proxied semantics;
- fallback or propagate;
- circuit CLOSED/OPEN/HALF_OPEN;
- task succeeded/failed/retrying;
- publish or fail at Kafka delivery boundary;
- reuse or rebuild resource stacks.

The supplied source snapshot contains **170 crawler-targeted pytest test functions** before parameterized cases are expanded:

- unit: **157**
- integration: **11**
- external: **2**

This count is an inventory of test functions in the snapshot, not a claim about the number of executed pytest cases after parametrization.

## 2. Test layers

```mermaid
flowchart TB
    EXT[External opt-in smoke\nreal Google Play / optional real proxy]
    INT[Integration\nreal PostgreSQL + Kafka\nfake Play Store adapter]
    UNIT[Unit / contract\nfake clocks, sessions, providers, adapters]

    UNIT --> INT --> EXT
```

### Unit tests

Primary purpose:

- deterministic policy/state-machine coverage;
- adapter normalization and compatibility behavior;
- error classification;
- resource lifetime;
- concurrency invariants without timing sleeps;
- composition-root selection;
- publisher contract behavior with fakes.

They do not contact Google Play.

### Integration tests

Primary purpose:

- real PostgreSQL migrations/constraints/state transitions;
- real Kafka producer/consumer flow;
- real lifecycle + orchestration interaction;
- partial failure behavior across DB and Kafka.

They intentionally use a fake Play Store adapter so third-party internet availability does not make infrastructure integration tests flaky.

### External tests

Primary purpose:

- prove the pinned, controlled primary adapter still works against the real Google Play surface;
- optionally prove the same controlled stack works through a real configured proxy.

They are skipped by default and are not normal CI dependencies.

## 3. Deterministic test techniques

### 3.1 Fake clock

`tests/unit/crawler/fakes.py::FakeClock` is thread-safe and supports:

- `monotonic()`;
- `sleep()` that advances fake time instead of blocking;
- explicit `advance()`.

It is used to test:

- retry backoff;
- token refill/wait deadlines;
- proxy cooldown;
- circuit cooldown;
- backward-clock defensive behavior.

This avoids wall-clock sleeps in unit tests.

### 3.2 Injected factories and callables

The production code provides injection points such as:

- executor factory;
- transport session factory;
- primary transport factory;
- secondary app/review fetchers;
- adapter `now` functions;
- RetryPolicy random jitter callable;
- Registry HTTP client factory.

Tests use these to observe behavior without replacing whole layers unnecessarily.

### 3.3 Threaded concurrency tests

Concurrency tests use explicit synchronization and counters rather than "sleep and hope" timing assertions where possible. Examples include:

- bounded application concurrency;
- serial mode;
- primary sequencing per application;
- ProxyPool/NoProxyProvider concurrent acquisition;
- token-bucket initial burst;
- exactly one HALF_OPEN circuit probe;
- stateless secondary wrapper under concurrent calls.

## 4. Test module map

### `tests/unit/crawler/test_application.py`

Focus: `ApplicationCrawlCommand` + `ApplicationPlayStoreClient` behavior.

Coverage includes:

- primary adapter and lease reuse across details/reviews;
- adapter rebuild on rotation;
- lease release even when primary close raises;
- secondary disabled behavior;
- secondary rate-limit/schema failure without recursive fallback;
- known adapter failure -> secondary vs unknown runtime failure -> conservative fail;
- JSON parse classification at application boundary;
- details/reviews independence;
- repeated 429 state carrying into next operation;
- logical attempt counting across retry and fallback;
- proxied 403 rotation vs direct terminal 403;
- bounded repeated 403 behavior;
- 407 unhealthy + rotation without global circuit pollution;
- proxy credential redaction in persisted lifecycle text;
- local token-wait failure isolation;
- circuit-open zero-attempt behavior;
- direct fallback after proxy exhaustion;
- Kafka publication failure -> corresponding task failure;
- unexpected error finalization of still-open tasks.

### `tests/unit/crawler/test_adapters.py`

Focus: primary/secondary normalization and pinned integration compatibility.

Coverage includes:

- semantic parity between primary and secondary DTOs;
- date precision;
- malformed primary parser/review shapes;
- batch marker and JSON behavior;
- timezone-aware review requirements;
- limit validation/cap and empty reviews;
- gplay version guard;
- secondary `lastUpdatedOn` compatibility;
- secondary timestamp matrix;
- missing `adSupported` characterization;
- secondary wrapper concurrent statelessness;
- controlled app/review request shape;
- no-locale request;
- default-header behavior;
- failure propagation without hidden backend fallback;
- real pinned gplay components using only the controlled transport.

### `tests/unit/crawler/test_contracts.py`

Focus: DTO/error/config/normalization contracts.

Coverage includes:

- Pydantic DTO validation;
- event envelope/date semantics;
- secondary capabilities;
- fallback eligibility matrix;
- classifier mappings/status/retry-after behavior;
- retry eligibility;
- proxy settings validation and secret boundary;
- safe lifecycle error redaction;
- Retry-After numeric/HTTP-date/header case behavior;
- HTTP status matrix including proxied/direct 403 flag;
- `normalize_updated_on` table and naive datetime rejection.

### `tests/unit/crawler/test_proxy_and_transport.py`

Focus: proxy provider/pool/network policy and physical transport.

Coverage includes:

- NoProxy/Static provider behavior;
- sticky direct lease lifecycle;
- same-context acquisition semantics;
- pool selection, release, cooldown, rotation;
- destructive failed rotation characterization;
- disabled/unhealthy endpoint exclusion;
- direct fallback;
- concurrent acquisition and secret safety;
- generic proxy failure threshold;
- dedicated 429 threshold/reset/accounting;
- 403/407 proxy state transitions;
- transport token use, proxy args, headers/body/timeout;
- HTTP semantics and Retry-After;
- local token wait isolation;
- non-refunded tokens across physical retries;
- exception translation matrix;
- direct requests omitting proxy argument;
- idempotent close / request-after-close;
- existing domain error identity preservation;
- one shared global limiter across rebuilt primary transports.

### `tests/unit/crawler/test_resilience.py`

Focus: token bucket, circuit breaker, and retry policy.

Coverage includes:

- token consumption/refill/wait;
- backward monotonic handling;
- initial-burst thread safety;
- constructor validation;
- capacity cap and deadline boundaries;
- no-op limiter;
- full circuit state machine;
- relevant/irrelevant failure semantics;
- direct/proxied access and timeout behavior;
- consecutive-failure reset on success;
- disabled circuit;
- one concurrent half-open probe;
- irrelevant/relevant half-open result behavior;
- stale in-flight success/failure generation races;
- bounded retry and deterministic jitter;
- Retry-After precedence and cap behavior;
- `before_retry` callback semantics;
- 503 and egress-change retry eligibility;
- retryable 5xx/timeout vs terminal 404.

### `tests/unit/crawler/test_orchestration.py`

Focus: `CrawlerService`, scheduler, Registry HTTP, Kafka publisher, composition root.

Coverage includes:

- bounded and serial application concurrency;
- details-before-reviews per app while apps overlap;
- worker cleanup after unexpected failure;
- Registry failure run handling;
- zero-active-app run;
- task creation/executor/future failure handling;
- preservation of orchestration + finalization errors;
- default no-topic-provisioning behavior;
- manual/scheduled trigger and schedule setup;
- overlap prevention and lock recovery after failure;
- Registry active endpoint and bounded retry;
- Kafka key/event-per-review behavior;
- empty review batch;
- delivery failure translation;
- settings-driven composition-root implementation selection.

### `tests/unit/crawler/test_persistence_metadata.py`

Fresh-process subprocess regression proving that importing crawler persistence registers `applications` in shared metadata and resolves the crawler task foreign key. The subprocess is essential because an ordinary pytest process can be contaminated by earlier imports.

## 5. Integration test map

### `tests/integration/crawler/test_lifecycle_persistence.py`

Uses real PostgreSQL to validate:

- Alembic-created tables, constraints, indexes, and foreign keys;
- independent task results and partial run status;
- zero-task successful run finalization;
- uniqueness and status constraints;
- transaction rollback;
- legal/illegal task state transitions;
- retry attempt count with stable original `started_at`;
- 500-character error-message bound.

### `tests/integration/crawler/test_crawler_flow.py`

Uses real PostgreSQL and real Kafka, with fake Play Store collection.

`test_real_postgres_and_kafka_complete_crawl_flow` proves:

```text
applications in DB (for FK target)
-> fake Registry refs
-> CrawlerService
-> lifecycle repository
-> fake primary adapter
-> normalized DTOs
-> real Kafka producer
-> real Kafka consumer
-> successful run/tasks and expected event types/keys
```

`test_partial_failure_preserves_successful_events_and_task_results` proves that one review-task failure does not discard other successful task outcomes/events and results in `partially_failed` run status.

### Integration infrastructure

`tests/integration/crawler/conftest.py`:

- uses `TEST_DATABASE_URL` if provided, otherwise Testcontainers Postgres `16-alpine`;
- applies Alembic migrations;
- truncates crawler/application tables around each test;
- uses `TEST_KAFKA_BOOTSTRAP_SERVERS` if provided, otherwise a Testcontainers Kafka KRaft broker;
- explicitly provisions crawler topics with one partition/replica for tests;
- deletes test topics after use when possible.

## 6. External smoke tests

File: `tests/external/test_controlled_primary_smoke.py`.

Global opt-in:

```bash
SAHABINO_RUN_EXTERNAL_PLAYSTORE_SMOKE=1 \
  uv run pytest tests/external/test_controlled_primary_smoke.py -v
```

### Direct controlled-primary smoke

Exercises:

```text
PrimaryAdapterFactory
-> GPlayScraperAdapter
-> ControlledGPlayHttpClient
-> CurlCffiTransport
-> real Google Play
```

using `com.whatsapp`, app details, and a small review limit. A counting rate limiter proves multiple physical calls pass through the controlled limiter surface.

### Real-proxy smoke

Additional environment:

```bash
SAHABINO_EXTERNAL_PROXY_URL='http://user:password@proxy.example:8080'
```

If absent, this one test skips. If present, it builds a real `ProxyEndpoint`/`ProxyLease` and exercises the same primary stack through that proxy.

Do not put real proxy credentials into source control or normal CI logs.

## 7. Decision coverage matrix

| Behavior | Representative tests |
| --- | --- |
| Primary success | application, adapters, integration flow |
| Primary retry | resilience + application |
| Retry-After 429/503 | contracts, proxy/transport, resilience |
| Parse/schema fallback | application + contracts |
| Secondary disabled | application |
| Secondary itself fails | application |
| 403 proxied rotation | application + proxy/transport |
| 403 direct terminal | application + proxy/transport |
| 407 unhealthy/rotate | application + proxy/transport |
| 429 threshold/reset | proxy/transport |
| Generic proxy failure threshold | proxy/transport |
| Proxy exhaustion direct fallback | application + proxy/transport |
| Circuit CLOSED/OPEN/HALF_OPEN | resilience |
| One half-open probe | resilience concurrency |
| Stale circuit result race | resilience concurrency |
| Local rate-limit wait | resilience/application/transport |
| Shared limiter after rotation | proxy/transport |
| Task lifecycle transitions | integration persistence |
| Run partial failure | unit orchestration + integration flow |
| Kafka delivery failure | application/orchestration |
| Fresh-process FK metadata | persistence metadata subprocess |
| Real pinned primary library compatibility | adapters + external smoke |

## 8. Quality commands

### Full normal test suite

```bash
uv run pytest
```

### Crawler unit tests

```bash
uv run pytest tests/unit/crawler -v
```

### Crawler integration tests

```bash
uv run pytest tests/integration/crawler -v
```

### Branch coverage

`pytest-cov` is present in the dev dependency group.

```bash
uv run pytest tests/unit/crawler \
  --cov=sahabino.crawler \
  --cov-branch \
  --cov-report=term-missing
```

Branch coverage is more useful than line-only coverage for this subsystem because the important behavior lives in branches/state transitions.

### Static checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pre-commit run --all-files
```

## 9. What each test layer does not prove

### Unit tests do not prove

- that Google Play's live private response shape is unchanged;
- that a real proxy provider on the internet works;
- that third-party scraper internals are thread-safe beyond what the wrapper itself controls;
- that Docker/network infrastructure is correctly configured in every environment.

### Integration tests do not prove

- the real Google Play parsing stack, because they deliberately use fake Play Store adapters;
- cross-system exactly-once behavior, because the architecture does not implement it;
- multi-process scheduler exclusion; current scheduler locking is in-process.

### External smoke tests do not prove

- stable long-running behavior or load characteristics;
- every status/error path;
- secondary real-network compatibility (current external tests target controlled primary);
- a full real-Google + real-Postgres + real-Kafka end-to-end crawl in one test.

## 10. Known remaining test/verification opportunities

The current suite is broad. Remaining useful hardening areas include:

1. **Rotation cleanup failure after a replacement lease has already been acquired.** In the current client path, `NetworkPolicy` may rotate/acquire a new lease before the old adapter is closed. If that old adapter's `close()` raises before the client stores the new lease, the replacement lease can become difficult to release correctly. A targeted regression test would clarify/fix this edge case.
2. **Multi-process scheduler overlap.** The current lock prevents overlap only inside one crawler process. Distributed scheduling would need a database/advisory/distributed lock and corresponding tests.
3. **Secondary live compatibility smoke.** If operational confidence in the fallback library becomes important, add a separately opt-in external test rather than putting it in normal CI.
4. **Long-duration/load behavior.** The suite validates concurrency correctness but is not a load/performance test for Kafka, proxies, or Google Play.
5. **Underlying third-party thread safety.** The secondary wrapper is tested as stateless; the repository cannot prove internals of `google-play-scraper` under arbitrary concurrent real network usage.

## 11. Test maintenance rules

When behavior changes:

- change the policy owner's tests first;
- update application-level interaction tests when multiple policies compose;
- add integration coverage only when a real external boundary is involved;
- keep external internet tests opt-in;
- prefer fake/injected time over real sleeps;
- do not duplicate a scenario in every layer unless each layer proves a different contract;
- preserve error taxonomy: a test should assert the meaningful domain error, not only "an exception happened";
- use subprocess tests when import-order contamination is the bug being prevented.

## 12. Current test-function inventory

The following appendix is generated from the supplied source snapshot and is included as a navigation index, not as a substitute for the scenario descriptions above.

### `tests/external/test_controlled_primary_smoke.py` (2 test functions)

- `test_controlled_primary_stack_against_one_public_package`
- `test_controlled_primary_stack_through_configured_real_proxy`

### `tests/integration/crawler/test_crawler_flow.py` (2 test functions)

- `test_real_postgres_and_kafka_complete_crawl_flow`
- `test_partial_failure_preserves_successful_events_and_task_results`

### `tests/integration/crawler/test_lifecycle_persistence.py` (9 test functions)

- `test_alembic_created_lifecycle_tables_constraints_indexes_and_foreign_keys`
- `test_task_lifecycle_independent_results_and_partial_run_status`
- `test_zero_tasks_finishes_succeeded_and_always_sets_finished_at`
- `test_unique_task_and_status_constraints_are_enforced`
- `test_uncommitted_repository_transaction_rolls_back`
- `test_legal_task_state_transitions`
- `test_illegal_task_state_transitions_raise_value_error`
- `test_retry_attempt_count_increments_without_replacing_original_started_at`
- `test_task_error_message_is_truncated_to_500_characters`

### `tests/unit/crawler/test_adapters.py` (27 test functions)

- `test_secondary_adapter_has_no_sibling_primary_implementation_dependency`
- `test_primary_and_secondary_app_normalization_have_semantic_parity`
- `test_primary_and_secondary_review_normalization_have_semantic_parity`
- `test_both_adapters_reduce_source_updates_to_calendar_dates`
- `test_primary_app_parser_non_mapping_is_parse_failure`
- `test_primary_rejects_invalid_reviews_dataset`
- `test_primary_review_decoder_requires_response_marker`
- `test_primary_review_decoder_exposes_raw_json_errors_for_classifier`
- `test_primary_review_normalization_rejects_missing_or_naive_timestamp`
- `test_primary_review_limit_must_be_positive`
- `test_primary_accepts_1000_reviews_and_caps_larger_requests`
- `test_primary_empty_reviews_are_a_successful_empty_dto`
- `test_primary_adapter_rejects_unpinned_gplay_version`
- `test_secondary_rejects_naive_review_timestamp_instead_of_using_local_timezone`
- `test_secondary_accepts_1000_reviews_and_preserves_positions`
- `test_secondary_default_fetcher_paginates_1000_newest_reviews`
- `test_secondary_uses_last_updated_on_when_updated_is_absent`
- `test_secondary_review_timestamp_normalization_matrix`
- `test_secondary_rejects_missing_or_naive_review_timestamps`
- `test_secondary_review_limit_must_be_positive`
- `test_secondary_caps_requested_review_count_before_fetching`
- `test_secondary_missing_ad_supported_defaults_to_false`
- `test_secondary_wrapper_has_no_cross_request_state_under_concurrent_calls`
- `test_controlled_client_preserves_app_and_review_requests`
- `test_controlled_client_app_request_without_locale_sends_only_id`
- `test_controlled_client_empty_headers_fall_back_to_defaults`
- `test_controlled_client_does_not_hide_failure_with_backend_fallback`
- `test_pinned_real_gplay_components_use_only_controlled_transport`

### `tests/unit/crawler/test_application.py` (24 test functions)

- `test_application_context_reuses_primary_adapter_and_lease_across_operations`
- `test_application_rotation_closes_old_adapter_and_builds_new_one_for_new_lease`
- `test_application_cleanup_releases_lease_even_when_primary_close_raises`
- `test_secondary_disabled_propagates_original_parse_failure_without_retry`
- `test_secondary_disabled_is_persisted_as_one_failed_task_attempt`
- `test_secondary_failure_is_classified_once_without_retry_or_recursive_fallback`
- `test_known_adapter_bug_uses_secondary_but_unknown_runtime_failure_does_not`
- `test_malformed_adapter_json_is_parse_failure_at_application_boundary`
- `test_details_and_reviews_succeed_independently`
- `test_details_failure_does_not_prevent_reviews`
- `test_reviews_failure_preserves_details_success`
- `test_parse_failure_uses_secondary_but_429_never_does`
- `test_repeated_429_cooldown_is_honored_by_the_next_operation`
- `test_primary_retry_and_secondary_fallback_each_count_as_logical_attempts`
- `test_proxy_403_rotates_without_secondary_and_direct_403_is_terminal`
- `test_repeated_403_rotation_is_bounded_by_operation_attempts`
- `test_repeated_403_with_no_remaining_egress_preserves_access_error`
- `test_proxy_407_is_unhealthy_rotates_and_does_not_open_circuit`
- `test_persisted_task_error_redacts_proxy_credentials`
- `test_local_rate_limit_failure_changes_no_proxy_circuit_or_adapter_policy`
- `test_open_circuit_fails_tasks_cleanly_without_attempts`
- `test_proxy_exhaustion_with_direct_fallback_uses_direct_primary`
- `test_kafka_failure_marks_corresponding_task_failed`
- `test_unexpected_application_error_is_visible_and_open_tasks_are_finalized`

### `tests/unit/crawler/test_contracts.py` (28 test functions)

- `test_app_details_validate_counts_score_and_aware_timestamp`
- `test_review_contract_rejects_naive_source_timestamp`
- `test_app_event_uses_date_precision_and_expected_envelope`
- `test_secondary_capabilities_are_direct_only`
- `test_parser_and_schema_failures_are_secondary_eligible`
- `test_throttle_and_not_found_are_not_secondary_eligible`
- `test_http_proxy_and_local_failures_never_use_secondary`
- `test_error_classifier_preserves_429_retry_after`
- `test_error_classifier_maps_http_status_attributes`
- `test_error_classifier_preserves_existing_domain_error_instance`
- `test_error_classifier_maps_pydantic_validation_failure_to_schema_failure`
- `test_error_classifier_library_and_message_mapping_matrix`
- `test_error_classifier_unknown_exception_uses_conservative_generic_error`
- `test_error_classifier_extracts_http_status_from_supported_attributes`
- `test_error_classifier_ignores_invalid_http_status_values`
- `test_error_classifier_preserves_retry_after_from_case_insensitive_headers`
- `test_retry_eligibility_is_explicit`
- `test_settings_validate_proxy_relationships`
- `test_settings_allow_explicit_direct_only_proxy_mode`
- `test_proxy_credentials_are_secret_at_settings_boundary`
- `test_lifecycle_error_text_redacts_credential_bearing_urls`
- `test_retry_after_header_lookup_is_case_insensitive`
- `test_numeric_retry_after_is_parsed_directly`
- `test_retry_after_http_date_parsing_uses_injected_time`
- `test_http_status_mapping_matrix`
- `test_http_403_egress_retry_flag_depends_on_proxy_use`
- `test_normalize_updated_on_table`
- `test_normalize_updated_on_rejects_naive_datetime`

### `tests/unit/crawler/test_orchestration.py` (19 test functions)

- `test_application_concurrency_is_bounded_without_timing_assertions`
- `test_serial_executor_mode_never_runs_more_than_one_application`
- `test_each_application_finishes_details_before_reviews_while_apps_overlap`
- `test_unexpected_worker_failure_waits_for_active_worker_cleanup_and_fails_run`
- `test_registry_failure_marks_run_failed_without_commands`
- `test_zero_active_applications_finishes_normally`
- `test_task_creation_failure_marks_run_failed_and_preserves_original_error`
- `test_executor_setup_failure_marks_run_failed_and_preserves_original_error`
- `test_future_failure_marks_run_failed_and_preserves_original_error`
- `test_orchestration_and_finalization_errors_are_both_preserved`
- `test_crawler_container_does_not_provision_topics_by_default`
- `test_manual_and_scheduled_trigger_types_and_schedule_configuration`
- `test_overlapping_scheduled_runs_are_prevented`
- `test_scheduler_releases_overlap_lock_after_crawl_failure`
- `test_registry_http_adapter_uses_active_endpoint_and_bounded_retry`
- `test_kafka_publisher_uses_application_key_and_one_event_per_review`
- `test_kafka_publisher_treats_empty_reviews_as_an_empty_successful_batch`
- `test_kafka_delivery_failure_is_translated_to_domain_publishing_error`
- `test_composition_root_selects_runtime_implementations_from_configuration`

### `tests/unit/crawler/test_persistence_metadata.py` (1 test functions)

- `test_crawler_models_register_application_fk_target_in_fresh_process`

### `tests/unit/crawler/test_proxy_and_transport.py` (31 test functions)

- `test_no_proxy_and_static_proxy_providers`
- `test_no_proxy_provider_reuses_one_active_direct_lease_per_context`
- `test_no_proxy_provider_release_then_acquire_creates_a_new_direct_lease`
- `test_no_proxy_provider_rotation_releases_and_replaces_direct_lease`
- `test_no_proxy_provider_concurrent_same_context_acquire_is_sticky`
- `test_pool_selection_sticky_release_failure_cooldown_and_rotation`
- `test_proxy_pool_same_context_lease_has_single_owner_lifecycle_semantics`
- `test_proxy_pool_failed_rotation_is_destructive_and_does_not_restore_old_lease`
- `test_disabled_and_unhealthy_proxies_are_excluded`
- `test_direct_fallback_and_disabled_direct_fallback`
- `test_pool_acquisition_is_thread_safe_and_credentials_are_secret`
- `test_network_policy_does_not_rotate_first_proxy_failure`
- `test_rate_limit_rotation_is_threshold_based`
- `test_rate_limit_counter_is_cleared_by_success_before_threshold`
- `test_direct_rate_limit_identity_is_cleared_by_success`
- `test_rate_limit_does_not_increment_generic_proxy_failure_accounting`
- `test_access_forbidden_cools_proxy_and_rotation_is_operation_bounded`
- `test_proxy_authentication_marks_endpoint_unhealthy_and_rotates`
- `test_transport_surfaces_429_retry_after_and_consumes_token`
- `test_transport_classifies_http_status_by_network_semantics`
- `test_proxy_403_and_407_errors_are_retryable_only_after_egress_change`
- `test_503_retry_after_is_preserved`
- `test_local_rate_limit_wait_failure_has_no_network_side_effects`
- `test_each_failed_physical_attempt_consumes_a_new_token_and_is_not_refunded`
- `test_transport_applies_proxy_timeout_headers_body_and_closes`
- `test_transport_exception_translation_matrix`
- `test_proxy_timeout_taxonomy_is_characterized_at_both_boundaries`
- `test_direct_transport_does_not_pass_a_proxy_argument`
- `test_transport_close_is_idempotent_and_requests_after_close_fail`
- `test_transport_preserves_existing_crawler_error_instance`
- `test_rebuilt_primary_transports_share_one_global_limiter_after_rotation`

### `tests/unit/crawler/test_resilience.py` (27 test functions)

- `test_token_bucket_capacity_consumption_refill_and_wait`
- `test_token_bucket_ignores_backward_monotonic_movement`
- `test_token_bucket_is_thread_safe_for_initial_burst`
- `test_token_bucket_rejects_non_positive_configuration`
- `test_token_bucket_refill_is_capped_at_capacity_after_long_idle_time`
- `test_token_bucket_allows_wait_exactly_equal_to_deadline`
- `test_token_bucket_rejects_wait_beyond_deadline_without_sleeping`
- `test_noop_rate_limiter_returns_immediately_without_state`
- `test_circuit_breaker_full_state_machine`
- `test_non_upstream_failures_do_not_open_circuit`
- `test_proxied_timeout_does_not_open_global_upstream_circuit`
- `test_access_forbidden_counts_only_for_direct_egress`
- `test_circuit_success_resets_consecutive_relevant_failures`
- `test_disabled_circuit_does_not_gate_or_account_for_failures`
- `test_exactly_one_concurrent_half_open_probe_is_permitted`
- `test_irrelevant_half_open_failure_releases_probe_for_next_caller`
- `test_relevant_half_open_failure_reopens_immediately_below_normal_threshold`
- `test_circuit_failure_relevance_matrix`
- `test_stale_inflight_success_cannot_close_newly_opened_circuit`
- `test_stale_inflight_failure_cannot_extend_newly_opened_cooldown`
- `test_retry_is_bounded_and_uses_exponential_jitter`
- `test_retry_after_takes_precedence_and_parse_does_not_retry`
- `test_retry_after_may_exceed_local_backoff_cap`
- `test_retry_after_never_shortens_local_backoff`
- `test_before_retry_runs_only_when_another_attempt_will_occur`
- `test_503_retry_after_and_egress_change_retry_eligibility`
- `test_5xx_and_timeout_are_retryable_but_not_404`

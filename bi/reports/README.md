# Phase 1 report catalog

These are saved-question-ready PostgreSQL `SELECT` files. They query available history and do
not create views or mutate source data. The readiness log described 89 snapshots per observed
app across five UTC dates and no snapshots for MyRightel; that is context, not a completeness
promise. None of the reports manufactures a seven-day window.

All historical locale dimensions come from `crawl_tasks`, joined through
`playstore_app_snapshots.crawl_task_id`. The locale on `applications` is shown only when it is
explicitly labeled as the current registry locale.

## Q01 — Executive portfolio: current store coverage

- **Managerial question:** Which real monitored apps have current store metadata, and which
  registry entries have no successful snapshot?
- **Grain:** One application registry row.
- **Source columns:** `applications.id/name/package_name/is_active/language_code/country_code`;
  primary-only `application_categories.application_id/category_id/is_primary` and
  `categories.id/code/name`; snapshot metadata and measures from
  `playstore_app_snapshots`; actual locale and success state from `crawl_tasks`.
- **Units:** Score is points on a 0–5 scale; counts are cumulative Google Play store totals;
  `min_installs_threshold` is a Google Play lower-bound milestone; age is hours.
- **Missing-data rules:** The application list is the left side of the query, so missing apps
  remain with `NULL` snapshot fields and `snapshot_coverage_status='missing'`. A missing primary
  category also remains `NULL`. Do not convert these values to zero.
- **Fixture rule:** The query excludes exactly
  `package_name='ir.rightel.myrightel'`, the deliberate MyRightel error fixture. It does not
  delete or modify that row and applies no other package exclusion. With the supplied 16-row
  seed this yields 15 real apps; separately registered non-fixture apps also appear.
- **Recommended visualization:** Table with conditional formatting for coverage status and data
  age; add single-number cards for available and missing counts by summarizing the saved question.
- **Recommended filters:** Primary category, active status, coverage status, crawl country,
  crawl language, and a clearly labeled freshness threshold.
- **SQL:** [`../questions/q01_portfolio_current.sql`](../questions/q01_portfolio_current.sql)

## Q02 — Store rating: daily last observation

- **Managerial question:** How has each app's store rating changed on each actually crawled
  locale?
- **Grain:** One application + crawl country + crawl language + UTC calendar date.
- **Source columns:** Snapshot `id/application_id/crawl_task_id/collected_at/score/ratings_count/
  reviews_count/source_adapter`; crawl task `id/application_id/task_type/status/country_code/
  language_code`; application `id/name/package_name`.
- **Units:** Score is points on a 0–5 scale; date is UTC; counts are cumulative store totals;
  `snapshots_in_day` is source coverage.
- **Selection rule:** `ROW_NUMBER()` chooses the latest successful `app_details` snapshot by
  `collected_at DESC, id DESC` inside each app/actual-locale/UTC-day partition. The final display
  rounds only after selection; no aggregation is rounded early.
- **Missing-data rules:** Only dates with a real successful snapshot exist. Missing days are not
  generated or interpolated. A locale is never filled from today's application registry.
- **Recommended visualization:** Multi-series line chart with UTC day on X, score on Y, and a
  required app/locale series break. Show points and tooltips containing collection time and
  `snapshots_in_day`.
- **Recommended filters:** App, crawl country, crawl language, and UTC date range. Start with
  “all available history.”
- **SQL:** [`../questions/q02_store_rating_daily.sql`](../questions/q02_store_rating_daily.sql)

## Q03 — Latest rating versus same-date category peers

- **Managerial question:** How does an app's latest score compare with primary-category peers
  observed in the same locale on the same UTC date?
- **Grain:** One application + crawl country + crawl language, at that app/locale's latest
  successful snapshot.
- **Source columns:** Q02 snapshot/task/application columns plus primary-only category assignment
  and category `id/code/name`.
- **Units:** Scores and score differences are points on a 0–5 scale; age is hours; peer count is
  the number of distinct *other* applications.
- **Comparator time rule:** Each app/locale first selects its own latest successful snapshot.
  Eligible peers are primary-category apps whose own latest snapshot has the identical crawl
  country, crawl language, and UTC date. This is a same-date cross-section, not a causal or
  longitudinal benchmark. Category assignments come from the current registry because the schema
  has no category-assignment history; the report must not imply that today's primary category was
  necessarily assigned on the snapshot date.
- **Small-sample rule:** The focal app is excluded from the peer set. One peer means the category
  has only two observed apps, so `peer_median_score_0_to_5` and the difference are returned as
  `NULL` with `peer_benchmark_status='insufficient_one_peer'`. Two peers are explicitly labeled
  `limited_two_peer_sample`; three or more are labeled `three_plus_peers`. Always display
  `peer_app_count` and status beside the median.
- **Missing-data rules:** No primary category, no same-date peers, or an insufficient peer sample
  produces an explicit status and a `NULL` benchmark. It never falls back to another date or
  locale.
- **Recommended visualization:** Dot plot or comparison table showing app score, peer median,
  peer count, comparator date, status, and data age. Never hide the peer-count/status fields.
- **Recommended filters:** Primary category, app, crawl country, crawl language, comparator UTC
  date, and benchmark status.
- **SQL:** [`../questions/q03_store_rating_vs_category.sql`](../questions/q03_store_rating_vs_category.sql)

## Q04 — Store rating/review count changes

- **Managerial question:** How did cumulative Google Play rating and review totals change between
  observed daily endpoints?
- **Grain:** One application + actual crawl locale + observed UTC day, after deterministic daily
  last selection.
- **Source columns:** Snapshot `id/application_id/crawl_task_id/collected_at/ratings_count/
  reviews_count`; actual locale and success state from `crawl_tasks`; application identity.
- **Units:** Counts and changes are Google Play cumulative totals, not sampled crawler review-row
  counts. `days_since_previous_observation` is UTC calendar days.
- **Change rule:** `LAG()` is partitioned by app and actual crawl locale. The first observation
  has `NULL` previous values and changes. Negative deltas remain negative and have dedicated
  correction flags; they are never clamped to zero.
- **Naming rule:** `ratings_count_change` is not install growth. `reviews_count` is the store's
  published cumulative count and must not be confused with the crawler's sampled review rows.
- **Missing-data rules:** No rows are generated for missing days. A multi-day gap remains one
  observed-to-observed change and is exposed by `days_since_previous_observation`.
- **Recommended visualization:** Two small-multiple bar charts for daily changes, with negative
  bars highlighted, plus tooltip totals, observation gap, and source coverage.
- **Recommended filters:** App, crawl country, crawl language, UTC date range, and negative
  correction flag.
- **SQL:** [`../questions/q04_store_counts_growth.sql`](../questions/q04_store_counts_growth.sql)

## Q05 — Observed install milestones

- **Managerial question:** When did Google Play expose a different minimum-install threshold for
  each app/locale?
- **Grain:** One application + actual crawl locale + observed UTC day, after deterministic daily
  last selection.
- **Source columns:** Snapshot `id/application_id/crawl_task_id/collected_at/min_installs`; actual
  locale and success state from `crawl_tasks`; application identity.
- **Units:** `min_installs_threshold` is a lower-bound milestone, not an exact installation count.
  Threshold changes use the same units.
- **Milestone rule:** Every real observed day remains so a step chart stays flat when the
  threshold is unchanged. `newly_observed_higher_threshold` is populated only for the first
  observation or a higher threshold. A lower correction is retained and flagged rather than
  rewritten.
- **Missing-data rules:** Missing calendar dates are not generated. Do not interpolate between
  thresholds or infer installs inside a step. All available observed history is returned.
- **Recommended visualization:** Step-after line chart with points only on observed dates;
  disable smoothing/interpolation. Use status to annotate higher milestones and lower revisions.
- **Recommended filters:** App, crawl country, crawl language, UTC date range, and threshold
  observation status.
- **SQL:** [`../questions/q05_install_milestones.sql`](../questions/q05_install_milestones.sql)

## Dashboard navigation and deferred work

Recommend these five top-level destinations in Metabase:

1. **Executive Portfolio** — Phase 1: Q01 and freshness/coverage cards.
2. **Store Growth & Rating** — Phase 1: Q02–Q05.
3. **Review Intelligence** — deferred.
4. **Network/Application Comparison** — deferred.
5. **Release Impact** — deferred.

Only the first two are built from this phase's SQL. No dashboard object is serialized here;
create saved questions and dashboards through the authorized Metabase UI after runtime and
read-only behavior are verified.

### Deferred Review Intelligence specification

Prerequisites: production revision and schema must be verified at or beyond the sentiment
migrations; observation-level `content`, `source_at`, status, language, and label coverage must be
measured; worker retry/failure behavior must be understood; privacy review must explicitly decide
whether any content can be exposed. Grant only newly approved aggregate columns. Never expose raw
review author or content by default, and never treat a crawler sample as all store reviews.

### Deferred Release Impact specification

Prerequisites: verified release/version transitions, stable pre/post windows with locale and data
coverage, and production-verified observation timestamps/sentiment. Report association only;
do not claim a release caused a rating or sentiment change. Define overlapping releases and
missing-window behavior before writing SQL.

### Deferred Network/Application Comparison specification

Prerequisites: real PCAP-derived rows, capture and application metadata linkage, unit definitions,
sampling boundaries, and metric-level coverage checks. Define how missing captures differ from
zero traffic. Do not build this report from dummy data or before real capture provenance is
validated.

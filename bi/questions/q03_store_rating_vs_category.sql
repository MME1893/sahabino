-- Latest store score per app/locale compared with primary-category peers whose
-- own latest snapshot is from the same actual locale and UTC calendar date.
WITH eligible_snapshots AS (
    SELECT
        s.id AS snapshot_id,
        s.application_id,
        a.name AS application_name,
        a.package_name,
        ct.country_code AS crawl_country_code,
        ct.language_code AS crawl_language_code,
        (s.collected_at AT TIME ZONE 'UTC')::date AS snapshot_day_utc,
        s.collected_at,
        s.score,
        c.id AS primary_category_id,
        c.code AS primary_category_code,
        c.name AS primary_category_name
    FROM public.playstore_app_snapshots AS s
    INNER JOIN public.crawl_tasks AS ct
        ON ct.id = s.crawl_task_id
       AND ct.application_id = s.application_id
    INNER JOIN public.applications AS a
        ON a.id = s.application_id
    LEFT JOIN public.application_categories AS ac
        ON ac.application_id = a.id
       AND ac.is_primary IS TRUE
    LEFT JOIN public.categories AS c
        ON c.id = ac.category_id
    WHERE ct.task_type = 'app_details'
      AND ct.status = 'succeeded'
),
ranked_latest AS (
    SELECT
        eligible_snapshots.*,
        ROW_NUMBER() OVER (
            PARTITION BY application_id, crawl_country_code, crawl_language_code
            ORDER BY collected_at DESC, snapshot_id DESC
        ) AS latest_rank
    FROM eligible_snapshots
),
latest AS (
    SELECT *
    FROM ranked_latest
    WHERE latest_rank = 1
)
SELECT
    focal.application_id,
    focal.application_name,
    focal.package_name,
    focal.primary_category_code,
    focal.primary_category_name,
    focal.crawl_country_code,
    focal.crawl_language_code,
    focal.snapshot_day_utc AS comparator_day_utc,
    focal.collected_at AS latest_collected_at,
    ROUND(focal.score::numeric, 2) AS application_store_score_0_to_5,
    peers.peer_app_count,
    CASE
        WHEN peers.peer_app_count >= 2
            THEN ROUND(peers.raw_peer_median_score::numeric, 2)
        ELSE NULL
    END AS peer_median_score_0_to_5,
    CASE
        WHEN peers.peer_app_count >= 2
            THEN ROUND((focal.score - peers.raw_peer_median_score)::numeric, 2)
        ELSE NULL
    END AS score_minus_peer_median,
    CASE
        WHEN focal.primary_category_id IS NULL THEN 'no_primary_category'
        WHEN peers.peer_app_count = 0 THEN 'no_same_date_locale_peers'
        WHEN peers.peer_app_count = 1 THEN 'insufficient_one_peer'
        WHEN peers.peer_app_count = 2 THEN 'limited_two_peer_sample'
        ELSE 'three_plus_peers'
    END AS peer_benchmark_status,
    ROUND(
        (EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - focal.collected_at)) / 3600.0)::numeric,
        1
    ) AS data_age_hours
FROM latest AS focal
CROSS JOIN LATERAL (
    SELECT
        COUNT(DISTINCT peer.application_id) AS peer_app_count,
        PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY peer.score) AS raw_peer_median_score
    FROM latest AS peer
    WHERE peer.primary_category_id = focal.primary_category_id
      AND peer.application_id <> focal.application_id
      AND peer.crawl_country_code = focal.crawl_country_code
      AND peer.crawl_language_code = focal.crawl_language_code
      AND peer.snapshot_day_utc = focal.snapshot_day_utc
) AS peers
ORDER BY
    focal.primary_category_name,
    focal.application_name,
    focal.crawl_country_code,
    focal.crawl_language_code;

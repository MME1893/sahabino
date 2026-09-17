-- Current portfolio coverage and latest successful store snapshot.
-- The deliberate MyRightel error fixture is the only registry row excluded.
WITH portfolio AS (
    SELECT
        a.id AS application_id,
        a.name AS application_name,
        a.package_name,
        a.is_active,
        a.language_code AS registry_language_code,
        a.country_code AS registry_country_code,
        c.code AS primary_category_code,
        c.name AS primary_category_name
    FROM public.applications AS a
    LEFT JOIN public.application_categories AS ac
        ON ac.application_id = a.id
       AND ac.is_primary IS TRUE
    LEFT JOIN public.categories AS c
        ON c.id = ac.category_id
    WHERE a.package_name <> 'ir.rightel.myrightel'
)
SELECT
    p.application_id,
    p.application_name,
    p.package_name,
    p.is_active,
    p.primary_category_code,
    p.primary_category_name,
    p.registry_country_code,
    p.registry_language_code,
    latest.crawl_country_code,
    latest.crawl_language_code,
    latest.collected_at AS latest_collected_at,
    ROUND(latest.score::numeric, 2) AS latest_store_score_0_to_5,
    latest.ratings_count,
    latest.reviews_count,
    latest.min_installs AS min_installs_threshold,
    latest.store_updated_on,
    latest.version,
    latest.ad_supported,
    latest.source_adapter,
    latest.successful_snapshot_count,
    (latest.collected_at IS NOT NULL) AS has_successful_snapshot,
    CASE
        WHEN latest.collected_at IS NULL THEN 'missing'
        ELSE 'available'
    END AS snapshot_coverage_status,
    ROUND(
        (EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - latest.collected_at)) / 3600.0)::numeric,
        1
    ) AS snapshot_age_hours
FROM portfolio AS p
LEFT JOIN LATERAL (
    SELECT
        s.collected_at,
        s.score,
        s.ratings_count,
        s.reviews_count,
        s.min_installs,
        s.store_updated_on,
        s.version,
        s.ad_supported,
        s.source_adapter,
        ct.country_code AS crawl_country_code,
        ct.language_code AS crawl_language_code,
        COUNT(*) OVER () AS successful_snapshot_count
    FROM public.playstore_app_snapshots AS s
    INNER JOIN public.crawl_tasks AS ct
        ON ct.id = s.crawl_task_id
       AND ct.application_id = s.application_id
    WHERE s.application_id = p.application_id
      AND ct.task_type = 'app_details'
      AND ct.status = 'succeeded'
    ORDER BY s.collected_at DESC, s.id DESC
    LIMIT 1
) AS latest ON TRUE
ORDER BY p.application_name, p.package_name;

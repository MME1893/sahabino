-- One deterministic last successful snapshot per app, actual crawl locale, and UTC day.
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
        s.ratings_count,
        s.reviews_count,
        s.source_adapter
    FROM public.playstore_app_snapshots AS s
    INNER JOIN public.crawl_tasks AS ct
        ON ct.id = s.crawl_task_id
       AND ct.application_id = s.application_id
    INNER JOIN public.applications AS a
        ON a.id = s.application_id
    WHERE a.package_name <> 'ir.rightel.myrightel'
      [[ AND a.package_name = {{application}} ]]
      [[ AND ct.country_code = {{country}} ]]
      [[ AND ct.language_code = {{language}} ]]
      AND ct.task_type = 'app_details'
      AND ct.status = 'succeeded'
),
ranked_daily AS (
    SELECT
        eligible_snapshots.*,
        COUNT(*) OVER (
            PARTITION BY
                application_id,
                crawl_country_code,
                crawl_language_code,
                snapshot_day_utc
        ) AS snapshots_in_day,
        ROW_NUMBER() OVER (
            PARTITION BY
                application_id,
                crawl_country_code,
                crawl_language_code,
                snapshot_day_utc
            ORDER BY collected_at DESC, snapshot_id DESC
        ) AS daily_rank
    FROM eligible_snapshots
)
SELECT
    application_id,
    application_name,
    package_name,
    crawl_country_code,
    crawl_language_code,
    snapshot_day_utc,
    collected_at AS daily_last_collected_at,
    score::numeric AS store_score_0_to_5,
    ratings_count,
    reviews_count,
    source_adapter,
    snapshots_in_day
FROM ranked_daily
WHERE daily_rank = 1
  [[ AND snapshot_day_utc >= {{start_date}} ]]
  [[ AND snapshot_day_utc <= {{end_date}} ]]
ORDER BY
    application_name,
    crawl_country_code,
    crawl_language_code,
    snapshot_day_utc;

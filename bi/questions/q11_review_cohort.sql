WITH observations AS (
    SELECT o.review_id, o.crawl_task_id, o.observed_at, o.score,
           r.application_id, r.first_observed_at, a.name AS application_name,
           a.package_name, ct.country_code AS crawl_country_code,
           ct.language_code AS crawl_language_code,
           cat.code AS primary_category_code
    FROM public.review_observations AS o
    JOIN public.reviews AS r ON r.id = o.review_id
    JOIN public.crawl_tasks AS ct ON ct.id = o.crawl_task_id
      AND ct.application_id = r.application_id AND ct.task_type = 'reviews'
      AND ct.status = 'succeeded'
    JOIN public.applications AS a ON a.id = r.application_id
    LEFT JOIN LATERAL (
        SELECT ac.category_id FROM public.application_categories AS ac
        WHERE ac.application_id = a.id AND ac.is_primary IS TRUE
        ORDER BY ac.category_id LIMIT 1
    ) AS primary_ac ON TRUE
    LEFT JOIN public.categories AS cat ON cat.id = primary_ac.category_id
    WHERE a.package_name <> 'ir.rightel.myrightel'
      [[ AND a.package_name = {{application}} ]]
      [[ AND ct.country_code = {{country}} ]]
      [[ AND ct.language_code = {{language}} ]]
      [[ AND cat.code = {{category}} ]]
      [[ AND (o.observed_at AT TIME ZONE 'UTC')::date <= {{end_date}} ]]
), selected AS (
    SELECT observations.*,
           ROW_NUMBER() OVER (PARTITION BY review_id
                    ORDER BY observed_at DESC, crawl_task_id DESC) AS review_rank
    FROM observations
), cohort AS (
    SELECT * FROM selected WHERE review_rank = 1
       [[ AND (first_observed_at AT TIME ZONE 'UTC')::date >= {{start_date}} ]]
)

-- An application/locale cohort counts each unique review only once; crawl sample is not all store reviews.
SELECT application_name,package_name,crawl_country_code,crawl_language_code,
 COUNT(*) AS unique_review_count,AVG(score)::numeric AS mean_unique_review_stars_1_to_5,
 COUNT(*) FILTER(WHERE score IN(1,2)) AS negative_review_count_1_2_stars,
 COUNT(*) FILTER(WHERE score IN(4,5)) AS positive_review_count_4_5_stars,
 COUNT(*) FILTER(WHERE score IN(1,2))::numeric/NULLIF(COUNT(*),0) AS negative_review_share,
 COUNT(*) FILTER(WHERE score IN(4,5))::numeric/NULLIF(COUNT(*),0) AS positive_review_share,
 MIN(first_observed_at) AS earliest_first_observed_at,MAX(first_observed_at) AS latest_first_observed_at,
 MAX(observed_at) AS latest_observed_at,
 EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP-MAX(observed_at)))/3600.0 AS observation_age_hours
FROM cohort GROUP BY application_name,package_name,crawl_country_code,crawl_language_code
ORDER BY application_name,crawl_country_code,crawl_language_code;

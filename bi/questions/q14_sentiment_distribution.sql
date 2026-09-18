WITH observations AS (
    SELECT o.review_id, o.crawl_task_id, o.observed_at, o.score, o.sentiment_status, o.sentiment_label, o.sentiment_language,
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

-- Share denominator is exclusively successfully classified eligible UNIQUE reviews.
SELECT application_name,package_name,crawl_country_code,crawl_language_code,sentiment_label,
 COUNT(*) AS classified_unique_reviews_for_label,
 SUM(COUNT(*)) OVER(PARTITION BY application_name,package_name,crawl_country_code,crawl_language_code) AS classified_sample_size,
 COUNT(*)::numeric/NULLIF(SUM(COUNT(*)) OVER(PARTITION BY application_name,package_name,crawl_country_code,crawl_language_code),0) AS sentiment_share
FROM cohort WHERE sentiment_status='done' AND sentiment_label IN('positive','neutral','negative')
GROUP BY application_name,package_name,crawl_country_code,crawl_language_code,sentiment_label
ORDER BY application_name,crawl_country_code,crawl_language_code,sentiment_label;

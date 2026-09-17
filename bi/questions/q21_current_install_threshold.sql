-- Latest reported install LOWER BOUND per application and historical crawl locale.
WITH observed AS (
 SELECT a.name AS application_name,a.package_name,ct.country_code AS crawl_country_code,
 ct.language_code AS crawl_language_code,s.min_installs AS min_installs_threshold,s.collected_at,
 ROW_NUMBER() OVER(PARTITION BY a.id,ct.country_code,ct.language_code ORDER BY s.collected_at DESC,s.id DESC) AS rn
 FROM public.playstore_app_snapshots s JOIN public.applications a ON a.id=s.application_id
 JOIN public.crawl_tasks ct ON ct.id=s.crawl_task_id AND ct.application_id=s.application_id
 WHERE a.package_name <> 'ir.rightel.myrightel' AND ct.task_type='app_details' AND ct.status='succeeded'
 [[ AND a.package_name={{application}} ]] [[ AND ct.country_code={{country}} ]] [[ AND ct.language_code={{language}} ]]
)
SELECT application_name,package_name,crawl_country_code,crawl_language_code,min_installs_threshold,collected_at AS latest_collected_at
FROM observed WHERE rn=1 ORDER BY application_name,crawl_country_code,crawl_language_code;

\set ON_ERROR_STOP on

BEGIN;

SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafana_reader') AS role_exists \gset

DO $public_audit$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_namespace namespace
        CROSS JOIN LATERAL aclexplode(
            coalesce(namespace.nspacl, acldefault('n', namespace.nspowner))
        ) privilege
        WHERE namespace.nspname = 'public'
          AND privilege.grantee = 0
          AND privilege.privilege_type = 'CREATE'
    ) THEN
        RAISE EXCEPTION 'PUBLIC can create objects in the public schema';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM information_schema.table_privileges
        WHERE grantee = 'PUBLIC'
          AND table_schema = 'public'
          AND table_name IN (
              'applications', 'crawl_runs', 'crawl_tasks', 'reviews', 'review_observations'
          )
          AND privilege_type IN (
              'SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER'
          )
    ) OR EXISTS (
        SELECT 1
        FROM information_schema.column_privileges
        WHERE grantee = 'PUBLIC'
          AND table_schema = 'public'
          AND table_name IN (
              'applications', 'crawl_runs', 'crawl_tasks', 'reviews', 'review_observations'
          )
          AND privilege_type IN ('SELECT', 'INSERT', 'UPDATE', 'REFERENCES')
    ) THEN
        RAISE EXCEPTION 'PUBLIC has unexpected privileges on dashboard tables';
    END IF;
END
$public_audit$;

DO $audit$
DECLARE
    reader_oid oid;
BEGIN
    SELECT oid INTO reader_oid FROM pg_roles WHERE rolname = 'grafana_reader';
    IF reader_oid IS NULL THEN
        RETURN;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_roles
        WHERE oid = reader_oid
          AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'grafana_reader has unsafe role attributes';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_auth_members WHERE member = reader_oid) THEN
        RAISE EXCEPTION 'grafana_reader has unexpected role memberships';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_class
        WHERE relowner = reader_oid
          AND relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
    ) THEN
        RAISE EXCEPTION 'grafana_reader unexpectedly owns database objects';
    END IF;

    IF has_schema_privilege('grafana_reader', 'public', 'CREATE') THEN
        RAISE EXCEPTION 'grafana_reader can create objects in the public schema';
    END IF;

    -- A pre-existing role could have column-level SELECT on sensitive fields
    -- without having table-level SELECT. Audit effective privileges, not only
    -- table grants, before adding the approved column grants below.
    IF EXISTS (
        SELECT 1
        FROM (VALUES
            ('applications', ARRAY[
                'id', 'name', 'package_name', 'language_code', 'country_code',
                'is_active', 'deactivated_at', 'created_at', 'updated_at'
            ]),
            ('crawl_runs', ARRAY[
                'id', 'trigger_type', 'status', 'scheduled_for', 'started_at',
                'finished_at', 'created_at'
            ]),
            ('crawl_tasks', ARRAY[
                'id', 'crawl_run_id', 'application_id', 'task_type', 'status',
                'language_code', 'country_code', 'attempt_count', 'started_at',
                'finished_at', 'error_code', 'created_at'
            ]),
            ('reviews', ARRAY[
                'id', 'application_id', 'source_at', 'thumbs_up_count',
                'score', 'source_adapter', 'first_observed_at',
                'last_observed_at', 'created_at', 'updated_at'
            ]),
            ('review_observations', ARRAY[
                'crawl_task_id', 'review_id', 'observed_at', 'position', 'score',
                'thumbs_up_count', 'source_adapter', 'source_at',
                'sentiment_language', 'sentiment_label', 'sentiment_status',
                'sentiment_processed_at', 'sentiment_attempt_count'
            ])
        ) AS allowed(table_name, column_names)
        JOIN pg_class relation ON relation.relname = allowed.table_name
        JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace
        JOIN pg_attribute attribute ON attribute.attrelid = relation.oid
        WHERE namespace.nspname = 'public'
          AND attribute.attnum > 0
          AND NOT attribute.attisdropped
          AND NOT (attribute.attname = ANY (allowed.column_names))
          AND has_column_privilege(
              'grafana_reader', relation.oid, attribute.attname, 'SELECT'
          )
    ) THEN
        RAISE EXCEPTION 'grafana_reader can read unapproved dashboard columns';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM unnest(ARRAY[
            'public.applications',
            'public.crawl_runs',
            'public.crawl_tasks',
            'public.reviews',
            'public.review_observations'
        ]) AS target_table(name)
        WHERE has_table_privilege(
                  'grafana_reader',
                  target_table.name,
                  'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
              )
           OR has_any_column_privilege(
                  'grafana_reader',
                  target_table.name,
                  'INSERT,UPDATE,REFERENCES'
              )
    ) THEN
        RAISE EXCEPTION 'grafana_reader has unexpected table or column privileges';
    END IF;
END
$audit$;

\if :role_exists
  \if :rotate_password
    ALTER ROLE grafana_reader PASSWORD :'reader_password';
  \endif
\else
  CREATE ROLE grafana_reader
    LOGIN
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOINHERIT
    NOREPLICATION
    NOBYPASSRLS
    CONNECTION LIMIT 5
    PASSWORD :'reader_password';
\endif

ALTER ROLE grafana_reader
  LOGIN
  NOSUPERUSER
  NOCREATEDB
  NOCREATEROLE
  NOINHERIT
  NOREPLICATION
  NOBYPASSRLS
  CONNECTION LIMIT 5;
ALTER ROLE grafana_reader SET default_transaction_read_only = on;
ALTER ROLE grafana_reader SET statement_timeout = '30s';
ALTER ROLE grafana_reader SET lock_timeout = '5s';
ALTER ROLE grafana_reader SET idle_in_transaction_session_timeout = '30s';

GRANT CONNECT ON DATABASE :"DBNAME" TO grafana_reader;
GRANT USAGE ON SCHEMA public TO grafana_reader;
GRANT SELECT (
    id, name, package_name, language_code, country_code, is_active,
    deactivated_at, created_at, updated_at
) ON public.applications TO grafana_reader;
GRANT SELECT (
    id, trigger_type, status, scheduled_for, started_at, finished_at, created_at
) ON public.crawl_runs TO grafana_reader;
GRANT SELECT (
    id, crawl_run_id, application_id, task_type, status, language_code,
    country_code, attempt_count, started_at, finished_at, error_code, created_at
) ON public.crawl_tasks TO grafana_reader;
GRANT SELECT (
    id, application_id, source_at, thumbs_up_count, score, source_adapter,
    first_observed_at, last_observed_at, created_at, updated_at
) ON public.reviews TO grafana_reader;
GRANT SELECT (
    crawl_task_id, review_id, observed_at, position, score, thumbs_up_count,
    source_adapter, source_at, sentiment_language, sentiment_label,
    sentiment_status, sentiment_processed_at, sentiment_attempt_count
) ON public.review_observations TO grafana_reader;

COMMIT;

\if :role_exists
  \echo GRAFANA_READER_VERIFIED
\else
  \echo GRAFANA_READER_CREATED
\endif

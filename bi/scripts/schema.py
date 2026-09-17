"""Column contracts: only these source attributes can be read by the BI role."""

BASE = {
    "alembic_version": ("version_num",),
    "applications": ("id", "name", "package_name", "is_active", "language_code", "country_code"),
    "categories": ("id", "code", "name"),
    "application_categories": ("application_id", "category_id", "is_primary"),
    "crawl_tasks": ("id", "application_id", "task_type", "status", "country_code", "language_code"),
    "playstore_app_snapshots": (
        "id",
        "application_id",
        "crawl_task_id",
        "collected_at",
        "score",
        "ratings_count",
        "reviews_count",
        "min_installs",
        "store_updated_on",
        "version",
        "ad_supported",
        "source_adapter",
    ),
    "reviews": ("id", "application_id", "first_observed_at"),
    "review_observations": ("crawl_task_id", "review_id", "observed_at", "score"),
}
SENTIMENT = {"review_observations": ("sentiment_status", "sentiment_label", "sentiment_language")}
SENSITIVE = {
    "reviews": ("content", "author_name", "external_review_id", "source_at", "thumbs_up_count"),
    "review_observations": (
        "content",
        "source_at",
        "position",
        "thumbs_up_count",
        "source_adapter",
        "sentiment_processed_at",
        "sentiment_attempt_count",
    ),
}

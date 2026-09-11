from enum import StrEnum


class TriggerType(StrEnum):
    SCHEDULED = "scheduled"
    MANUAL = "manual"


class CrawlRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIALLY_FAILED = "partially_failed"
    FAILED = "failed"


class CrawlTaskType(StrEnum):
    APP_DETAILS = "app_details"
    REVIEWS = "reviews"


class CrawlTaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

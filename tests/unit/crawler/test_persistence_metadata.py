from __future__ import annotations

import subprocess
import sys


def test_crawler_models_register_application_fk_target_in_fresh_process() -> None:
    code = """
from sahabino.crawler.infrastructure.persistence.models import CrawlTask
from sahabino.db.base import Base

assert "applications" in Base.metadata.tables
foreign_key = next(iter(CrawlTask.__table__.c.application_id.foreign_keys))
assert foreign_key.target_fullname == "applications.id"
assert foreign_key.column is Base.metadata.tables["applications"].c.id
"""

    result = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr

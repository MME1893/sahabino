from __future__ import annotations

import argparse
import logging

from sahabino.common.config import get_settings
from sahabino.common.observability import configure_logging
from sahabino.crawler.bootstrap.container import build_container
from sahabino.crawler.domain.results import TriggerType


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sahabino Google Play crawler")
    parser.add_argument("command", choices=("crawl-once", "scheduler"))
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    settings = get_settings()
    configure_logging(
        service_name="sahabino-crawler",
        level=settings.log_level,
        log_format=settings.log_format,
        environment=settings.environment,
    )
    logger = logging.getLogger(__name__)
    logger.info(
        "crawler process started",
        extra={"event": "crawler.process.started", "command": arguments.command},
    )
    try:
        with build_container() as container:
            if arguments.command == "crawl-once":
                run_id = container.crawler.crawl_once(TriggerType.MANUAL)
                print(run_id)
            else:
                container.scheduler.start()
    finally:
        logger.info(
            "crawler process stopped",
            extra={"event": "crawler.process.stopped", "command": arguments.command},
        )


if __name__ == "__main__":
    main()

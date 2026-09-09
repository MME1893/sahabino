from __future__ import annotations

import argparse

from sahabino.crawler.bootstrap.container import build_container
from sahabino.crawler.domain.results import TriggerType


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sahabino Google Play crawler")
    parser.add_argument("command", choices=("crawl-once", "scheduler"))
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    with build_container() as container:
        if arguments.command == "crawl-once":
            run_id = container.crawler.crawl_once(TriggerType.MANUAL)
            # TODO: instead of just printig we should add logging system
            print(run_id)
        else:
            container.scheduler.start()


if __name__ == "__main__":
    main()

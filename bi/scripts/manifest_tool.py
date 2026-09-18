#!/usr/bin/env python3
"""Offline/private manifest validator; optional DB checks are explicit and read-only."""

import argparse
import json
import sys

from metadata import validate_experiment, validate_release
from preflight import query


def db_capture_records(args, capture_ids):
    if not args.db_container:
        return None
    if not capture_ids:
        return []
    values = ",".join("'" + value.replace("'", "''") + "'::uuid" for value in capture_ids)
    sql = f"""
SELECT COALESCE(json_agg(x),'[]'::json)::text FROM (
 SELECT nc.id::text capture_id,a.package_name,nc.scenario,nc.transfer_file_size_bytes,nc.status
 FROM public.network_captures nc JOIN public.applications a ON a.id=nc.application_id
 WHERE nc.id IN ({values})
) x;
"""
    return json.loads(
        query(args.db_container, args.reader, args.database, sql, args.reader_password_file)
    )


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--experiment-manifest")
    p.add_argument("--release-manifest")
    p.add_argument(
        "--db-container", help="explicit container ID/name;" \
        " enables DB-aware capture checks"
    )
    p.add_argument("--reader", default="sahabino_bi_reader")
    p.add_argument("--database", default="sahabino")
    p.add_argument("--reader-password-file")
    args = p.parse_args(argv)
    try:
        first = validate_experiment(args.experiment_manifest)
        records = db_capture_records(args, [x["capture_id"] for x in first.records])
        experiment = (
            validate_experiment(args.experiment_manifest, records) if records is not None \
                else first
        )
        release = validate_release(args.release_manifest)
        output = {"experiment": experiment.safe_dict(), "release": release.safe_dict()}
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0 if experiment.valid and release.valid else 2
    except Exception:
        print(
            "BLOCKER: manifest validation failed; database/server details suppressed",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())

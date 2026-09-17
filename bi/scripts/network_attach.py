#!/usr/bin/env python3
"""Explicit, idempotent, failure-safe attachment of the existing Compose postgres."""

import argparse
import json
import subprocess
import sys

ALIAS = "sahabino-postgres-bi"


class NetworkError(Exception):
    pass


def docker(*args):
    try:
        proc = subprocess.run(["docker", *args], text=True, capture_output=True, check=False)
    except OSError as exc:
        raise NetworkError(
            "Docker unavailable; install/start Docker and retry; no changes made"
        ) from exc
    if proc.returncode:
        raise NetworkError(
            "Docker command failed ("
            + " ".join(args[:2])
            + "); inspect Docker daemon/access; no automatic repair"
        )
    return proc.stdout.strip()


def inspect(kind, identity):
    data = json.loads(docker("inspect", "--type", kind, identity))
    if len(data) != 1:
        raise NetworkError("Ambiguous Docker inspect result")
    return data[0]


def discover(project, service):
    ids = docker(
        "ps",
        "-q",
        "--no-trunc",
        "--filter",
        f"label=com.docker.compose.project={project}",
        "--filter",
        f"label=com.docker.compose.service={service}",
    ).splitlines()
    if len(ids) != 1:
        raise NetworkError(
            f"Expected exactly one running {project}/{service} container, found {len(ids)}; select correct Compose project explicitly"
        )
    item = inspect("container", ids[0])
    labels = item.get("Config", {}).get("Labels") or {}
    if (
        labels.get("com.docker.compose.project") != project
        or labels.get("com.docker.compose.service") != service
    ):
        raise NetworkError("Exact Compose label mismatch; nothing changed")
    if not item.get("State", {}).get("Running"):
        raise NetworkError("Selected PostgreSQL is not running")
    return item


def membership(container, network):
    networks = container.get("NetworkSettings", {}).get("Networks") or {}
    endpoint = networks.get(network)
    if not endpoint:
        return None
    aliases = endpoint.get("Aliases") or []
    return set(aliases)


def operate(project, service, network, selected, action):
    container = discover(project, service)
    cid = container["Id"]
    if selected and selected != cid:
        raise NetworkError(
            "Selected full container ID differs from exact Compose-label discovery; rerun discovery"
        )
    try:
        net = inspect("network", network)
    except NetworkError:
        if action != "attach":
            raise NetworkError(
                "BI network missing. Operator: docker network create --driver bridge --attachable "
                + network
            )
        raise NetworkError(
            "BI network missing. Operator must create explicitly: docker network create --driver bridge --attachable "
            + network
        )
    if net["Name"] != network or net.get("Driver") != "bridge":
        raise NetworkError("Unexpected network identity/driver; inspect manually")
    aliases = membership(container, network)
    in_network = cid in (net.get("Containers") or {})
    if aliases is not None or in_network:
        if aliases is None or not in_network or ALIAS not in aliases:
            raise NetworkError(
                f"Attached state/alias inconsistent; do NOT disconnect live PostgreSQL automatically. Schedule maintenance; inspect docker network inspect {network} and docker inspect {cid}; after verifying client impact and an approved window, manually disconnect/reconnect this BI-only network with --alias {ALIAS}."
            )
        print(f"OK: {cid} attached to {network} with alias {ALIAS}; no change")
        return cid
    if action == "check":
        raise NetworkError(
            f"PostgreSQL {cid} is not attached to {network}; run attach explicitly with --container-id {cid}"
        )
    docker("network", "connect", "--alias", ALIAS, network, cid)
    updated = inspect("container", cid)
    if ALIAS not in (membership(updated, network) or set()):
        raise NetworkError(
            "Attachment returned but alias verification failed; manual inspection required"
        )
    print(f"OK: attached {cid} to {network} with verified alias {ALIAS}")
    return cid


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["discover", "check", "attach"])
    parser.add_argument("--project", required=True, help="exact Compose project label")
    parser.add_argument("--service", default="postgres", help="exact Compose service label")
    parser.add_argument("--network", default="sahabino-bi-db")
    parser.add_argument("--container-id", help="full ID from discover; required for attach")
    args = parser.parse_args(argv)
    try:
        if args.action == "discover":
            c = discover(args.project, args.service)
            print(f"ID={c['Id']} NAME={c['Name']} PROJECT={args.project} SERVICE={args.service}")
        else:
            if args.action == "attach" and not args.container_id:
                raise NetworkError("Attach requires --container-id with the full ID from discover")
            operate(args.project, args.service, args.network, args.container_id, args.action)
    except (NetworkError, ValueError, KeyError) as exc:
        print(f"BLOCKER: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pathlib
import stat
import sys
from typing import Any


def _inside(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_payload(payload: Any) -> None:
    if not isinstance(payload, dict) or not isinstance(payload.get("identities"), list):
        raise ValueError("invalid identities document")
    identities = payload["identities"]
    if len(identities) != 1 or not isinstance(identities[0], dict):
        raise ValueError("exactly one production identity is required")
    credentials = identities[0].get("credentials")
    if not isinstance(credentials, list) or len(credentials) != 1:
        raise ValueError("exactly one credential is required")
    credential = credentials[0]
    if not isinstance(credential, dict):
        raise ValueError("credential must be an object")
    if not credential.get("accessKey") or not credential.get("secretKey"):
        raise ValueError("credential keys must be non-empty")


def install_config(
    candidate: pathlib.Path,
    destination: pathlib.Path,
    allowed_root: pathlib.Path,
    *,
    uid: int,
    gid: int,
) -> None:
    root = allowed_root.resolve(strict=True)
    parent = destination.parent.resolve(strict=True)
    if not _inside(parent, root):
        raise ValueError("destination escapes the runtime root")
    if destination.is_symlink():
        raise ValueError("destination must not be a symlink")
    if destination.exists() and not destination.is_file():
        raise ValueError("destination must be a regular file")
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError("candidate must be a non-symlink regular file")
    if candidate.parent.resolve(strict=True) != parent:
        raise ValueError("candidate and destination must share a filesystem directory")

    with candidate.open("r", encoding="utf-8") as stream:
        _validate_payload(json.load(stream))

    os.chmod(candidate, 0o600)
    candidate_stat = candidate.stat(follow_symlinks=False)
    if os.name == "posix" and (candidate_stat.st_uid, candidate_stat.st_gid) != (uid, gid):
        if not hasattr(os, "chown"):
            raise OSError("ownership adjustment is unavailable")
        os.chown(candidate, uid, gid)
    candidate_stat = candidate.stat(follow_symlinks=False)
    if os.name == "posix" and stat.S_IMODE(candidate_stat.st_mode) != 0o600:
        raise ValueError("candidate mode is not 0600")
    if os.name == "posix" and (candidate_stat.st_uid, candidate_stat.st_gid) != (uid, gid):
        raise ValueError("candidate ownership is incorrect")

    os.replace(candidate, destination)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", required=True, type=pathlib.Path)
    parser.add_argument("--destination", required=True, type=pathlib.Path)
    parser.add_argument("--allowed-root", required=True, type=pathlib.Path)
    parser.add_argument("--uid", required=True, type=int)
    parser.add_argument("--gid", required=True, type=int)
    return parser


def main(argv: list[str]) -> int:
    arguments = _parser().parse_args(argv[1:])
    try:
        install_config(
            arguments.candidate,
            arguments.destination,
            arguments.allowed_root,
            uid=arguments.uid,
            gid=arguments.gid,
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        try:
            if arguments.candidate.is_file() and not arguments.candidate.is_symlink():
                arguments.candidate.unlink()
        except OSError:
            pass
        print(f"SeaweedFS config candidate rejected: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

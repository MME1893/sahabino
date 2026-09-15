#!/usr/bin/env python3
from __future__ import annotations

import ipaddress
import re
import sys
from urllib.parse import urlsplit


def validate_public_origin(value: str) -> None:
    if not value or any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError("whitespace and control characters are not allowed")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise ValueError("malformed URL or port") from error

    if parsed.scheme not in {"http", "https"}:
        raise ValueError("scheme must be http or https")
    if not hostname:
        raise ValueError("hostname is required")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("credentials are not allowed")
    if parsed.query or parsed.fragment:
        raise ValueError("query strings and fragments are not allowed")
    if parsed.path not in {"", "/"}:
        raise ValueError("paths are not allowed")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("port is outside 1..65535")
    if parsed.netloc.endswith(":"):
        raise ValueError("port must not be empty")

    normalized_host = hostname.rstrip(".").lower()
    if normalized_host in {"localhost", "host.docker.internal"} or normalized_host.endswith(
        (".localhost", ".local", ".internal", ".test", ".example", ".invalid")
    ):
        raise ValueError("local-only hostname is not allowed")

    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        try:
            ascii_host = normalized_host.encode("idna").decode("ascii")
        except UnicodeError as error:
            raise ValueError("hostname is malformed") from error
        if "." not in ascii_host:
            raise ValueError("single-label hostnames are not public origins") from None
        if len(ascii_host) > 253 or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in ascii_host.split(".")
        ):
            raise ValueError("hostname is malformed") from None
        return
    if not address.is_global:
        raise ValueError("non-public IP address is not allowed")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: validate_public_origin.py URL", file=sys.stderr)
        return 2
    try:
        validate_public_origin(argv[1])
    except ValueError as error:
        print(f"invalid public object-storage origin: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

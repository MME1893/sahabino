"""Generate a tiny deterministic Ethernet/IPv4/TCP PCAPNG capture for tests and smoke runs."""

from __future__ import annotations

import argparse
import struct
from pathlib import Path


def _pad4(value: bytes) -> bytes:
    return value + b"\x00" * (-len(value) % 4)


def _block(block_type: int, body: bytes) -> bytes:
    padded = _pad4(body)
    total_length = 12 + len(padded)
    return struct.pack("<II", block_type, total_length) + padded + struct.pack("<I", total_length)


def _checksum(value: bytes) -> int:
    padded = value if len(value) % 2 == 0 else value + b"\x00"
    words = struct.unpack(f"!{len(padded) // 2}H", padded)
    total = sum(words)
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def _tcp_packet(
    *,
    source: bytes,
    destination: bytes,
    source_port: int,
    destination_port: int,
    sequence: int,
    acknowledgment: int,
    flags: int,
    payload: bytes = b"",
) -> bytes:
    tcp_without_checksum = struct.pack(
        "!HHIIBBHHH",
        source_port,
        destination_port,
        sequence,
        acknowledgment,
        5 << 4,
        flags,
        65535,
        0,
        0,
    )
    pseudo_header = (
        source + destination + struct.pack("!BBH", 0, 6, len(tcp_without_checksum) + len(payload))
    )
    tcp_checksum = _checksum(pseudo_header + tcp_without_checksum + payload)
    tcp = bytearray(tcp_without_checksum)
    struct.pack_into("!H", tcp, 16, tcp_checksum)

    total_length = 20 + len(tcp) + len(payload)
    ip = bytearray(
        struct.pack(
            "!BBHHHBBH4s4s",
            0x45,
            0,
            total_length,
            sequence & 0xFFFF,
            0x4000,
            64,
            6,
            0,
            source,
            destination,
        )
    )
    struct.pack_into("!H", ip, 10, _checksum(bytes(ip)))
    ethernet = bytes.fromhex("0200000000020200000000010800")
    return ethernet + bytes(ip) + bytes(tcp) + payload


def _enhanced_packet(packet: bytes, timestamp_microseconds: int, direction: int) -> bytes:
    timestamp_high = timestamp_microseconds >> 32
    timestamp_low = timestamp_microseconds & 0xFFFFFFFF
    fixed = struct.pack(
        "<IIIII",
        0,
        timestamp_high,
        timestamp_low,
        len(packet),
        len(packet),
    )
    # epb_flags option: low two bits are 1=inbound, 2=outbound.
    options = struct.pack("<HHIHH", 2, 4, direction, 0, 0)
    return _block(0x00000006, fixed + _pad4(packet) + options)


def build_fixture() -> bytes:
    client = bytes((10, 0, 0, 1))
    server = bytes((10, 0, 0, 2))
    packets = (
        (
            1_000_000,
            2,
            _tcp_packet(
                source=client,
                destination=server,
                source_port=50000,
                destination_port=443,
                sequence=1,
                acknowledgment=0,
                flags=0x02,
            ),
        ),
        (
            1_050_000,
            1,
            _tcp_packet(
                source=server,
                destination=client,
                source_port=443,
                destination_port=50000,
                sequence=10,
                acknowledgment=2,
                flags=0x12,
            ),
        ),
        (
            1_060_000,
            2,
            _tcp_packet(
                source=client,
                destination=server,
                source_port=50000,
                destination_port=443,
                sequence=2,
                acknowledgment=11,
                flags=0x10,
            ),
        ),
        (
            1_100_000,
            2,
            _tcp_packet(
                source=client,
                destination=server,
                source_port=50000,
                destination_port=443,
                sequence=2,
                acknowledgment=11,
                flags=0x18,
                payload=b"abcdefghij",
            ),
        ),
        (
            1_200_000,
            2,
            _tcp_packet(
                source=client,
                destination=server,
                source_port=50000,
                destination_port=443,
                sequence=12,
                acknowledgment=11,
                flags=0x18,
                payload=b"klmnopqrst",
            ),
        ),
        (
            1_250_000,
            1,
            _tcp_packet(
                source=server,
                destination=client,
                source_port=443,
                destination_port=50000,
                sequence=11,
                acknowledgment=22,
                flags=0x10,
            ),
        ),
    )
    section_header = _block(
        0x0A0D0D0A,
        struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1),
    )
    interface_description = _block(0x00000001, struct.pack("<HHI", 1, 0, 65535))
    return (
        section_header
        + interface_description
        + b"".join(
            _enhanced_packet(packet, timestamp, direction)
            for timestamp, direction, packet in packets
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(build_fixture())
    print(args.output)


if __name__ == "__main__":
    main()

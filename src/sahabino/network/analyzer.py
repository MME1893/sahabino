from __future__ import annotations

import csv
import hashlib
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from sahabino.messaging.network_events import (
    CaptureMetricsV1,
    DnsMetricsV1,
    FlowMetricsV1,
    IpMetricsV1,
    NetworkCapabilitiesV1,
    ProtocolMixMetricsV1,
    QuicMetricsV1,
    TcpMetricsV1,
    TrafficMetricsV1,
    TransferMetricsV1,
    UdpMetricsV1,
)
from sahabino.network.exceptions import TerminalAnalysisError
from sahabino.network.metrics import PacketRecord, aggregate_packets

ANALYZER_VERSION = "1.0.0"
PCAP_MAGIC = {
    bytes.fromhex("d4c3b2a1"),
    bytes.fromhex("a1b2c3d4"),
    bytes.fromhex("4d3cb2a1"),
    bytes.fromhex("a1b23c4d"),
}
PCAPNG_MAGIC = bytes.fromhex("0a0d0d0a")
ICMPV4_ERROR_TYPES = frozenset({3, 4, 5, 11, 12})

FIELD_NAMES = (
    "frame.time_epoch",
    "frame.len",
    "frame.cap_len",
    "frame.packet_flags_direction",
    "frame.protocols",
    "ip.version",
    "ip.len",
    "ip.src",
    "ip.dst",
    "ip.flags.mf",
    "ip.frag_offset",
    "ip.dsfield.ecn",
    "ipv6.plen",
    "ipv6.src",
    "ipv6.dst",
    "ipv6.tclass",
    "ipv6.fraghdr.offset",
    "tcp.srcport",
    "tcp.dstport",
    "tcp.stream",
    "tcp.len",
    "tcp.flags.syn",
    "tcp.flags.ack",
    "tcp.flags.reset",
    "tcp.window_size_value",
    "tcp.analysis.initial_rtt",
    "tcp.analysis.ack_rtt",
    "tcp.analysis.retransmission",
    "tcp.analysis.fast_retransmission",
    "tcp.analysis.spurious_retransmission",
    "tcp.analysis.zero_window",
    "tcp.analysis.window_full",
    "tcp.analysis.out_of_order",
    "tcp.analysis.duplicate_ack",
    "tcp.analysis.lost_segment",
    "udp.srcport",
    "udp.dstport",
    "udp.length",
    "quic.version",
    "quic.long.packet_type",
    "quic.spin_bit",
    "dns.flags.response",
    "dns.flags.rcode",
    "dns.time",
    "icmp.type",
    "icmpv6.type",
)
REQUIRED_FIELDS = frozenset(
    {
        "frame.time_epoch",
        "frame.len",
        "frame.cap_len",
        "frame.protocols",
        "ip.len",
        "ipv6.plen",
        "tcp.len",
        "udp.length",
    }
)


@dataclass(frozen=True, slots=True)
class TsharkOutput:
    version: str
    supported_fields: set[str]
    packets: list[PacketRecord]


class AnalysisEngine(Protocol):
    def read_packets(self, capture_path: Path) -> TsharkOutput: ...


class AnalysisMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    analyzed_at: datetime
    analyzer_version: str
    tshark_version: str
    source_analyzer: Literal["tshark"] = "tshark"
    verified_sha256: str
    capture: CaptureMetricsV1
    capabilities: NetworkCapabilitiesV1
    traffic: TrafficMetricsV1
    protocol_mix: ProtocolMixMetricsV1
    ip: IpMetricsV1
    flow: FlowMetricsV1
    transfer: TransferMetricsV1
    tcp: TcpMetricsV1 | None
    quic: QuicMetricsV1 | None
    udp: UdpMetricsV1 | None
    dns: DnsMetricsV1 | None


def capture_format(path: Path) -> Literal["pcap", "pcapng"]:
    try:
        with path.open("rb") as stream:
            magic = stream.read(4)
    except OSError as error:
        raise TerminalAnalysisError("invalid_capture", "Capture could not be read") from error
    if magic in PCAP_MAGIC:
        return "pcap"
    if magic == PCAPNG_MAGIC:
        return "pcapng"
    raise TerminalAnalysisError("invalid_capture", "Capture magic/header is not PCAP or PCAPNG")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise TerminalAnalysisError("invalid_capture", "Capture could not be read") from error
    return digest.hexdigest()


def _integer(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    first = value.split(",", 1)[0].strip()
    try:
        return int(first, 0)
    except ValueError:
        try:
            return int(first)
        except ValueError:
            return None


def _float(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    try:
        return float(value.split(",", 1)[0])
    except ValueError:
        return None


def _flag(value: str | None) -> bool:
    return value is not None and value.strip().lower() not in {"", "0", "false", "no"}


def _direction(value: str | None) -> Literal["tx", "rx"] | None:
    normalized = (value or "").strip().lower()
    if normalized in {"2", "outbound", "sent"}:
        return "tx"
    if normalized in {"1", "inbound", "received"}:
        return "rx"
    return None


def _first(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.split(",", 1)[0].strip()
    return stripped or None


def _packet(row: dict[str, str]) -> PacketRecord | None:
    timestamp = _float(row.get("frame.time_epoch"))
    frame_len = _integer(row.get("frame.len"))
    captured_len = _integer(row.get("frame.cap_len"))
    if timestamp is None or frame_len is None or captured_len is None:
        return None

    ipv4_len = _integer(row.get("ip.len"))
    ipv6_payload_len = _integer(row.get("ipv6.plen"))
    if ipv4_len is not None:
        ip_version = 4
        network_bytes = ipv4_len
        src_ip = _first(row.get("ip.src"))
        dst_ip = _first(row.get("ip.dst"))
        ecn_value = _integer(row.get("ip.dsfield.ecn"))
    elif ipv6_payload_len is not None:
        ip_version = 6
        network_bytes = ipv6_payload_len + 40
        src_ip = _first(row.get("ipv6.src"))
        dst_ip = _first(row.get("ipv6.dst"))
        ecn_raw = _integer(row.get("ipv6.tclass"))
        ecn_value = ecn_raw & 0x3 if ecn_raw is not None else None
    else:
        ip_version = None
        network_bytes = 0
        src_ip = None
        dst_ip = None
        ecn_value = None

    tcp_src = _integer(row.get("tcp.srcport"))
    tcp_dst = _integer(row.get("tcp.dstport"))
    udp_src = _integer(row.get("udp.srcport"))
    udp_dst = _integer(row.get("udp.dstport"))
    src_port: int | None
    dst_port: int | None
    if tcp_src is not None and tcp_dst is not None:
        transport: Literal["tcp", "udp", "other"] = "tcp"
        src_port, dst_port = tcp_src, tcp_dst
        payload_bytes = _integer(row.get("tcp.len")) or 0
    elif udp_src is not None and udp_dst is not None:
        transport = "udp"
        src_port, dst_port = udp_src, udp_dst
        udp_length = _integer(row.get("udp.length")) or 0
        payload_bytes = max(0, udp_length - 8)
    else:
        transport = "other"
        src_port = dst_port = None
        payload_bytes = 0

    protocols = (row.get("frame.protocols") or "").lower().split(":")
    is_quic = "quic" in protocols or any(
        _first(row.get(name)) is not None
        for name in ("quic.version", "quic.long.packet_type", "quic.spin_bit")
    )
    dns_response = _integer(row.get("dns.flags.response"))
    icmp_type = _integer(row.get("icmp.type"))
    icmpv6_type = _integer(row.get("icmpv6.type"))
    return PacketRecord(
        timestamp=timestamp,
        frame_len=frame_len,
        captured_len=captured_len,
        direction=_direction(row.get("frame.packet_flags_direction")),
        ip_version=ip_version,
        network_bytes=max(0, network_bytes),
        src_ip=src_ip,
        dst_ip=dst_ip,
        src_port=src_port,
        dst_port=dst_port,
        transport=transport,
        l4_payload_bytes=max(0, payload_bytes),
        tcp_stream=_integer(row.get("tcp.stream")),
        ip_fragment=(
            _flag(row.get("ip.flags.mf"))
            or (_integer(row.get("ip.frag_offset")) or 0) > 0
            or _first(row.get("ipv6.fraghdr.offset")) is not None
        ),
        ecn_ce=ecn_value == 3,
        icmp_error=(
            icmp_type in ICMPV4_ERROR_TYPES or (icmpv6_type is not None and icmpv6_type < 128)
        ),
        tcp_syn=_flag(row.get("tcp.flags.syn")),
        tcp_ack=_flag(row.get("tcp.flags.ack")),
        tcp_rst=_flag(row.get("tcp.flags.reset")),
        tcp_window_size=_integer(row.get("tcp.window_size_value")),
        tcp_initial_rtt_seconds=_float(row.get("tcp.analysis.initial_rtt")),
        tcp_ack_rtt_seconds=_float(row.get("tcp.analysis.ack_rtt")),
        tcp_retransmission=_flag(row.get("tcp.analysis.retransmission")),
        tcp_fast_retransmission=_flag(row.get("tcp.analysis.fast_retransmission")),
        tcp_spurious_retransmission=_flag(row.get("tcp.analysis.spurious_retransmission")),
        tcp_zero_window=_flag(row.get("tcp.analysis.zero_window")),
        tcp_window_full=_flag(row.get("tcp.analysis.window_full")),
        tcp_out_of_order=_flag(row.get("tcp.analysis.out_of_order")),
        tcp_duplicate_ack=_flag(row.get("tcp.analysis.duplicate_ack")),
        tcp_lost_segment=_flag(row.get("tcp.analysis.lost_segment")),
        quic=is_quic,
        quic_version=_first(row.get("quic.version")),
        quic_packet_type=_first(row.get("quic.long.packet_type")),
        quic_spin_bit=_integer(row.get("quic.spin_bit")),
        dns_is_response=(bool(dns_response) if dns_response is not None else None),
        dns_response_code=_integer(row.get("dns.flags.rcode")),
        dns_time_seconds=_float(row.get("dns.time")),
    )


class TSharkEngine:
    def __init__(
        self,
        executable: str = "tshark",
        timeout_seconds: float = 120.0,
        max_parsed_records: int = 250_000,
    ) -> None:
        if not executable.strip():
            raise ValueError("TShark executable must not be blank")
        if timeout_seconds <= 0:
            raise ValueError("TShark timeout must be positive")
        if max_parsed_records <= 0:
            raise ValueError("maximum parsed record count must be positive")
        self._executable = executable
        self._timeout_seconds = timeout_seconds
        self._max_parsed_records = max_parsed_records
        self._version: str | None = None
        self._supported_fields: set[str] | None = None

    def validate_runtime(self) -> None:
        """Fail startup before Kafka consumption when TShark is unusable."""
        self.version()
        self.supported_fields()

    def version(self) -> str:
        if self._version is not None:
            return self._version
        result = self._run_metadata([self._executable, "--version"], "TShark version")
        first_line = result.stdout.splitlines()[0].strip() if result.stdout else ""
        if not first_line:
            raise RuntimeError("TShark returned an unknown version")
        self._version = first_line
        return first_line

    def supported_fields(self) -> set[str]:
        if self._supported_fields is not None:
            return set(self._supported_fields)
        result = self._run_metadata([self._executable, "-G", "fields"], "TShark fields")
        supported: set[str] = set()
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and parts[0] == "F":
                supported.add(parts[2])
        missing = REQUIRED_FIELDS - supported
        if missing:
            names = ", ".join(sorted(missing))
            raise RuntimeError(f"TShark is missing required fields: {names}")
        self._supported_fields = supported
        return set(supported)

    def _run_metadata(
        self, command: list[str], description: str
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout_seconds,
            )
        except FileNotFoundError as error:
            raise RuntimeError("TShark executable was not found") from error
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f"{description} command timed out") from error
        if result.returncode != 0:
            raise RuntimeError(f"{description} command failed")
        return result

    def read_packets(self, capture_path: Path) -> TsharkOutput:
        version = self.version()
        supported = self.supported_fields()
        selected_fields = [name for name in FIELD_NAMES if name in supported]
        command = [
            self._executable,
            "-n",
            "-r",
            str(capture_path),
            "-T",
            "fields",
            "-E",
            "separator=/t",
            "-E",
            "quote=d",
            "-E",
            "occurrence=f",
        ]
        for field_name in selected_fields:
            command.extend(("-e", field_name))

        timed_out = threading.Event()
        process: subprocess.Popen[str] | None = None

        def terminate_on_timeout() -> None:
            timed_out.set()
            if process is not None:
                process.kill()

        packets: list[PacketRecord] = []
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr:
            try:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=stderr,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    shell=False,
                )
            except FileNotFoundError as error:
                raise RuntimeError("TShark executable was not found") from error
            timer = threading.Timer(self._timeout_seconds, terminate_on_timeout)
            timer.start()
            try:
                assert process.stdout is not None
                reader: Iterator[list[str]] = iter(
                    csv.reader(process.stdout, delimiter="\t", quotechar='"')
                )
                for values in reader:
                    padded = values + [""] * max(0, len(selected_fields) - len(values))
                    packet = _packet(dict(zip(selected_fields, padded, strict=False)))
                    if packet is not None:
                        if len(packets) >= self._max_parsed_records:
                            process.kill()
                            process.wait()
                            raise TerminalAnalysisError(
                                "capture_resource_limit",
                                "Capture exceeds the configured parsed-record limit",
                            )
                        packets.append(packet)
                return_code = process.wait()
            finally:
                timer.cancel()
                if process.stdout is not None:
                    process.stdout.close()
            stderr.seek(0)
            stderr_text = stderr.read()
        if timed_out.is_set():
            raise TerminalAnalysisError("tshark_timeout", "TShark analysis timed out")
        if return_code != 0:
            detail = stderr_text.strip().splitlines()[-1] if stderr_text.strip() else ""
            message = "TShark could not parse the capture"
            if (
                "not a capture file" in detail.lower()
                or "appears to have been cut short" in detail.lower()
            ):
                raise TerminalAnalysisError("invalid_capture", message)
            raise TerminalAnalysisError("tshark_parse_failure", message)
        return TsharkOutput(version=version, supported_fields=supported, packets=packets)


def analyze_capture(
    path: str | Path,
    scenario: Literal["upload", "download"],
    transfer_file_size_bytes: int,
    *,
    declared_format: Literal["pcap", "pcapng"] | None = None,
    expected_sha256: str | None = None,
    engine: AnalysisEngine | None = None,
) -> AnalysisMetrics:
    """Verify and analyze one local capture without requiring Kafka or PostgreSQL."""
    capture_path = Path(path)
    if transfer_file_size_bytes <= 0:
        raise ValueError("transfer_file_size_bytes must be positive")
    actual_format = capture_format(capture_path)
    if declared_format is not None and actual_format != declared_format:
        raise TerminalAnalysisError(
            "invalid_capture", "Capture header does not match the declared format"
        )
    verified_sha256 = sha256_file(capture_path)
    if expected_sha256 is not None and verified_sha256 != expected_sha256:
        raise TerminalAnalysisError(
            "checksum_mismatch", "Capture SHA-256 does not match the registered hash"
        )
    selected_engine = engine or TSharkEngine()
    output = selected_engine.read_packets(capture_path)
    aggregated = aggregate_packets(
        output.packets,
        capture_format=actual_format,
        capture_size_bytes=capture_path.stat().st_size,
        scenario=scenario,
        transfer_file_size_bytes=transfer_file_size_bytes,
        supported_fields=output.supported_fields,
    )
    return AnalysisMetrics(
        analyzed_at=datetime.now(UTC),
        analyzer_version=ANALYZER_VERSION,
        tshark_version=output.version,
        verified_sha256=verified_sha256,
        capture=aggregated.capture,
        capabilities=aggregated.capabilities,
        traffic=aggregated.traffic,
        protocol_mix=aggregated.protocol_mix,
        ip=aggregated.ip,
        flow=aggregated.flow,
        transfer=aggregated.transfer,
        tcp=aggregated.tcp,
        quic=aggregated.quic,
        udp=aggregated.udp,
        dns=aggregated.dns,
    )

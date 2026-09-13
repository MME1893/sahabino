from __future__ import annotations

import hashlib
import io
import subprocess
from pathlib import Path

import pytest

from sahabino.network.analyzer import (
    FIELD_NAMES,
    REQUIRED_FIELDS,
    AnalysisEngine,
    TSharkEngine,
    TsharkOutput,
    _packet,
    analyze_capture,
    capture_format,
)
from sahabino.network.exceptions import TerminalAnalysisError
from sahabino.network.metrics import PacketRecord

PCAP_HEADER = bytes.fromhex(
    "d4c3b2a1"  # little-endian microsecond PCAP magic
    "02000400"  # version 2.4
    "00000000"  # timezone
    "00000000"  # timestamp accuracy
    "ffff0000"  # snap length
    "01000000"  # Ethernet
)


class FakeEngine(AnalysisEngine):
    def __init__(self, packets: list[PacketRecord]) -> None:
        self.packets = packets

    def read_packets(self, capture_path: Path) -> TsharkOutput:
        assert capture_path.exists()
        return TsharkOutput(
            version="TShark 4.4.0",
            supported_fields={"quic.version", "quic.long.packet_type"},
            packets=self.packets,
        )


def test_analyze_capture_verifies_hash_header_and_uses_shared_metrics(tmp_path: Path) -> None:
    capture = tmp_path / "synthetic.pcap"
    capture.write_bytes(PCAP_HEADER)
    expected = hashlib.sha256(PCAP_HEADER).hexdigest()
    packet = PacketRecord(
        timestamp=1.0,
        frame_len=60,
        captured_len=60,
        direction=None,
        ip_version=4,
        network_bytes=40,
        src_ip="192.0.2.1",
        dst_ip="198.51.100.1",
        src_port=1234,
        dst_port=443,
        transport="tcp",
        l4_payload_bytes=0,
    )

    result = analyze_capture(
        capture,
        "upload",
        10,
        declared_format="pcap",
        expected_sha256=expected,
        engine=FakeEngine([packet]),
    )

    assert result.verified_sha256 == expected
    assert result.tshark_version == "TShark 4.4.0"
    assert result.capture.capture_format == "pcap"
    assert result.capabilities.tcp_present is True


def test_capture_magic_and_declared_format_are_not_trusted_from_extension(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "misleading.pcapng"
    capture.write_bytes(PCAP_HEADER)

    assert capture_format(capture) == "pcap"
    with pytest.raises(TerminalAnalysisError) as error:
        analyze_capture(
            capture,
            "upload",
            1,
            declared_format="pcapng",
            engine=FakeEngine([]),
        )
    assert error.value.code == "invalid_capture"


def test_checksum_mismatch_fails_before_packet_analysis(tmp_path: Path) -> None:
    capture = tmp_path / "synthetic.pcap"
    capture.write_bytes(PCAP_HEADER)

    with pytest.raises(TerminalAnalysisError) as error:
        analyze_capture(
            capture,
            "download",
            1,
            expected_sha256="0" * 64,
            engine=FakeEngine([]),
        )

    assert error.value.code == "checksum_mismatch"


def test_tshark_field_row_uses_ip_lengths_and_packet_direction() -> None:
    packet = _packet(
        {
            "frame.time_epoch": "1.25",
            "frame.len": "200",
            "frame.cap_len": "180",
            "frame.packet_flags_direction": "2",
            "frame.protocols": "eth:ip:tcp",
            "ip.len": "160",
            "ip.src": "10.0.0.2",
            "ip.dst": "203.0.113.1",
            "tcp.srcport": "50000",
            "tcp.dstport": "443",
            "tcp.stream": "7",
            "tcp.len": "100",
            "tcp.analysis.retransmission": "1",
            "ip.dsfield.ecn": "3",
        }
    )

    assert packet is not None
    assert packet.network_bytes == 160
    assert packet.frame_len == 200
    assert packet.direction == "tx"
    assert packet.l4_payload_bytes == 100
    assert packet.tcp_stream == 7
    assert packet.tcp_retransmission is True
    assert packet.ecn_ce is True


def test_tshark_field_row_only_classifies_icmp_error_types() -> None:
    common = {
        "frame.time_epoch": "1.25",
        "frame.len": "100",
        "frame.cap_len": "100",
        "frame.protocols": "eth:ip:icmp",
        "ip.len": "80",
        "ip.src": "203.0.113.1",
        "ip.dst": "10.0.0.2",
    }

    timestamp_request = _packet({**common, "icmp.type": "13"})
    destination_unreachable = _packet({**common, "icmp.type": "3"})

    assert timestamp_request is not None
    assert timestamp_request.icmp_error is False
    assert destination_unreachable is not None
    assert destination_unreachable.icmp_error is True


def _metadata_result(command: list[str], fields: set[str]) -> subprocess.CompletedProcess[str]:
    stdout = (
        "TShark 4.4.0\n"
        if "--version" in command
        else "".join(f"F\tfield\t{name}\n" for name in sorted(fields))
    )
    return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")


def test_tshark_runtime_validation_fails_when_executable_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(*args: object, **kwargs: object) -> None:
        _ = (args, kwargs)
        raise FileNotFoundError

    monkeypatch.setattr("sahabino.network.analyzer.subprocess.run", missing)

    with pytest.raises(RuntimeError, match="executable was not found"):
        TSharkEngine("missing-tshark").validate_runtime()


def test_tshark_runtime_validation_rejects_missing_required_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = TSharkEngine()
    fields = set(REQUIRED_FIELDS) - {"tcp.len"}
    monkeypatch.setattr(
        engine,
        "_run_metadata",
        lambda command, description: _metadata_result(command, fields),
    )

    with pytest.raises(RuntimeError, match="tcp.len"):
        engine.validate_runtime()


def test_tshark_runtime_validation_allows_optional_field_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = TSharkEngine()
    fields = set(REQUIRED_FIELDS)
    monkeypatch.setattr(
        engine,
        "_run_metadata",
        lambda command, description: _metadata_result(command, fields),
    )

    engine.validate_runtime()

    assert "tcp.stream" not in engine.supported_fields()


def test_tshark_record_limit_fails_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supported = set(REQUIRED_FIELDS) | {
        "frame.cap_len",
        "ip.src",
        "ip.dst",
        "tcp.srcport",
        "tcp.dstport",
    }
    selected = [name for name in FIELD_NAMES if name in supported]
    values = {
        "frame.time_epoch": "1.0",
        "frame.len": "60",
        "frame.cap_len": "60",
        "frame.protocols": "eth:ip:tcp",
        "ip.len": "40",
        "ip.src": "192.0.2.1",
        "ip.dst": "198.51.100.1",
        "tcp.srcport": "1234",
        "tcp.dstport": "443",
        "tcp.len": "0",
    }
    row = "\t".join(values.get(name, "") for name in selected) + "\n"

    class Process:
        def __init__(self) -> None:
            self.stdout = io.StringIO(row * 2)
            self.killed = False

        def kill(self) -> None:
            self.killed = True

        def wait(self) -> int:
            return 0

    process = Process()
    monkeypatch.setattr(
        "sahabino.network.analyzer.subprocess.Popen",
        lambda *args, **kwargs: process,
    )
    engine = TSharkEngine(max_parsed_records=1)
    engine._version = "TShark 4.4.0"
    engine._supported_fields = supported

    with pytest.raises(TerminalAnalysisError) as error:
        engine.read_packets(Path("oversized.pcap"))

    assert error.value.code == "capture_resource_limit"
    assert process.killed is True

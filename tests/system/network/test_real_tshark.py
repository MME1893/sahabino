from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from sahabino.network.analyzer import TSharkEngine, analyze_capture
from scripts.generate_network_fixture import build_fixture


@pytest.mark.system
def test_real_tshark_parses_synthetic_pcapng(tmp_path: Path) -> None:
    if shutil.which("tshark") is None:
        pytest.skip("TShark is not installed on this host; run in the analyzer image")
    capture = tmp_path / "synthetic.pcapng"
    capture.write_bytes(build_fixture())

    metrics = analyze_capture(
        capture,
        "upload",
        20,
        engine=TSharkEngine(timeout_seconds=30),
    )

    assert metrics.source_analyzer == "tshark"
    assert metrics.tshark_version.startswith("TShark")
    assert metrics.capture.packet_count == 6
    assert metrics.capabilities.direction_metadata_available is True
    assert metrics.capabilities.comparison_ready is True
    assert metrics.tcp is not None
    assert metrics.tcp.tcp_successful_handshake_count == 1
    assert metrics.transfer.effective_file_throughput_mbps is not None

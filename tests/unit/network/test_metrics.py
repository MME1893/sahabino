from __future__ import annotations

from dataclasses import replace

import pytest

from sahabino.network.metrics import PacketRecord, aggregate_packets, percentile


def _tcp_packet(timestamp: float, direction: str, **changes: object) -> PacketRecord:
    values: dict[str, object] = {
        "timestamp": timestamp,
        "frame_len": 140,
        "captured_len": 140,
        "direction": direction,
        "ip_version": 4,
        "network_bytes": 120,
        "src_ip": "10.0.0.2" if direction == "tx" else "203.0.113.5",
        "dst_ip": "203.0.113.5" if direction == "tx" else "10.0.0.2",
        "src_port": 50000 if direction == "tx" else 443,
        "dst_port": 443 if direction == "tx" else 50000,
        "transport": "tcp",
        "l4_payload_bytes": 0,
    }
    values.update(changes)
    return PacketRecord(**values)  # type: ignore[arg-type]


def test_tcp_metrics_and_presentation_kpis_are_aggregated() -> None:
    packets = [
        _tcp_packet(0.0, "tx", tcp_syn=True),
        _tcp_packet(0.1, "rx", tcp_syn=True, tcp_ack=True, tcp_initial_rtt_seconds=0.1),
        _tcp_packet(0.2, "tx", tcp_ack=True, tcp_ack_rtt_seconds=0.05),
        _tcp_packet(1.0, "tx", l4_payload_bytes=100),
        _tcp_packet(1.2, "rx", tcp_zero_window=True, tcp_window_size=0),
        _tcp_packet(1.5, "rx", tcp_window_size=4096),
        _tcp_packet(
            2.0,
            "tx",
            l4_payload_bytes=50,
            tcp_retransmission=True,
            tcp_fast_retransmission=True,
            tcp_out_of_order=True,
        ),
    ]

    result = aggregate_packets(
        packets,
        capture_format="pcapng",
        capture_size_bytes=1000,
        scenario="upload",
        transfer_file_size_bytes=100,
    )

    assert result.capabilities.comparison_ready is True
    assert result.capabilities.tcp_rtt_available is True
    assert result.traffic.network_bytes_total == 840
    assert result.traffic.tx_network_bytes == 480
    assert result.flow.flow_count == 1
    assert result.flow.flows_for_80pct_payload == 1
    assert result.flow.top_flow_payload_share == 1
    assert result.transfer.effective_file_throughput_mbps == pytest.approx(0.0008)
    assert result.transfer.primary_direction_amplification_ratio == 4.8
    assert result.transfer.total_transfer_amplification_ratio == 8.4
    assert result.transfer.reverse_path_cost_ratio == 0.75
    assert result.tcp is not None
    assert result.tcp.tcp_successful_handshake_count == 1
    assert result.tcp.tcp_initial_rtt_p50_ms == 100
    assert result.tcp.tcp_ack_rtt_p95_ms == 50
    assert result.tcp.tcp_retransmission_count == 1
    assert result.tcp.tcp_recovery_tax == pytest.approx(1 / 3)
    assert result.tcp.tcp_zero_window_duration_ms == pytest.approx(300)
    assert result.tcp.tcp_receiver_stall_ratio == pytest.approx(0.15)
    assert result.tcp.tcp_out_of_order_count == 1


def test_missing_direction_degrades_directional_metrics_without_fake_zero() -> None:
    packet = _tcp_packet(1.0, "tx", l4_payload_bytes=10)
    packet = replace(packet, direction=None)

    result = aggregate_packets(
        [packet],
        capture_format="pcap",
        capture_size_bytes=100,
        scenario="upload",
        transfer_file_size_bytes=10,
    )

    assert result.capabilities.direction_metadata_available is False
    assert result.capabilities.comparison_ready is False
    assert result.traffic.tx_network_bytes is None
    assert result.transfer.primary_direction_amplification_ratio is None
    assert "direction_unavailable" in result.capture.analysis_warnings


def test_tcp_handshake_requires_the_final_acknowledgment() -> None:
    result = aggregate_packets(
        [
            _tcp_packet(0.0, "tx", tcp_syn=True),
            _tcp_packet(0.1, "rx", tcp_syn=True, tcp_ack=True),
        ],
        capture_format="pcapng",
        capture_size_bytes=280,
        scenario="upload",
        transfer_file_size_bytes=100,
    )

    assert result.tcp is not None
    assert result.tcp.tcp_connection_count == 1
    assert result.tcp.tcp_successful_handshake_count == 0
    assert result.tcp.tcp_incomplete_handshake_count == 1
    assert result.tcp.tcp_handshake_success_rate == 0


def test_quic_only_has_no_tcp_loss_or_rtt_metrics() -> None:
    packets = [
        PacketRecord(
            timestamp=1.0,
            frame_len=100,
            captured_len=100,
            direction="tx",
            ip_version=4,
            network_bytes=80,
            src_ip="10.0.0.2",
            dst_ip="198.51.100.10",
            src_port=50000,
            dst_port=443,
            transport="udp",
            l4_payload_bytes=52,
            quic=True,
            quic_version="0x00000001",
            quic_packet_type="0",
        ),
        PacketRecord(
            timestamp=1.1,
            frame_len=100,
            captured_len=100,
            direction="rx",
            ip_version=4,
            network_bytes=80,
            src_ip="198.51.100.10",
            dst_ip="10.0.0.2",
            src_port=443,
            dst_port=50000,
            transport="udp",
            l4_payload_bytes=52,
            quic=True,
            quic_version="0x00000001",
            quic_packet_type="0",
        ),
    ]

    result = aggregate_packets(
        packets,
        capture_format="pcapng",
        capture_size_bytes=200,
        scenario="upload",
        transfer_file_size_bytes=52,
    )

    assert result.tcp is None
    assert result.capabilities.tcp_present is False
    assert result.quic is not None
    assert result.quic.quic_identified_connection_count == 1
    assert result.quic.quic_initial_rtt_p50_ms == pytest.approx(100)
    assert result.udp is None


def test_generic_udp_and_dns_metrics_do_not_invent_transport_loss() -> None:
    packets = [
        PacketRecord(
            timestamp=1.0,
            frame_len=80,
            captured_len=80,
            direction="tx",
            ip_version=4,
            network_bytes=60,
            src_ip="10.0.0.2",
            dst_ip="8.8.8.8",
            src_port=53000,
            dst_port=53,
            transport="udp",
            l4_payload_bytes=32,
            dns_is_response=False,
        ),
        PacketRecord(
            timestamp=1.05,
            frame_len=90,
            captured_len=90,
            direction="rx",
            ip_version=4,
            network_bytes=70,
            src_ip="8.8.8.8",
            dst_ip="10.0.0.2",
            src_port=53,
            dst_port=53000,
            transport="udp",
            l4_payload_bytes=42,
            dns_is_response=True,
            dns_response_code=0,
            dns_time_seconds=0.05,
        ),
    ]
    result = aggregate_packets(
        packets,
        capture_format="pcapng",
        capture_size_bytes=170,
        scenario="upload",
        transfer_file_size_bytes=32,
    )

    assert result.udp is not None
    assert result.udp.udp_datagram_count == 2
    assert result.udp.udp_payload_size_p95 == 42
    assert result.dns is not None
    assert result.dns.dns_rtt_p50_ms == 50
    assert result.tcp is None


def test_protocol_mix_and_ip_signal_denominators_are_explicit() -> None:
    packets = [
        PacketRecord(
            timestamp=0.0,
            frame_len=120,
            captured_len=120,
            direction="tx",
            ip_version=4,
            network_bytes=100,
            src_ip="10.0.0.2",
            dst_ip="203.0.113.1",
            src_port=50000,
            dst_port=443,
            transport="tcp",
            l4_payload_bytes=60,
        ),
        PacketRecord(
            timestamp=1.0,
            frame_len=100,
            captured_len=100,
            direction="rx",
            ip_version=6,
            network_bytes=80,
            src_ip="2001:db8::1",
            dst_ip="2001:db8::2",
            src_port=443,
            dst_port=50001,
            transport="udp",
            l4_payload_bytes=40,
            quic=True,
        ),
        PacketRecord(
            timestamp=2.0,
            frame_len=80,
            captured_len=80,
            direction="tx",
            ip_version=4,
            network_bytes=60,
            src_ip="10.0.0.2",
            dst_ip="198.51.100.1",
            src_port=50002,
            dst_port=123,
            transport="udp",
            l4_payload_bytes=20,
            ip_fragment=True,
            ecn_ce=True,
        ),
        PacketRecord(
            timestamp=3.0,
            frame_len=60,
            captured_len=60,
            direction="rx",
            ip_version=4,
            network_bytes=40,
            src_ip="198.51.100.2",
            dst_ip="10.0.0.2",
            src_port=None,
            dst_port=None,
            transport="other",
            l4_payload_bytes=0,
            icmp_error=True,
        ),
    ]

    result = aggregate_packets(
        packets,
        capture_format="pcapng",
        capture_size_bytes=360,
        scenario="upload",
        transfer_file_size_bytes=80,
    )

    assert result.protocol_mix.tcp_byte_share == pytest.approx(100 / 240)
    assert result.protocol_mix.quic_byte_share == pytest.approx(80 / 240)
    assert result.protocol_mix.other_udp_byte_share == pytest.approx(60 / 240)
    assert result.protocol_mix.ipv4_byte_share == pytest.approx(200 / 280)
    assert result.protocol_mix.ipv6_byte_share == pytest.approx(80 / 280)
    assert result.ip.ip_fragment_count == 1
    assert result.ip.ip_fragment_rate == pytest.approx(1 / 4)
    assert result.ip.ecn_ce_packet_count == 1
    assert result.ip.ecn_ce_packet_rate == pytest.approx(1 / 4)
    assert result.ip.icmp_error_count == 1


def test_percentile_uses_documented_nearest_rank_and_validates_input() -> None:
    assert percentile([40, 10, 30, 20], 50) == 20
    assert percentile([40, 10, 30, 20], 95) == 40
    assert percentile([], 95) is None
    with pytest.raises(ValueError):
        percentile([1], 101)


def test_tcp_overlapping_directional_zero_windows_are_not_double_counted() -> None:
    packets = [
        _tcp_packet(0.0, "tx", tcp_zero_window=True, tcp_window_size=0),
        _tcp_packet(0.2, "rx", tcp_zero_window=True, tcp_window_size=0),
        _tcp_packet(0.8, "tx", tcp_window_size=4096),
        _tcp_packet(1.0, "rx", tcp_window_size=4096),
    ]

    result = aggregate_packets(
        packets,
        capture_format="pcapng",
        capture_size_bytes=400,
        scenario="upload",
        transfer_file_size_bytes=100,
    )

    assert result.tcp is not None
    assert result.tcp.tcp_zero_window_duration_ms == pytest.approx(1000)
    assert result.tcp.tcp_receiver_stall_ratio == pytest.approx(1)


def test_tcp_stream_distinguishes_reused_connections_without_splitting_generic_flow() -> None:
    packets = [
        _tcp_packet(0.0, "tx", tcp_stream=4, l4_payload_bytes=10),
        _tcp_packet(1.0, "tx", tcp_stream=5, l4_payload_bytes=10),
    ]

    result = aggregate_packets(
        packets,
        capture_format="pcapng",
        capture_size_bytes=280,
        scenario="upload",
        transfer_file_size_bytes=20,
    )

    assert result.flow.flow_count == 1
    assert result.tcp is not None
    assert result.tcp.tcp_connection_count == 2

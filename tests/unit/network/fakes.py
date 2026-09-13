from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sahabino.messaging.network_events import (
    CaptureMetricsV1,
    FlowMetricsV1,
    IpMetricsV1,
    NetworkAnalysisCollectedV1,
    NetworkCapabilitiesV1,
    NetworkCaptureReadyV1,
    ProtocolMixMetricsV1,
    TrafficMetricsV1,
    TransferMetricsV1,
)

SHA256 = "a" * 64


def capture_ready_payload(
    *, application_id: UUID | None = None, capture_id: UUID | None = None
) -> NetworkCaptureReadyV1:
    return NetworkCaptureReadyV1(
        capture_id=capture_id or uuid4(),
        analysis_id=uuid4(),
        application_id=application_id or uuid4(),
        package_name="com.example.network",
        scenario="upload",
        object_key=f"captures/sha256/aa/{SHA256}.pcap",
        capture_format="pcap",
        capture_size_bytes=100,
        transfer_file_size_bytes=50,
        expected_sha256=SHA256,
        uploaded_at=datetime(2026, 9, 12, 8, tzinfo=UTC),
    )


def analysis_payload(
    *, application_id: UUID | None = None, capture_id: UUID | None = None
) -> NetworkAnalysisCollectedV1:
    return NetworkAnalysisCollectedV1(
        analysis_id=uuid4(),
        capture_id=capture_id or uuid4(),
        application_id=application_id or uuid4(),
        package_name="com.example.network",
        scenario="upload",
        analyzed_at=datetime(2026, 9, 12, 9, tzinfo=UTC),
        analyzer_version="1.0.0",
        tshark_version="TShark 4.4.0",
        verified_sha256=SHA256,
        capture=CaptureMetricsV1(
            capture_format="pcap",
            capture_size_bytes=100,
            transfer_file_size_bytes=50,
            capture_duration_ms=1000,
            packet_count=1,
            truncated_packet_count=0,
            analysis_warning_count=1,
            analysis_warnings=("direction_unavailable",),
        ),
        capabilities=NetworkCapabilitiesV1(
            direction_metadata_available=False,
            payload_plaintext_available=True,
            handshake_observed=False,
            tcp_present=False,
            quic_present=False,
            other_udp_present=False,
            tcp_rtt_available=False,
            quic_initial_rtt_available=False,
            quic_spin_rtt_available=False,
            dns_metrics_available=False,
            comparison_ready=False,
        ),
        traffic=TrafficMetricsV1(
            network_bytes_total=0,
            tx_network_bytes=None,
            rx_network_bytes=None,
            l4_payload_bytes_total=0,
            tx_l4_payload_bytes=None,
            rx_l4_payload_bytes=None,
            ip_transport_header_bytes=0,
            ip_transport_header_overhead_ratio=None,
            tx_packet_count=None,
            rx_packet_count=None,
        ),
        protocol_mix=ProtocolMixMetricsV1(
            tcp_bytes=0,
            quic_identified_bytes=0,
            other_udp_bytes=0,
            tcp_byte_share=None,
            quic_byte_share=None,
            other_udp_byte_share=None,
            ipv4_byte_share=None,
            ipv6_byte_share=None,
        ),
        ip=IpMetricsV1(
            ip_fragment_count=0,
            ip_fragment_rate=None,
            ecn_ce_packet_count=0,
            ecn_ce_packet_rate=None,
            icmp_error_count=0,
        ),
        flow=FlowMetricsV1(
            flow_count=0,
            unique_remote_ip_count=None,
            top_flow_payload_share=None,
            flows_for_80pct_payload=0,
            connection_churn_per_mib=0,
        ),
        transfer=TransferMetricsV1(
            observed_primary_payload_span_ms=None,
            average_network_throughput_mbps=0,
            peak_1s_network_throughput_mbps=0,
            p95_1s_network_throughput_mbps=0,
            effective_file_throughput_mbps=None,
            primary_direction_amplification_ratio=None,
            total_transfer_amplification_ratio=0,
            reverse_path_cost_ratio=None,
        ),
        tcp=None,
        quic=None,
        udp=None,
        dns=None,
    )

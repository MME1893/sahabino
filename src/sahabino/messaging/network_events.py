from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from sahabino.messaging.events import EventEnvelope

NETWORK_CAPTURE_READY_EVENT_TYPE = "network.capture.ready"
NETWORK_ANALYSIS_COLLECTED_EVENT_TYPE = "network.analysis.collected"
NETWORK_SCHEMA_VERSION = 1

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
NonBlankString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeFloat = Annotated[float, Field(ge=0)]
Ratio = Annotated[float, Field(ge=0, le=1)]
Scenario = Literal["upload", "download"]
CaptureFormat = Literal["pcap", "pcapng"]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("event timestamps must be timezone-aware")
    return value.astimezone(UTC)


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NetworkCaptureReadyV1(FrozenModel):
    capture_id: UUID
    analysis_id: UUID
    application_id: UUID
    package_name: NonBlankString
    scenario: Scenario
    object_key: NonBlankString
    capture_format: CaptureFormat
    capture_size_bytes: PositiveInt
    transfer_file_size_bytes: PositiveInt
    expected_sha256: Sha256
    uploaded_at: datetime

    @field_validator("uploaded_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        return _utc(value)


class CaptureMetricsV1(FrozenModel):
    capture_format: CaptureFormat
    capture_size_bytes: PositiveInt
    transfer_file_size_bytes: PositiveInt
    capture_duration_ms: NonNegativeFloat
    packet_count: NonNegativeInt
    truncated_packet_count: NonNegativeInt
    analysis_warning_count: NonNegativeInt
    analysis_warnings: tuple[NonBlankString, ...] = ()


class NetworkCapabilitiesV1(FrozenModel):
    direction_metadata_available: bool
    payload_plaintext_available: bool
    handshake_observed: bool
    tcp_present: bool
    quic_present: bool
    other_udp_present: bool
    tcp_rtt_available: bool
    quic_initial_rtt_available: bool
    quic_spin_rtt_available: bool
    dns_metrics_available: bool
    comparison_ready: bool


class TrafficMetricsV1(FrozenModel):
    network_bytes_total: NonNegativeInt
    tx_network_bytes: NonNegativeInt | None
    rx_network_bytes: NonNegativeInt | None
    l4_payload_bytes_total: NonNegativeInt
    tx_l4_payload_bytes: NonNegativeInt | None
    rx_l4_payload_bytes: NonNegativeInt | None
    ip_transport_header_bytes: NonNegativeInt
    ip_transport_header_overhead_ratio: Ratio | None
    tx_packet_count: NonNegativeInt | None
    rx_packet_count: NonNegativeInt | None


class ProtocolMixMetricsV1(FrozenModel):
    tcp_bytes: NonNegativeInt
    quic_identified_bytes: NonNegativeInt
    other_udp_bytes: NonNegativeInt
    tcp_byte_share: Ratio | None
    quic_byte_share: Ratio | None
    other_udp_byte_share: Ratio | None
    ipv4_byte_share: Ratio | None
    ipv6_byte_share: Ratio | None


class IpMetricsV1(FrozenModel):
    ip_fragment_count: NonNegativeInt
    ip_fragment_rate: Ratio | None
    ecn_ce_packet_count: NonNegativeInt
    ecn_ce_packet_rate: Ratio | None
    icmp_error_count: NonNegativeInt


class FlowMetricsV1(FrozenModel):
    flow_count: NonNegativeInt
    unique_remote_ip_count: NonNegativeInt | None
    top_flow_payload_share: Ratio | None
    flows_for_80pct_payload: NonNegativeInt
    connection_churn_per_mib: NonNegativeFloat


class TransferMetricsV1(FrozenModel):
    observed_primary_payload_span_ms: NonNegativeFloat | None
    average_network_throughput_mbps: NonNegativeFloat | None
    peak_1s_network_throughput_mbps: NonNegativeFloat | None
    p95_1s_network_throughput_mbps: NonNegativeFloat | None
    effective_file_throughput_mbps: NonNegativeFloat | None
    primary_direction_amplification_ratio: NonNegativeFloat | None
    total_transfer_amplification_ratio: NonNegativeFloat
    reverse_path_cost_ratio: NonNegativeFloat | None


class TcpMetricsV1(FrozenModel):
    tcp_connection_count: NonNegativeInt
    tcp_successful_handshake_count: NonNegativeInt
    tcp_incomplete_handshake_count: NonNegativeInt
    tcp_handshake_success_rate: Ratio | None
    tcp_initial_rtt_avg_ms: NonNegativeFloat | None
    tcp_initial_rtt_p50_ms: NonNegativeFloat | None
    tcp_initial_rtt_p95_ms: NonNegativeFloat | None
    tcp_ack_rtt_min_ms: NonNegativeFloat | None
    tcp_ack_rtt_p50_ms: NonNegativeFloat | None
    tcp_ack_rtt_p95_ms: NonNegativeFloat | None
    tcp_rtt_tail_inflation: NonNegativeFloat | None
    tcp_data_segment_count: NonNegativeInt
    tcp_retransmission_count: NonNegativeInt
    tcp_fast_retransmission_count: NonNegativeInt
    tcp_spurious_retransmission_count: NonNegativeInt
    tcp_retransmitted_payload_bytes: NonNegativeInt
    tcp_retransmission_rate: Ratio | None
    tcp_recovery_tax: Ratio | None
    tcp_zero_window_count: NonNegativeInt
    tcp_window_full_count: NonNegativeInt
    tcp_zero_window_duration_ms: NonNegativeFloat
    tcp_active_duration_ms: NonNegativeFloat
    tcp_receiver_stall_ratio: Ratio | None
    tcp_reset_count: NonNegativeInt
    tcp_reset_rate: Ratio | None
    tcp_out_of_order_count: NonNegativeInt
    tcp_duplicate_ack_count: NonNegativeInt
    tcp_lost_segment_indicator_count: NonNegativeInt


class QuicMetricsV1(FrozenModel):
    quic_identified_connection_count: NonNegativeInt | None
    quic_version_count: NonNegativeInt | None
    quic_versions_seen: tuple[str, ...] | None
    quic_retry_count: NonNegativeInt | None
    quic_version_negotiation_count: NonNegativeInt | None
    quic_0rtt_observed_count: NonNegativeInt | None
    quic_initial_rtt_avg_ms: NonNegativeFloat | None
    quic_initial_rtt_p50_ms: NonNegativeFloat | None
    quic_initial_rtt_p95_ms: NonNegativeFloat | None
    quic_spin_rtt_sample_count: NonNegativeInt | None
    quic_spin_rtt_min_ms: NonNegativeFloat | None
    quic_spin_rtt_p50_ms: NonNegativeFloat | None
    quic_spin_rtt_p95_ms: NonNegativeFloat | None


class UdpMetricsV1(FrozenModel):
    udp_flow_count: NonNegativeInt
    udp_datagram_count: NonNegativeInt
    udp_network_bytes: NonNegativeInt
    udp_payload_bytes: NonNegativeInt
    udp_payload_size_p50: NonNegativeFloat | None
    udp_payload_size_p95: NonNegativeFloat | None
    udp_bidirectional_byte_ratio: Ratio | None


class DnsMetricsV1(FrozenModel):
    dns_query_count: NonNegativeInt
    dns_response_count: NonNegativeInt
    dns_failure_count: NonNegativeInt
    dns_rtt_p50_ms: NonNegativeFloat | None
    dns_rtt_p95_ms: NonNegativeFloat | None


class NetworkAnalysisCollectedV1(FrozenModel):
    analysis_id: UUID
    capture_id: UUID
    application_id: UUID
    package_name: NonBlankString
    scenario: Scenario
    analyzed_at: datetime
    analyzer_version: NonBlankString
    tshark_version: NonBlankString
    source_analyzer: Literal["tshark"] = "tshark"
    verified_sha256: Sha256
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

    @field_validator("analyzed_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        return _utc(value)


def capture_ready_envelope(
    payload: NetworkCaptureReadyV1, *, event_id: UUID
) -> EventEnvelope[NetworkCaptureReadyV1]:
    return EventEnvelope(
        event_id=event_id,
        event_type=NETWORK_CAPTURE_READY_EVENT_TYPE,
        schema_version=NETWORK_SCHEMA_VERSION,
        occurred_at=payload.uploaded_at,
        payload=payload,
    )


def analysis_collected_envelope(
    payload: NetworkAnalysisCollectedV1, *, event_id: UUID
) -> EventEnvelope[NetworkAnalysisCollectedV1]:
    return EventEnvelope(
        event_id=event_id,
        event_type=NETWORK_ANALYSIS_COLLECTED_EVENT_TYPE,
        schema_version=NETWORK_SCHEMA_VERSION,
        occurred_at=payload.analyzed_at,
        payload=payload,
    )

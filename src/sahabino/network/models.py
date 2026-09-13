from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from sahabino.app_registry import models as registry_models
from sahabino.db.base import Base

_ = registry_models

_NON_NEGATIVE_ANALYSIS_COLUMNS = (
    "tx_network_bytes",
    "rx_network_bytes",
    "tx_l4_payload_bytes",
    "rx_l4_payload_bytes",
    "tx_packet_count",
    "rx_packet_count",
    "tcp_bytes",
    "quic_identified_bytes",
    "other_udp_bytes",
    "ecn_ce_packet_count",
    "icmp_error_count",
    "unique_remote_ip_count",
    "tcp_connection_count",
    "tcp_successful_handshake_count",
    "tcp_incomplete_handshake_count",
    "tcp_data_segment_count",
    "tcp_retransmission_count",
    "tcp_fast_retransmission_count",
    "tcp_spurious_retransmission_count",
    "tcp_retransmitted_payload_bytes",
    "tcp_zero_window_count",
    "tcp_window_full_count",
    "tcp_reset_count",
    "tcp_out_of_order_count",
    "tcp_duplicate_ack_count",
    "tcp_lost_segment_indicator_count",
    "quic_identified_connection_count",
    "quic_version_count",
    "quic_retry_count",
    "quic_version_negotiation_count",
    "quic_0rtt_observed_count",
    "quic_spin_rtt_sample_count",
    "udp_flow_count",
    "udp_datagram_count",
    "udp_network_bytes",
    "udp_payload_bytes",
    "dns_query_count",
    "dns_response_count",
    "dns_failure_count",
    "connection_churn_per_mib",
    "observed_primary_payload_span_ms",
    "average_network_throughput_mbps",
    "peak_1s_network_throughput_mbps",
    "p95_1s_network_throughput_mbps",
    "effective_file_throughput_mbps",
    "primary_direction_amplification_ratio",
    "total_transfer_amplification_ratio",
    "reverse_path_cost_ratio",
    "tcp_initial_rtt_avg_ms",
    "tcp_initial_rtt_p50_ms",
    "tcp_initial_rtt_p95_ms",
    "tcp_ack_rtt_min_ms",
    "tcp_ack_rtt_p50_ms",
    "tcp_ack_rtt_p95_ms",
    "tcp_rtt_tail_inflation",
    "tcp_zero_window_duration_ms",
    "tcp_active_duration_ms",
    "quic_initial_rtt_avg_ms",
    "quic_initial_rtt_p50_ms",
    "quic_initial_rtt_p95_ms",
    "quic_spin_rtt_min_ms",
    "quic_spin_rtt_p50_ms",
    "quic_spin_rtt_p95_ms",
    "udp_payload_size_p50",
    "udp_payload_size_p95",
    "dns_rtt_p50_ms",
    "dns_rtt_p95_ms",
)


class NetworkCapture(Base):
    __tablename__ = "network_captures"
    __table_args__ = (
        UniqueConstraint("ready_event_id", name="uq_network_captures_ready_event_id"),
        UniqueConstraint("analysis_id", name="uq_network_captures_analysis_id"),
        UniqueConstraint("analysis_event_id", name="uq_network_captures_analysis_event_id"),
        UniqueConstraint(
            "application_id",
            "scenario",
            "expected_sha256",
            name="uq_network_captures_application_scenario_sha256",
        ),
        CheckConstraint("scenario IN ('upload', 'download')", name="scenario"),
        CheckConstraint("capture_format IN ('pcap', 'pcapng')", name="capture_format"),
        CheckConstraint(
            "status IN ('pending_upload', 'uploaded', 'analyzing', 'analyzed', "
            "'failed', 'expired')",
            name="status",
        ),
        CheckConstraint("capture_size_bytes > 0", name="capture_size_positive"),
        CheckConstraint("transfer_file_size_bytes > 0", name="transfer_file_size_positive"),
        CheckConstraint("analysis_attempt_count >= 0", name="analysis_attempt_count_non_negative"),
        Index("ix_network_captures_application_id", "application_id"),
        Index("ix_network_captures_status", "status"),
        Index("ix_network_captures_created_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    application_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("applications.id", ondelete="RESTRICT")
    )
    scenario: Mapped[str] = mapped_column(Text)
    original_filename: Mapped[str] = mapped_column(Text)
    capture_format: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(Text)
    object_key: Mapped[str] = mapped_column(Text)
    expected_sha256: Mapped[str] = mapped_column(Text)
    verified_sha256: Mapped[str | None] = mapped_column(Text)
    capture_size_bytes: Mapped[int] = mapped_column(BigInteger)
    transfer_file_size_bytes: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(
        Text, default="pending_upload", server_default="pending_upload"
    )
    ready_event_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), default=uuid4)
    analysis_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), default=uuid4)
    analysis_event_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), default=uuid4)
    analysis_attempt_count: Mapped[int] = mapped_column(
        SmallInteger, default=0, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    analysis_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    analysis_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    object_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)


class NetworkCaptureIdempotencyKey(Base):
    __tablename__ = "network_capture_idempotency_keys"
    __table_args__ = (Index("ix_network_capture_idempotency_keys_capture_id", "capture_id"),)

    idempotency_key: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    capture_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("network_captures.id", ondelete="RESTRICT"),
    )
    request_fingerprint: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NetworkAnalysisResult(Base):
    __tablename__ = "network_analysis_results"
    __table_args__ = (
        UniqueConstraint("capture_id", name="uq_network_analysis_results_capture_id"),
        CheckConstraint("scenario IN ('upload', 'download')", name="scenario"),
        CheckConstraint("capture_format IN ('pcap', 'pcapng')", name="capture_format"),
        CheckConstraint("capture_size_bytes > 0", name="capture_size_positive"),
        CheckConstraint("transfer_file_size_bytes > 0", name="transfer_file_size_positive"),
        CheckConstraint("capture_duration_ms >= 0", name="capture_duration_non_negative"),
        CheckConstraint("packet_count >= 0", name="packet_count_non_negative"),
        CheckConstraint("truncated_packet_count >= 0", name="truncated_packets_non_negative"),
        CheckConstraint("analysis_warning_count >= 0", name="warning_count_non_negative"),
        CheckConstraint("network_bytes_total >= 0", name="network_bytes_non_negative"),
        CheckConstraint("l4_payload_bytes_total >= 0", name="payload_bytes_non_negative"),
        CheckConstraint("ip_transport_header_bytes >= 0", name="header_bytes_non_negative"),
        CheckConstraint("flow_count >= 0", name="flow_count_non_negative"),
        CheckConstraint("flows_for_80pct_payload >= 0", name="flows_80_non_negative"),
        CheckConstraint(
            "ip_transport_header_overhead_ratio IS NULL OR "
            "(ip_transport_header_overhead_ratio >= 0 AND "
            "ip_transport_header_overhead_ratio <= 1)",
            name="ip_transport_header_overhead_ratio_range",
        ),
        CheckConstraint(
            "tcp_byte_share IS NULL OR (tcp_byte_share >= 0 AND tcp_byte_share <= 1)",
            name="tcp_byte_share_range",
        ),
        CheckConstraint(
            "quic_byte_share IS NULL OR (quic_byte_share >= 0 AND quic_byte_share <= 1)",
            name="quic_byte_share_range",
        ),
        CheckConstraint(
            "other_udp_byte_share IS NULL OR "
            "(other_udp_byte_share >= 0 AND other_udp_byte_share <= 1)",
            name="other_udp_byte_share_range",
        ),
        CheckConstraint(
            "ipv4_byte_share IS NULL OR (ipv4_byte_share >= 0 AND ipv4_byte_share <= 1)",
            name="ipv4_byte_share_range",
        ),
        CheckConstraint(
            "ipv6_byte_share IS NULL OR (ipv6_byte_share >= 0 AND ipv6_byte_share <= 1)",
            name="ipv6_byte_share_range",
        ),
        CheckConstraint(
            "ip_fragment_rate IS NULL OR (ip_fragment_rate >= 0 AND ip_fragment_rate <= 1)",
            name="ip_fragment_rate_range",
        ),
        CheckConstraint(
            "ecn_ce_packet_rate IS NULL OR (ecn_ce_packet_rate >= 0 AND ecn_ce_packet_rate <= 1)",
            name="ecn_ce_packet_rate_range",
        ),
        CheckConstraint(
            "top_flow_payload_share IS NULL OR "
            "(top_flow_payload_share >= 0 AND top_flow_payload_share <= 1)",
            name="top_flow_payload_share_range",
        ),
        CheckConstraint(
            "tcp_handshake_success_rate IS NULL OR "
            "(tcp_handshake_success_rate >= 0 AND tcp_handshake_success_rate <= 1)",
            name="tcp_handshake_success_rate_range",
        ),
        CheckConstraint(
            "tcp_retransmission_rate IS NULL OR "
            "(tcp_retransmission_rate >= 0 AND tcp_retransmission_rate <= 1)",
            name="tcp_retransmission_rate_range",
        ),
        CheckConstraint(
            "tcp_recovery_tax IS NULL OR (tcp_recovery_tax >= 0 AND tcp_recovery_tax <= 1)",
            name="tcp_recovery_tax_range",
        ),
        CheckConstraint(
            "tcp_receiver_stall_ratio IS NULL OR "
            "(tcp_receiver_stall_ratio >= 0 AND tcp_receiver_stall_ratio <= 1)",
            name="tcp_receiver_stall_ratio_range",
        ),
        CheckConstraint(
            "tcp_reset_rate IS NULL OR (tcp_reset_rate >= 0 AND tcp_reset_rate <= 1)",
            name="tcp_reset_rate_range",
        ),
        CheckConstraint(
            "udp_bidirectional_byte_ratio IS NULL OR "
            "(udp_bidirectional_byte_ratio >= 0 AND udp_bidirectional_byte_ratio <= 1)",
            name="udp_bidirectional_byte_ratio_range",
        ),
        *(
            CheckConstraint(
                f"{column} IS NULL OR {column} >= 0",
                name=f"nn_{index}",
            )
            for index, column in enumerate(_NON_NEGATIVE_ANALYSIS_COLUMNS, start=1)
        ),
        Index("ix_network_analysis_results_application_id", "application_id"),
        Index("ix_network_analysis_results_analyzed_at", "analyzed_at"),
    )

    analysis_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    capture_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("network_captures.id", ondelete="RESTRICT"),
    )
    application_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("applications.id", ondelete="RESTRICT")
    )
    package_name: Mapped[str] = mapped_column(Text)
    scenario: Mapped[str] = mapped_column(Text)
    analyzed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    analyzer_version: Mapped[str] = mapped_column(Text)
    tshark_version: Mapped[str] = mapped_column(Text)
    source_analyzer: Mapped[str] = mapped_column(Text)
    verified_sha256: Mapped[str] = mapped_column(Text)
    capture_format: Mapped[str] = mapped_column(Text)
    capture_size_bytes: Mapped[int] = mapped_column(BigInteger)
    transfer_file_size_bytes: Mapped[int] = mapped_column(BigInteger)

    capture_duration_ms: Mapped[float] = mapped_column(Float)
    packet_count: Mapped[int] = mapped_column(BigInteger)
    truncated_packet_count: Mapped[int] = mapped_column(BigInteger)
    analysis_warning_count: Mapped[int] = mapped_column(BigInteger)
    direction_metadata_available: Mapped[bool] = mapped_column(Boolean)
    payload_plaintext_available: Mapped[bool] = mapped_column(Boolean)
    handshake_observed: Mapped[bool] = mapped_column(Boolean)
    tcp_present: Mapped[bool] = mapped_column(Boolean)
    quic_present: Mapped[bool] = mapped_column(Boolean)
    other_udp_present: Mapped[bool] = mapped_column(Boolean)
    tcp_rtt_available: Mapped[bool] = mapped_column(Boolean)
    quic_initial_rtt_available: Mapped[bool] = mapped_column(Boolean)
    quic_spin_rtt_available: Mapped[bool] = mapped_column(Boolean)
    dns_metrics_available: Mapped[bool] = mapped_column(Boolean)
    comparison_ready: Mapped[bool] = mapped_column(Boolean)
    analysis_warnings: Mapped[list[str]] = mapped_column(JSONB)

    network_bytes_total: Mapped[int] = mapped_column(BigInteger)
    tx_network_bytes: Mapped[int | None] = mapped_column(BigInteger)
    rx_network_bytes: Mapped[int | None] = mapped_column(BigInteger)
    l4_payload_bytes_total: Mapped[int] = mapped_column(BigInteger)
    tx_l4_payload_bytes: Mapped[int | None] = mapped_column(BigInteger)
    rx_l4_payload_bytes: Mapped[int | None] = mapped_column(BigInteger)
    ip_transport_header_bytes: Mapped[int] = mapped_column(BigInteger)
    ip_transport_header_overhead_ratio: Mapped[float | None] = mapped_column(Float)
    tx_packet_count: Mapped[int | None] = mapped_column(BigInteger)
    rx_packet_count: Mapped[int | None] = mapped_column(BigInteger)

    tcp_bytes: Mapped[int] = mapped_column(BigInteger)
    quic_identified_bytes: Mapped[int] = mapped_column(BigInteger)
    other_udp_bytes: Mapped[int] = mapped_column(BigInteger)
    tcp_byte_share: Mapped[float | None] = mapped_column(Float)
    quic_byte_share: Mapped[float | None] = mapped_column(Float)
    other_udp_byte_share: Mapped[float | None] = mapped_column(Float)
    ipv4_byte_share: Mapped[float | None] = mapped_column(Float)
    ipv6_byte_share: Mapped[float | None] = mapped_column(Float)

    ip_fragment_count: Mapped[int] = mapped_column(BigInteger)
    ip_fragment_rate: Mapped[float | None] = mapped_column(Float)
    ecn_ce_packet_count: Mapped[int] = mapped_column(BigInteger)
    ecn_ce_packet_rate: Mapped[float | None] = mapped_column(Float)
    icmp_error_count: Mapped[int] = mapped_column(BigInteger)

    flow_count: Mapped[int] = mapped_column(BigInteger)
    unique_remote_ip_count: Mapped[int | None] = mapped_column(BigInteger)
    top_flow_payload_share: Mapped[float | None] = mapped_column(Float)
    flows_for_80pct_payload: Mapped[int] = mapped_column(BigInteger)
    connection_churn_per_mib: Mapped[float] = mapped_column(Float)

    observed_primary_payload_span_ms: Mapped[float | None] = mapped_column(Float)
    average_network_throughput_mbps: Mapped[float | None] = mapped_column(Float)
    peak_1s_network_throughput_mbps: Mapped[float | None] = mapped_column(Float)
    p95_1s_network_throughput_mbps: Mapped[float | None] = mapped_column(Float)
    effective_file_throughput_mbps: Mapped[float | None] = mapped_column(Float)
    primary_direction_amplification_ratio: Mapped[float | None] = mapped_column(Float)
    total_transfer_amplification_ratio: Mapped[float] = mapped_column(Float)
    reverse_path_cost_ratio: Mapped[float | None] = mapped_column(Float)

    tcp_connection_count: Mapped[int | None] = mapped_column(BigInteger)
    tcp_successful_handshake_count: Mapped[int | None] = mapped_column(BigInteger)
    tcp_incomplete_handshake_count: Mapped[int | None] = mapped_column(BigInteger)
    tcp_handshake_success_rate: Mapped[float | None] = mapped_column(Float)
    tcp_initial_rtt_avg_ms: Mapped[float | None] = mapped_column(Float)
    tcp_initial_rtt_p50_ms: Mapped[float | None] = mapped_column(Float)
    tcp_initial_rtt_p95_ms: Mapped[float | None] = mapped_column(Float)
    tcp_ack_rtt_min_ms: Mapped[float | None] = mapped_column(Float)
    tcp_ack_rtt_p50_ms: Mapped[float | None] = mapped_column(Float)
    tcp_ack_rtt_p95_ms: Mapped[float | None] = mapped_column(Float)
    tcp_rtt_tail_inflation: Mapped[float | None] = mapped_column(Float)
    tcp_data_segment_count: Mapped[int | None] = mapped_column(BigInteger)
    tcp_retransmission_count: Mapped[int | None] = mapped_column(BigInteger)
    tcp_fast_retransmission_count: Mapped[int | None] = mapped_column(BigInteger)
    tcp_spurious_retransmission_count: Mapped[int | None] = mapped_column(BigInteger)
    tcp_retransmitted_payload_bytes: Mapped[int | None] = mapped_column(BigInteger)
    tcp_retransmission_rate: Mapped[float | None] = mapped_column(Float)
    tcp_recovery_tax: Mapped[float | None] = mapped_column(Float)
    tcp_zero_window_count: Mapped[int | None] = mapped_column(BigInteger)
    tcp_window_full_count: Mapped[int | None] = mapped_column(BigInteger)
    tcp_zero_window_duration_ms: Mapped[float | None] = mapped_column(Float)
    tcp_active_duration_ms: Mapped[float | None] = mapped_column(Float)
    tcp_receiver_stall_ratio: Mapped[float | None] = mapped_column(Float)
    tcp_reset_count: Mapped[int | None] = mapped_column(BigInteger)
    tcp_reset_rate: Mapped[float | None] = mapped_column(Float)
    tcp_out_of_order_count: Mapped[int | None] = mapped_column(BigInteger)
    tcp_duplicate_ack_count: Mapped[int | None] = mapped_column(BigInteger)
    tcp_lost_segment_indicator_count: Mapped[int | None] = mapped_column(BigInteger)

    quic_identified_connection_count: Mapped[int | None] = mapped_column(BigInteger)
    quic_version_count: Mapped[int | None] = mapped_column(BigInteger)
    quic_versions_seen: Mapped[list[str] | None] = mapped_column(JSONB)
    quic_retry_count: Mapped[int | None] = mapped_column(BigInteger)
    quic_version_negotiation_count: Mapped[int | None] = mapped_column(BigInteger)
    quic_0rtt_observed_count: Mapped[int | None] = mapped_column(BigInteger)
    quic_initial_rtt_avg_ms: Mapped[float | None] = mapped_column(Float)
    quic_initial_rtt_p50_ms: Mapped[float | None] = mapped_column(Float)
    quic_initial_rtt_p95_ms: Mapped[float | None] = mapped_column(Float)
    quic_spin_rtt_sample_count: Mapped[int | None] = mapped_column(BigInteger)
    quic_spin_rtt_min_ms: Mapped[float | None] = mapped_column(Float)
    quic_spin_rtt_p50_ms: Mapped[float | None] = mapped_column(Float)
    quic_spin_rtt_p95_ms: Mapped[float | None] = mapped_column(Float)

    udp_flow_count: Mapped[int | None] = mapped_column(BigInteger)
    udp_datagram_count: Mapped[int | None] = mapped_column(BigInteger)
    udp_network_bytes: Mapped[int | None] = mapped_column(BigInteger)
    udp_payload_bytes: Mapped[int | None] = mapped_column(BigInteger)
    udp_payload_size_p50: Mapped[float | None] = mapped_column(Float)
    udp_payload_size_p95: Mapped[float | None] = mapped_column(Float)
    udp_bidirectional_byte_ratio: Mapped[float | None] = mapped_column(Float)

    dns_query_count: Mapped[int | None] = mapped_column(BigInteger)
    dns_response_count: Mapped[int | None] = mapped_column(BigInteger)
    dns_failure_count: Mapped[int | None] = mapped_column(BigInteger)
    dns_rtt_p50_ms: Mapped[float | None] = mapped_column(Float)
    dns_rtt_p95_ms: Mapped[float | None] = mapped_column(Float)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

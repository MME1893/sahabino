"""add network capture lifecycle and analytical results

Revision ID: 20260912_0004
Revises: 20260911_0003
Create Date: 2026-09-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260912_0004"
down_revision: str | None = "20260911_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

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


def _capture_columns() -> list[sa.Column[object]]:
    return [
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("application_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scenario", sa.Text(), nullable=False),
        sa.Column("original_filename", sa.Text(), nullable=False),
        sa.Column("capture_format", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("expected_sha256", sa.Text(), nullable=False),
        sa.Column("verified_sha256", sa.Text(), nullable=True),
        sa.Column("capture_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("transfer_file_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending_upload'"), nullable=False),
        sa.Column("ready_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("analysis_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("analysis_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "analysis_attempt_count",
            sa.SmallInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("analysis_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("analysis_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("object_deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
    ]


def _idempotency_key_columns() -> list[sa.Column[object]]:
    return [
        sa.Column("idempotency_key", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("capture_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_fingerprint", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def _analysis_identity_columns() -> list[sa.Column[object]]:
    return [
        sa.Column("analysis_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("capture_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("application_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("package_name", sa.Text(), nullable=False),
        sa.Column("scenario", sa.Text(), nullable=False),
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("analyzer_version", sa.Text(), nullable=False),
        sa.Column("tshark_version", sa.Text(), nullable=False),
        sa.Column("source_analyzer", sa.Text(), nullable=False),
        sa.Column("verified_sha256", sa.Text(), nullable=False),
        sa.Column("capture_format", sa.Text(), nullable=False),
        sa.Column("capture_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("transfer_file_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("capture_duration_ms", sa.Float(), nullable=False),
        sa.Column("packet_count", sa.BigInteger(), nullable=False),
        sa.Column("truncated_packet_count", sa.BigInteger(), nullable=False),
        sa.Column("analysis_warning_count", sa.BigInteger(), nullable=False),
        sa.Column("direction_metadata_available", sa.Boolean(), nullable=False),
        sa.Column("payload_plaintext_available", sa.Boolean(), nullable=False),
        sa.Column("handshake_observed", sa.Boolean(), nullable=False),
        sa.Column("tcp_present", sa.Boolean(), nullable=False),
        sa.Column("quic_present", sa.Boolean(), nullable=False),
        sa.Column("other_udp_present", sa.Boolean(), nullable=False),
        sa.Column("tcp_rtt_available", sa.Boolean(), nullable=False),
        sa.Column("quic_initial_rtt_available", sa.Boolean(), nullable=False),
        sa.Column("quic_spin_rtt_available", sa.Boolean(), nullable=False),
        sa.Column("dns_metrics_available", sa.Boolean(), nullable=False),
        sa.Column("comparison_ready", sa.Boolean(), nullable=False),
        sa.Column("analysis_warnings", postgresql.JSONB(), nullable=False),
    ]


def _analysis_metric_columns() -> list[sa.Column[object]]:
    required_integers = (
        "network_bytes_total",
        "l4_payload_bytes_total",
        "ip_transport_header_bytes",
        "tcp_bytes",
        "quic_identified_bytes",
        "other_udp_bytes",
        "ip_fragment_count",
        "ecn_ce_packet_count",
        "icmp_error_count",
        "flow_count",
        "flows_for_80pct_payload",
    )
    optional_integers = (
        "tx_network_bytes",
        "rx_network_bytes",
        "tx_l4_payload_bytes",
        "rx_l4_payload_bytes",
        "tx_packet_count",
        "rx_packet_count",
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
    )
    required_floats = ("connection_churn_per_mib", "total_transfer_amplification_ratio")
    optional_floats = (
        "ip_transport_header_overhead_ratio",
        "tcp_byte_share",
        "quic_byte_share",
        "other_udp_byte_share",
        "ipv4_byte_share",
        "ipv6_byte_share",
        "ip_fragment_rate",
        "ecn_ce_packet_rate",
        "top_flow_payload_share",
        "observed_primary_payload_span_ms",
        "average_network_throughput_mbps",
        "peak_1s_network_throughput_mbps",
        "p95_1s_network_throughput_mbps",
        "effective_file_throughput_mbps",
        "primary_direction_amplification_ratio",
        "reverse_path_cost_ratio",
        "tcp_handshake_success_rate",
        "tcp_initial_rtt_avg_ms",
        "tcp_initial_rtt_p50_ms",
        "tcp_initial_rtt_p95_ms",
        "tcp_ack_rtt_min_ms",
        "tcp_ack_rtt_p50_ms",
        "tcp_ack_rtt_p95_ms",
        "tcp_rtt_tail_inflation",
        "tcp_retransmission_rate",
        "tcp_recovery_tax",
        "tcp_zero_window_duration_ms",
        "tcp_active_duration_ms",
        "tcp_receiver_stall_ratio",
        "tcp_reset_rate",
        "quic_initial_rtt_avg_ms",
        "quic_initial_rtt_p50_ms",
        "quic_initial_rtt_p95_ms",
        "quic_spin_rtt_min_ms",
        "quic_spin_rtt_p50_ms",
        "quic_spin_rtt_p95_ms",
        "udp_payload_size_p50",
        "udp_payload_size_p95",
        "udp_bidirectional_byte_ratio",
        "dns_rtt_p50_ms",
        "dns_rtt_p95_ms",
    )
    return [
        *[sa.Column(name, sa.BigInteger(), nullable=False) for name in required_integers],
        *[sa.Column(name, sa.BigInteger(), nullable=True) for name in optional_integers],
        *[sa.Column(name, sa.Float(), nullable=False) for name in required_floats],
        *[sa.Column(name, sa.Float(), nullable=True) for name in optional_floats],
        sa.Column("quic_versions_seen", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def _range_constraints() -> list[sa.CheckConstraint]:
    ratios = (
        "ip_transport_header_overhead_ratio",
        "tcp_byte_share",
        "quic_byte_share",
        "other_udp_byte_share",
        "ipv4_byte_share",
        "ipv6_byte_share",
        "ip_fragment_rate",
        "ecn_ce_packet_rate",
        "top_flow_payload_share",
        "tcp_handshake_success_rate",
        "tcp_retransmission_rate",
        "tcp_recovery_tax",
        "tcp_receiver_stall_ratio",
        "tcp_reset_rate",
        "udp_bidirectional_byte_ratio",
    )
    range_constraints = [
        sa.CheckConstraint(
            f"{name} IS NULL OR ({name} >= 0 AND {name} <= 1)",
            name=f"{name}_range",
        )
        for name in ratios
    ]
    non_negative_constraints = [
        sa.CheckConstraint(
            f"{name} IS NULL OR {name} >= 0",
            name=f"nn_{index}",
        )
        for index, name in enumerate(_NON_NEGATIVE_ANALYSIS_COLUMNS, start=1)
    ]
    return range_constraints + non_negative_constraints


def upgrade() -> None:
    op.create_table(
        "network_captures",
        *_capture_columns(),
        sa.CheckConstraint("scenario IN ('upload', 'download')", name="scenario"),
        sa.CheckConstraint("capture_format IN ('pcap', 'pcapng')", name="capture_format"),
        sa.CheckConstraint(
            "status IN ('pending_upload', 'uploaded', 'analyzing', 'analyzed', "
            "'failed', 'expired')",
            name="status",
        ),
        sa.CheckConstraint("capture_size_bytes > 0", name="capture_size_positive"),
        sa.CheckConstraint(
            "transfer_file_size_bytes > 0",
            name="transfer_file_size_positive",
        ),
        sa.CheckConstraint(
            "analysis_attempt_count >= 0",
            name="analysis_attempt_count_non_negative",
        ),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            name="fk_network_captures_application_id_applications",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_network_captures"),
        sa.UniqueConstraint("ready_event_id", name="uq_network_captures_ready_event_id"),
        sa.UniqueConstraint("analysis_id", name="uq_network_captures_analysis_id"),
        sa.UniqueConstraint("analysis_event_id", name="uq_network_captures_analysis_event_id"),
        sa.UniqueConstraint(
            "application_id",
            "scenario",
            "expected_sha256",
            name="uq_network_captures_application_scenario_sha256",
        ),
    )
    op.create_index("ix_network_captures_application_id", "network_captures", ["application_id"])
    op.create_index("ix_network_captures_status", "network_captures", ["status"])
    op.create_index("ix_network_captures_created_at", "network_captures", ["created_at"])

    op.create_table(
        "network_capture_idempotency_keys",
        *_idempotency_key_columns(),
        sa.ForeignKeyConstraint(
            ["capture_id"],
            ["network_captures.id"],
            name="fk_network_capture_idempotency_keys_capture_id_network_captures",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "idempotency_key",
            name="pk_network_capture_idempotency_keys",
        ),
    )
    op.create_index(
        "ix_network_capture_idempotency_keys_capture_id",
        "network_capture_idempotency_keys",
        ["capture_id"],
    )

    op.create_table(
        "network_analysis_results",
        *_analysis_identity_columns(),
        *_analysis_metric_columns(),
        *_range_constraints(),
        sa.CheckConstraint("scenario IN ('upload', 'download')", name="scenario"),
        sa.CheckConstraint(
            "capture_format IN ('pcap', 'pcapng')",
            name="capture_format",
        ),
        sa.CheckConstraint(
            "capture_size_bytes > 0",
            name="capture_size_positive",
        ),
        sa.CheckConstraint(
            "transfer_file_size_bytes > 0",
            name="transfer_file_size_positive",
        ),
        sa.CheckConstraint(
            "capture_duration_ms >= 0",
            name="capture_duration_non_negative",
        ),
        sa.CheckConstraint("packet_count >= 0", name="packet_count_non_negative"),
        sa.CheckConstraint(
            "truncated_packet_count >= 0",
            name="truncated_packets_non_negative",
        ),
        sa.CheckConstraint(
            "analysis_warning_count >= 0",
            name="warning_count_non_negative",
        ),
        sa.CheckConstraint(
            "network_bytes_total >= 0",
            name="network_bytes_non_negative",
        ),
        sa.CheckConstraint(
            "l4_payload_bytes_total >= 0",
            name="payload_bytes_non_negative",
        ),
        sa.CheckConstraint(
            "ip_transport_header_bytes >= 0",
            name="header_bytes_non_negative",
        ),
        sa.CheckConstraint("flow_count >= 0", name="flow_count_non_negative"),
        sa.CheckConstraint(
            "flows_for_80pct_payload >= 0",
            name="flows_80_non_negative",
        ),
        sa.ForeignKeyConstraint(
            ["capture_id"],
            ["network_captures.id"],
            name="fk_network_analysis_results_capture_id_network_captures",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            name="fk_network_analysis_results_application_id_applications",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("analysis_id", name="pk_network_analysis_results"),
        sa.UniqueConstraint("capture_id", name="uq_network_analysis_results_capture_id"),
    )
    op.create_index(
        "ix_network_analysis_results_application_id",
        "network_analysis_results",
        ["application_id"],
    )
    op.create_index(
        "ix_network_analysis_results_analyzed_at",
        "network_analysis_results",
        ["analyzed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_network_analysis_results_analyzed_at", table_name="network_analysis_results")
    op.drop_index(
        "ix_network_analysis_results_application_id", table_name="network_analysis_results"
    )
    op.drop_table("network_analysis_results")
    op.drop_index(
        "ix_network_capture_idempotency_keys_capture_id",
        table_name="network_capture_idempotency_keys",
    )
    op.drop_table("network_capture_idempotency_keys")
    op.drop_index("ix_network_captures_created_at", table_name="network_captures")
    op.drop_index("ix_network_captures_status", table_name="network_captures")
    op.drop_index("ix_network_captures_application_id", table_name="network_captures")
    op.drop_table("network_captures")

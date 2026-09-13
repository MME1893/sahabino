from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

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

FlowKey = tuple[int, str, int, str, int, str]
TcpConnectionKey = tuple[Literal["stream"], int] | tuple[Literal["flow"], FlowKey]


@dataclass(frozen=True, slots=True)
class PacketRecord:
    timestamp: float
    frame_len: int
    captured_len: int
    direction: Literal["tx", "rx"] | None
    ip_version: int | None
    network_bytes: int
    src_ip: str | None
    dst_ip: str | None
    src_port: int | None
    dst_port: int | None
    transport: Literal["tcp", "udp", "other"]
    l4_payload_bytes: int
    tcp_stream: int | None = None
    ip_fragment: bool = False
    ecn_ce: bool = False
    icmp_error: bool = False
    tcp_syn: bool = False
    tcp_ack: bool = False
    tcp_rst: bool = False
    tcp_window_size: int | None = None
    tcp_initial_rtt_seconds: float | None = None
    tcp_ack_rtt_seconds: float | None = None
    tcp_retransmission: bool = False
    tcp_fast_retransmission: bool = False
    tcp_spurious_retransmission: bool = False
    tcp_zero_window: bool = False
    tcp_window_full: bool = False
    tcp_out_of_order: bool = False
    tcp_duplicate_ack: bool = False
    tcp_lost_segment: bool = False
    quic: bool = False
    quic_version: str | None = None
    quic_packet_type: str | None = None
    quic_spin_bit: int | None = None
    dns_is_response: bool | None = None
    dns_response_code: int | None = None
    dns_time_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class AggregatedMetrics:
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


def percentile(values: list[float], percentile_value: float) -> float | None:
    """Return a deterministic nearest-rank percentile."""
    if not values:
        return None
    if not 0 <= percentile_value <= 100:
        raise ValueError("percentile must be between 0 and 100")
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile_value / 100 * len(ordered)))
    return ordered[rank - 1]


def _ratio(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def _flow_key(packet: PacketRecord) -> FlowKey | None:
    if (
        packet.ip_version is None
        or packet.src_ip is None
        or packet.dst_ip is None
        or packet.src_port is None
        or packet.dst_port is None
        or packet.transport not in {"tcp", "udp"}
    ):
        return None
    left = (packet.src_ip, packet.src_port)
    right = (packet.dst_ip, packet.dst_port)
    endpoint_a, endpoint_b = sorted((left, right))
    return (
        packet.ip_version,
        endpoint_a[0],
        endpoint_a[1],
        endpoint_b[0],
        endpoint_b[1],
        packet.transport,
    )


def _flows_for_80_percent(flow_payload: dict[FlowKey, int], total_payload: int) -> int:
    if total_payload <= 0:
        return 0
    threshold = total_payload * 0.8
    accumulated = 0
    for index, payload_bytes in enumerate(sorted(flow_payload.values(), reverse=True), start=1):
        accumulated += payload_bytes
        if accumulated >= threshold:
            return index
    return len(flow_payload)


def _tcp_connection_key(packet: PacketRecord) -> TcpConnectionKey | None:
    if packet.tcp_stream is not None:
        return ("stream", packet.tcp_stream)
    flow_key = _flow_key(packet)
    if flow_key is None:
        return None
    return ("flow", flow_key)


def _tcp_handshake_completed(records: list[PacketRecord]) -> bool:
    for syn_index, syn in enumerate(records):
        if not syn.tcp_syn or syn.tcp_ack:
            continue
        initiator = (syn.src_ip, syn.src_port)
        responder = (syn.dst_ip, syn.dst_port)
        for syn_ack_index in range(syn_index + 1, len(records)):
            syn_ack = records[syn_ack_index]
            if not (syn_ack.tcp_syn and syn_ack.tcp_ack):
                continue
            if (syn_ack.src_ip, syn_ack.src_port) != responder or (
                syn_ack.dst_ip,
                syn_ack.dst_port,
            ) != initiator:
                continue
            for acknowledgment in records[syn_ack_index + 1 :]:
                if acknowledgment.tcp_syn or not acknowledgment.tcp_ack:
                    continue
                if (acknowledgment.src_ip, acknowledgment.src_port) == initiator and (
                    acknowledgment.dst_ip,
                    acknowledgment.dst_port,
                ) == responder:
                    return True
    return False


def _aggregate_tcp(
    packets: list[PacketRecord], warnings: set[str]
) -> tuple[TcpMetricsV1 | None, bool]:
    tcp_packets = [packet for packet in packets if packet.transport == "tcp"]
    if not tcp_packets:
        return None, False

    flow_packets: dict[TcpConnectionKey, list[PacketRecord]] = defaultdict(list)
    for packet in tcp_packets:
        key = _tcp_connection_key(packet)
        if key is not None:
            flow_packets[key].append(packet)

    successful = 0
    initial_rtts: list[float] = []
    reset_flows: set[TcpConnectionKey] = set()
    active_duration_ms = 0.0
    zero_window_duration_ms = 0.0
    for key, records in flow_packets.items():
        records.sort(key=lambda item: item.timestamp)
        if _tcp_handshake_completed(records):
            successful += 1
        for packet in records:
            if packet.tcp_initial_rtt_seconds is not None:
                initial_rtts.append(packet.tcp_initial_rtt_seconds * 1000)
                break
        if any(packet.tcp_rst for packet in records):
            reset_flows.add(key)
        active_duration_ms += max(0.0, (records[-1].timestamp - records[0].timestamp) * 1000)

        open_stalls: dict[tuple[str | None, int | None, str | None, int | None], float] = {}
        stall_intervals: list[tuple[float, float]] = []
        for packet in records:
            direction_key = (
                packet.src_ip,
                packet.src_port,
                packet.dst_ip,
                packet.dst_port,
            )
            if packet.tcp_zero_window:
                open_stalls.setdefault(direction_key, packet.timestamp)
            elif packet.tcp_window_size is not None and packet.tcp_window_size > 0:
                started = open_stalls.pop(direction_key, None)
                if started is not None:
                    stall_intervals.append((started, packet.timestamp))
        if open_stalls:
            warnings.add("tcp_zero_window_open_at_capture_end")
            flow_end = records[-1].timestamp
            stall_intervals.extend((started, flow_end) for started in open_stalls.values())
        # Merge intervals across both flow directions so simultaneous receiver pressure
        # cannot make stall duration exceed the flow's active wall-clock duration.
        merged: list[list[float]] = []
        for started, ended in sorted(stall_intervals):
            if ended <= started:
                continue
            if not merged or started > merged[-1][1]:
                merged.append([started, ended])
            else:
                merged[-1][1] = max(merged[-1][1], ended)
        zero_window_duration_ms += sum((ended - started) * 1000 for started, ended in merged)

    ack_rtts = [
        packet.tcp_ack_rtt_seconds * 1000
        for packet in tcp_packets
        if packet.tcp_ack_rtt_seconds is not None
    ]
    ack_p50 = percentile(ack_rtts, 50)
    ack_p95 = percentile(ack_rtts, 95)
    data_segments = sum(packet.l4_payload_bytes > 0 for packet in tcp_packets)
    retransmitted = [
        packet
        for packet in tcp_packets
        if packet.tcp_retransmission
        or packet.tcp_fast_retransmission
        or packet.tcp_spurious_retransmission
    ]
    total_tcp_payload = sum(packet.l4_payload_bytes for packet in tcp_packets)
    connection_count = len(flow_packets)
    if successful == 0:
        warnings.add("missing_tcp_handshake")
    metrics = TcpMetricsV1(
        tcp_connection_count=connection_count,
        tcp_successful_handshake_count=successful,
        tcp_incomplete_handshake_count=max(0, connection_count - successful),
        tcp_handshake_success_rate=_ratio(successful, connection_count),
        tcp_initial_rtt_avg_ms=(sum(initial_rtts) / len(initial_rtts) if initial_rtts else None),
        tcp_initial_rtt_p50_ms=percentile(initial_rtts, 50),
        tcp_initial_rtt_p95_ms=percentile(initial_rtts, 95),
        tcp_ack_rtt_min_ms=min(ack_rtts) if ack_rtts else None,
        tcp_ack_rtt_p50_ms=ack_p50,
        tcp_ack_rtt_p95_ms=ack_p95,
        tcp_rtt_tail_inflation=(
            ack_p95 / ack_p50 if ack_p95 is not None and ack_p50 not in {None, 0} else None
        ),
        tcp_data_segment_count=data_segments,
        tcp_retransmission_count=len(retransmitted),
        tcp_fast_retransmission_count=sum(packet.tcp_fast_retransmission for packet in tcp_packets),
        tcp_spurious_retransmission_count=sum(
            packet.tcp_spurious_retransmission for packet in tcp_packets
        ),
        tcp_retransmitted_payload_bytes=sum(packet.l4_payload_bytes for packet in retransmitted),
        tcp_retransmission_rate=_ratio(len(retransmitted), data_segments),
        tcp_recovery_tax=_ratio(
            sum(packet.l4_payload_bytes for packet in retransmitted), total_tcp_payload
        ),
        tcp_zero_window_count=sum(packet.tcp_zero_window for packet in tcp_packets),
        tcp_window_full_count=sum(packet.tcp_window_full for packet in tcp_packets),
        tcp_zero_window_duration_ms=zero_window_duration_ms,
        tcp_active_duration_ms=active_duration_ms,
        tcp_receiver_stall_ratio=_ratio(zero_window_duration_ms, active_duration_ms),
        tcp_reset_count=len(reset_flows),
        tcp_reset_rate=_ratio(len(reset_flows), connection_count),
        tcp_out_of_order_count=sum(packet.tcp_out_of_order for packet in tcp_packets),
        tcp_duplicate_ack_count=sum(packet.tcp_duplicate_ack for packet in tcp_packets),
        tcp_lost_segment_indicator_count=sum(packet.tcp_lost_segment for packet in tcp_packets),
    )
    return metrics, bool(initial_rtts or ack_rtts)


def _aggregate_quic(
    packets: list[PacketRecord], supported_fields: set[str]
) -> tuple[QuicMetricsV1 | None, bool, bool]:
    quic_packets = [packet for packet in packets if packet.quic]
    if not quic_packets:
        return None, False, False
    flow_packets: dict[FlowKey, list[PacketRecord]] = defaultdict(list)
    for packet in quic_packets:
        key = _flow_key(packet)
        if key is not None:
            flow_packets[key].append(packet)

    versions = sorted(
        {
            packet.quic_version
            for packet in quic_packets
            if packet.quic_version not in {None, "", "0", "0x00000000"}
        }
    )
    packet_types_available = "quic.long.packet_type" in supported_fields
    packet_types = [
        packet.quic_packet_type.lower()
        for packet in quic_packets
        if packet.quic_packet_type is not None
    ]
    retry_count = (
        sum(value in {"3", "retry"} for value in packet_types) if packet_types_available else None
    )
    version_negotiation_count = (
        sum(packet.quic_version in {"0", "0x00000000"} for packet in quic_packets)
        if "quic.version" in supported_fields
        else None
    )
    zero_rtt_count = (
        sum(value in {"1", "0-rtt", "0rtt", "0_rtt"} for value in packet_types)
        if packet_types_available
        else None
    )

    initial_rtts: list[float] = []
    spin_samples: list[float] = []
    for records in flow_packets.values():
        records.sort(key=lambda item: item.timestamp)
        first_by_direction: dict[str, float] = {}
        for packet in records:
            if (
                packet.direction is not None
                and packet.quic_packet_type is not None
                and packet.quic_packet_type.lower() in {"0", "initial"}
            ):
                first_by_direction.setdefault(packet.direction, packet.timestamp)
        if {"tx", "rx"} <= first_by_direction.keys():
            initial_rtts.append(abs(first_by_direction["rx"] - first_by_direction["tx"]) * 1000)

        previous: dict[str, tuple[int, float]] = {}
        for packet in records:
            if packet.direction is None or packet.quic_spin_bit is None:
                continue
            prior = previous.get(packet.direction)
            if prior is not None and prior[0] != packet.quic_spin_bit:
                spin_samples.append(max(0.0, (packet.timestamp - prior[1]) * 1000))
            previous[packet.direction] = (packet.quic_spin_bit, packet.timestamp)

    metrics = QuicMetricsV1(
        quic_identified_connection_count=len(flow_packets),
        quic_version_count=(len(versions) if "quic.version" in supported_fields else None),
        quic_versions_seen=(tuple(versions) if "quic.version" in supported_fields else None),
        quic_retry_count=retry_count,
        quic_version_negotiation_count=version_negotiation_count,
        quic_0rtt_observed_count=zero_rtt_count,
        quic_initial_rtt_avg_ms=(sum(initial_rtts) / len(initial_rtts) if initial_rtts else None),
        quic_initial_rtt_p50_ms=percentile(initial_rtts, 50),
        quic_initial_rtt_p95_ms=percentile(initial_rtts, 95),
        quic_spin_rtt_sample_count=(
            len(spin_samples) if "quic.spin_bit" in supported_fields else None
        ),
        quic_spin_rtt_min_ms=min(spin_samples) if spin_samples else None,
        quic_spin_rtt_p50_ms=percentile(spin_samples, 50),
        quic_spin_rtt_p95_ms=percentile(spin_samples, 95),
    )
    return metrics, bool(initial_rtts), bool(spin_samples)


def _aggregate_udp(packets: list[PacketRecord]) -> UdpMetricsV1 | None:
    udp_packets = [packet for packet in packets if packet.transport == "udp" and not packet.quic]
    if not udp_packets:
        return None
    flow_directions: dict[FlowKey, list[int]] = defaultdict(lambda: [0, 0])
    flow_keys: set[FlowKey] = set()
    for packet in udp_packets:
        key = _flow_key(packet)
        if key is None:
            continue
        flow_keys.add(key)
        source = (packet.src_ip, packet.src_port)
        endpoint_a = (key[1], key[2])
        direction_index = 0 if source == endpoint_a else 1
        flow_directions[key][direction_index] += packet.network_bytes
    direction_totals = [0, 0]
    for directional_bytes in flow_directions.values():
        direction_totals[0] += directional_bytes[0]
        direction_totals[1] += directional_bytes[1]
    largest_direction = max(direction_totals)
    payload_sizes = [float(packet.l4_payload_bytes) for packet in udp_packets]
    return UdpMetricsV1(
        udp_flow_count=len(flow_keys),
        udp_datagram_count=len(udp_packets),
        udp_network_bytes=sum(packet.network_bytes for packet in udp_packets),
        udp_payload_bytes=sum(packet.l4_payload_bytes for packet in udp_packets),
        udp_payload_size_p50=percentile(payload_sizes, 50),
        udp_payload_size_p95=percentile(payload_sizes, 95),
        udp_bidirectional_byte_ratio=(
            min(direction_totals) / largest_direction if largest_direction > 0 else None
        ),
    )


def _aggregate_dns(packets: list[PacketRecord]) -> DnsMetricsV1 | None:
    dns_packets = [packet for packet in packets if packet.dns_is_response is not None]
    if not dns_packets:
        return None
    rtts = [
        packet.dns_time_seconds * 1000
        for packet in dns_packets
        if packet.dns_time_seconds is not None
    ]
    return DnsMetricsV1(
        dns_query_count=sum(packet.dns_is_response is False for packet in dns_packets),
        dns_response_count=sum(packet.dns_is_response is True for packet in dns_packets),
        dns_failure_count=sum(
            packet.dns_is_response is True
            and packet.dns_response_code is not None
            and packet.dns_response_code != 0
            for packet in dns_packets
        ),
        dns_rtt_p50_ms=percentile(rtts, 50),
        dns_rtt_p95_ms=percentile(rtts, 95),
    )


def aggregate_packets(
    packets: list[PacketRecord],
    *,
    capture_format: Literal["pcap", "pcapng"],
    capture_size_bytes: int,
    scenario: Literal["upload", "download"],
    transfer_file_size_bytes: int,
    supported_fields: set[str] | None = None,
) -> AggregatedMetrics:
    supported_fields = supported_fields or {
        "quic.version",
        "quic.long.packet_type",
        "quic.spin_bit",
    }
    warnings: set[str] = set()
    timestamps = [packet.timestamp for packet in packets]
    duration_ms = max(0.0, (max(timestamps) - min(timestamps)) * 1000) if timestamps else 0.0
    truncated_count = sum(packet.captured_len < packet.frame_len for packet in packets)
    if truncated_count:
        warnings.add("capture_truncated")

    ip_packets = [packet for packet in packets if packet.ip_version in {4, 6}]
    direction_available = bool(ip_packets) and all(
        packet.direction is not None for packet in ip_packets
    )
    if not direction_available:
        warnings.add("direction_unavailable")
    network_bytes_total = sum(packet.network_bytes for packet in ip_packets)
    payload_total = sum(packet.l4_payload_bytes for packet in ip_packets)
    tx_packets = [packet for packet in ip_packets if packet.direction == "tx"]
    rx_packets = [packet for packet in ip_packets if packet.direction == "rx"]
    tx_network = sum(packet.network_bytes for packet in tx_packets) if direction_available else None
    rx_network = sum(packet.network_bytes for packet in rx_packets) if direction_available else None
    tx_payload = (
        sum(packet.l4_payload_bytes for packet in tx_packets) if direction_available else None
    )
    rx_payload = (
        sum(packet.l4_payload_bytes for packet in rx_packets) if direction_available else None
    )
    header_bytes = max(0, network_bytes_total - payload_total)

    flow_payload: dict[FlowKey, int] = defaultdict(int)
    remote_ips: set[str] = set()
    for packet in ip_packets:
        key = _flow_key(packet)
        if key is not None:
            flow_payload[key] += packet.l4_payload_bytes
        if direction_available:
            remote = packet.dst_ip if packet.direction == "tx" else packet.src_ip
            if remote:
                remote_ips.add(remote)
    flow_count = len(flow_payload)

    tcp_bytes = sum(packet.network_bytes for packet in ip_packets if packet.transport == "tcp")
    quic_bytes = sum(packet.network_bytes for packet in ip_packets if packet.quic)
    other_udp_bytes = sum(
        packet.network_bytes
        for packet in ip_packets
        if packet.transport == "udp" and not packet.quic
    )
    transport_bytes = tcp_bytes + quic_bytes + other_udp_bytes
    ipv4_bytes = sum(packet.network_bytes for packet in ip_packets if packet.ip_version == 4)
    ipv6_bytes = sum(packet.network_bytes for packet in ip_packets if packet.ip_version == 6)

    one_second_bytes: dict[int, int] = defaultdict(int)
    for packet in ip_packets:
        one_second_bytes[math.floor(packet.timestamp)] += packet.network_bytes
    throughput_windows = [value * 8 / 1_000_000 for value in one_second_bytes.values()]
    duration_seconds = duration_ms / 1000
    primary_direction = "tx" if scenario == "upload" else "rx"
    primary_payload_times = [
        packet.timestamp
        for packet in ip_packets
        if packet.direction == primary_direction and packet.l4_payload_bytes > 0
    ]
    primary_span_ms: float | None = None
    if direction_available and primary_payload_times:
        primary_span_ms = max(primary_payload_times) - min(primary_payload_times)
        primary_span_ms *= 1000
        if primary_span_ms <= 0:
            warnings.add("zero_transfer_span")
            primary_span_ms = None
    else:
        warnings.add("no_primary_payload")

    primary_network = tx_network if scenario == "upload" else rx_network
    reverse_network = rx_network if scenario == "upload" else tx_network
    tcp, tcp_rtt_available = _aggregate_tcp(ip_packets, warnings)
    quic, quic_initial_available, quic_spin_available = _aggregate_quic(
        ip_packets, supported_fields
    )
    udp = _aggregate_udp(ip_packets)
    dns = _aggregate_dns(packets)
    if quic is not None and not quic_spin_available:
        warnings.add("quic_spin_unavailable")
    if dns is not None and dns.dns_query_count != dns.dns_response_count:
        warnings.add("dns_incomplete")

    comparison_ready = (
        bool(packets)
        and duration_ms > 0
        and truncated_count == 0
        and direction_available
        and primary_span_ms is not None
    )
    warning_tuple = tuple(sorted(warnings))
    capture = CaptureMetricsV1(
        capture_format=capture_format,
        capture_size_bytes=capture_size_bytes,
        transfer_file_size_bytes=transfer_file_size_bytes,
        capture_duration_ms=duration_ms,
        packet_count=len(packets),
        truncated_packet_count=truncated_count,
        analysis_warning_count=len(warning_tuple),
        analysis_warnings=warning_tuple,
    )
    capabilities = NetworkCapabilitiesV1(
        direction_metadata_available=direction_available,
        payload_plaintext_available=True,
        handshake_observed=(tcp is not None and tcp.tcp_successful_handshake_count > 0),
        tcp_present=tcp is not None,
        quic_present=quic is not None,
        other_udp_present=udp is not None,
        tcp_rtt_available=tcp_rtt_available,
        quic_initial_rtt_available=quic_initial_available,
        quic_spin_rtt_available=quic_spin_available,
        dns_metrics_available=dns is not None,
        comparison_ready=comparison_ready,
    )
    traffic = TrafficMetricsV1(
        network_bytes_total=network_bytes_total,
        tx_network_bytes=tx_network,
        rx_network_bytes=rx_network,
        l4_payload_bytes_total=payload_total,
        tx_l4_payload_bytes=tx_payload,
        rx_l4_payload_bytes=rx_payload,
        ip_transport_header_bytes=header_bytes,
        ip_transport_header_overhead_ratio=_ratio(header_bytes, network_bytes_total),
        tx_packet_count=len(tx_packets) if direction_available else None,
        rx_packet_count=len(rx_packets) if direction_available else None,
    )
    protocol_mix = ProtocolMixMetricsV1(
        tcp_bytes=tcp_bytes,
        quic_identified_bytes=quic_bytes,
        other_udp_bytes=other_udp_bytes,
        tcp_byte_share=_ratio(tcp_bytes, transport_bytes),
        quic_byte_share=_ratio(quic_bytes, transport_bytes),
        other_udp_byte_share=_ratio(other_udp_bytes, transport_bytes),
        ipv4_byte_share=_ratio(ipv4_bytes, network_bytes_total),
        ipv6_byte_share=_ratio(ipv6_bytes, network_bytes_total),
    )
    ip_metrics = IpMetricsV1(
        ip_fragment_count=sum(packet.ip_fragment for packet in ip_packets),
        ip_fragment_rate=_ratio(sum(packet.ip_fragment for packet in ip_packets), len(ip_packets)),
        ecn_ce_packet_count=sum(packet.ecn_ce for packet in ip_packets),
        ecn_ce_packet_rate=_ratio(sum(packet.ecn_ce for packet in ip_packets), len(ip_packets)),
        icmp_error_count=sum(packet.icmp_error for packet in packets),
    )
    flow = FlowMetricsV1(
        flow_count=flow_count,
        unique_remote_ip_count=len(remote_ips) if direction_available else None,
        top_flow_payload_share=(
            _ratio(max(flow_payload.values()), payload_total) if flow_payload else None
        ),
        flows_for_80pct_payload=_flows_for_80_percent(flow_payload, payload_total),
        connection_churn_per_mib=flow_count / (transfer_file_size_bytes / 1_048_576),
    )
    transfer = TransferMetricsV1(
        observed_primary_payload_span_ms=primary_span_ms,
        average_network_throughput_mbps=(
            network_bytes_total * 8 / duration_seconds / 1_000_000 if duration_seconds > 0 else None
        ),
        peak_1s_network_throughput_mbps=(max(throughput_windows) if throughput_windows else None),
        p95_1s_network_throughput_mbps=percentile(throughput_windows, 95),
        effective_file_throughput_mbps=(
            transfer_file_size_bytes * 8 / (primary_span_ms / 1000) / 1_000_000
            if primary_span_ms is not None
            else None
        ),
        primary_direction_amplification_ratio=(
            primary_network / transfer_file_size_bytes if primary_network is not None else None
        ),
        total_transfer_amplification_ratio=network_bytes_total / transfer_file_size_bytes,
        reverse_path_cost_ratio=(
            _ratio(reverse_network, primary_network)
            if reverse_network is not None and primary_network is not None
            else None
        ),
    )
    return AggregatedMetrics(
        capture=capture,
        capabilities=capabilities,
        traffic=traffic,
        protocol_mix=protocol_mix,
        ip=ip_metrics,
        flow=flow,
        transfer=transfer,
        tcp=tcp,
        quic=quic,
        udp=udp,
        dns=dns,
    )

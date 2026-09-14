#!/usr/bin/env python3
"""Standalone LAN UDP server for the adaptive Wi-Fi probe experiment.

Wire format (32 bytes, network byte order)::

    magic[4] version:u8 type:u8 status:u8 test_kind:u8
    session_id:u64 test_id:u64 sequence:u32 profile_id:u8
    candidate_id:u16 reserved:u8

The server acknowledges PROBE_SEND immediately and remembers its
``(session_id, test_id)``.  A later QUERY_RESULT returns whether that probe was
seen.  No external services or third-party Python packages are required.
"""

from __future__ import annotations

import argparse
import csv
import enum
import ipaddress
import signal
import socket
import struct
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple


MAGIC = b"AWP1"
VERSION = 1
PACKET = struct.Struct("!4sBBBBQQIBHB")
DEFAULT_PORT = 33333


class MessageType(enum.IntEnum):
    PROBE_SEND = 1
    PROBE_ACK = 2
    QUERY_RESULT = 3
    QUERY_RESULT_REPLY = 4


class ResultStatus(enum.IntEnum):
    NONE = 0
    RECEIVED = 1
    NOT_FOUND = 2


@dataclass(frozen=True)
class ProbePacket:
    message_type: MessageType
    session_id: int
    test_id: int
    sequence: int
    profile_id: int
    test_kind: int
    candidate_id: int = 0
    status: ResultStatus = ResultStatus.NONE

    def encode(self) -> bytes:
        return PACKET.pack(
            MAGIC,
            VERSION,
            int(self.message_type),
            int(self.status),
            self.test_kind,
            self.session_id,
            self.test_id,
            self.sequence,
            self.profile_id,
            self.candidate_id,
            0,
        )

    @classmethod
    def decode(cls, data: bytes) -> "ProbePacket":
        if len(data) != PACKET.size:
            raise ValueError(f"packet length {len(data)} != {PACKET.size}")
        (
            magic,
            version,
            message_type,
            status,
            test_kind,
            session_id,
            test_id,
            sequence,
            profile_id,
            candidate_id,
            reserved,
        ) = PACKET.unpack(data)
        if magic != MAGIC:
            raise ValueError("bad protocol magic")
        if version != VERSION:
            raise ValueError(f"unsupported protocol version {version}")
        if reserved != 0:
            raise ValueError("reserved byte must be zero")
        try:
            kind = MessageType(message_type)
            result = ResultStatus(status)
        except ValueError as exc:
            raise ValueError(f"invalid enum value: {exc}") from exc
        return cls(
            message_type=kind,
            status=result,
            test_kind=test_kind,
            session_id=session_id,
            test_id=test_id,
            sequence=sequence,
            profile_id=profile_id,
            candidate_id=candidate_id,
        )

    def reply(self, message_type: MessageType, status: ResultStatus) -> "ProbePacket":
        return ProbePacket(
            message_type=message_type,
            status=status,
            test_kind=self.test_kind,
            session_id=self.session_id,
            test_id=self.test_id,
            sequence=self.sequence,
            profile_id=self.profile_id,
            candidate_id=self.candidate_id,
        )


@dataclass
class StoredResult:
    received_monotonic: float
    sequence: int
    profile_id: int
    test_kind: int
    candidate_id: int
    duplicates: int = 0


@dataclass
class SessionStats:
    received: int = 0
    missing: int = 0
    duplicates: int = 0
    queries: int = 0
    last_sequence: Optional[int] = None
    run: int = 1


class ResultTable:
    """TTL- and count-bounded store of logical probe results."""

    def __init__(self, ttl_seconds: float, max_results: int) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_results <= 0:
            raise ValueError("max_results must be positive")
        self.ttl_seconds = ttl_seconds
        self.max_results = max_results
        self.results: "OrderedDict[Tuple[int, int], StoredResult]" = OrderedDict()
        self.sessions: Dict[int, SessionStats] = {}

    def _prune(self, now: float) -> None:
        expiry = now - self.ttl_seconds
        while self.results:
            _, oldest = next(iter(self.results.items()))
            if oldest.received_monotonic >= expiry:
                break
            self.results.popitem(last=False)
        while len(self.results) > self.max_results:
            self.results.popitem(last=False)

    def record(self, packet: ProbePacket, now: float) -> Tuple[bool, SessionStats, bool]:
        self._prune(now)
        key = (packet.session_id, packet.test_id)
        stats = self.sessions.setdefault(packet.session_id, SessionStats())
        existing = self.results.get(key)
        if existing is not None:
            existing.duplicates += 1
            stats.duplicates += 1
            return True, stats, False

        restarted = stats.last_sequence is not None and packet.sequence < stats.last_sequence
        if restarted:
            stats.received = 0
            stats.missing = 0
            stats.duplicates = 0
            stats.queries = 0
            stats.run += 1
            stats.last_sequence = None

        if stats.last_sequence is not None and packet.sequence > stats.last_sequence + 1:
            stats.missing += packet.sequence - stats.last_sequence - 1
        stats.last_sequence = packet.sequence
        stats.received += 1
        self.results[key] = StoredResult(
            received_monotonic=now,
            sequence=packet.sequence,
            profile_id=packet.profile_id,
            test_kind=packet.test_kind,
            candidate_id=packet.candidate_id,
        )
        self._prune(now)
        return False, stats, restarted

    def query(self, packet: ProbePacket, now: float) -> Tuple[bool, SessionStats]:
        self._prune(now)
        stats = self.sessions.setdefault(packet.session_id, SessionStats())
        stats.queries += 1
        return (packet.session_id, packet.test_id) in self.results, stats


LOG_FIELDS = (
    "utc_time",
    "event",
    "client_ip",
    "client_port",
    "session_id",
    "test_id",
    "sequence",
    "profile_id",
    "test_kind",
    "candidate_id",
    "status",
    "duplicate",
    "run_restarted",
    "run",
    "received",
    "missing",
    "duplicates",
    "queries",
)


class TsvLogger:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not path.exists() or path.stat().st_size == 0
        self._stream = path.open("a", encoding="utf-8", newline="", buffering=1)
        self._writer = csv.DictWriter(self._stream, fieldnames=LOG_FIELDS, delimiter="\t")
        if is_new:
            self._writer.writeheader()
            self._stream.flush()

    def write(
        self,
        event: str,
        packet: ProbePacket,
        address: Tuple[str, int],
        stats: SessionStats,
        *,
        status: ResultStatus,
        duplicate: bool = False,
        restarted: bool = False,
    ) -> None:
        self._writer.writerow(
            {
                "utc_time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "event": event,
                "client_ip": address[0],
                "client_port": address[1],
                "session_id": packet.session_id,
                "test_id": packet.test_id,
                "sequence": packet.sequence,
                "profile_id": packet.profile_id,
                "test_kind": packet.test_kind,
                "candidate_id": packet.candidate_id,
                "status": status.name,
                "duplicate": int(duplicate),
                "run_restarted": int(restarted),
                "run": stats.run,
                "received": stats.received,
                "missing": stats.missing,
                "duplicates": stats.duplicates,
                "queries": stats.queries,
            }
        )
        self._stream.flush()

    def close(self) -> None:
        self._stream.close()


class ProbeServer:
    def __init__(
        self,
        bind: str,
        port: int,
        log_path: Path,
        ttl_seconds: float,
        max_results: int,
        quiet: bool = False,
    ) -> None:
        self.bind = bind
        self.port = port
        self.quiet = quiet
        self.table = ResultTable(ttl_seconds, max_results)
        self.log = TsvLogger(log_path)
        self.stop_event = threading.Event()
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind((bind, port))
        self.socket.settimeout(0.5)

    def handle_datagram(self, data: bytes, address: Tuple[str, int]) -> Optional[bytes]:
        try:
            packet = ProbePacket.decode(data)
        except ValueError as exc:
            print(f"DROP from={address[0]}:{address[1]} reason={exc}", file=sys.stderr)
            return None

        now = time.monotonic()
        if packet.message_type == MessageType.PROBE_SEND:
            duplicate, stats, restarted = self.table.record(packet, now)
            self.log.write(
                "PROBE_SEND",
                packet,
                address,
                stats,
                status=ResultStatus.RECEIVED,
                duplicate=duplicate,
                restarted=restarted,
            )
            if not self.quiet:
                total = stats.received + stats.missing
                loss = (100.0 * stats.missing / total) if total else 0.0
                print(
                    f"RUN={stats.run} seq={packet.sequence} recv={stats.received} "
                    f"lost={stats.missing} loss={loss:.2f}% dup={stats.duplicates} "
                    f"profile=P{packet.profile_id}",
                    flush=True,
                )
            return packet.reply(MessageType.PROBE_ACK, ResultStatus.RECEIVED).encode()

        if packet.message_type == MessageType.QUERY_RESULT:
            found, stats = self.table.query(packet, now)
            status = ResultStatus.RECEIVED if found else ResultStatus.NOT_FOUND
            self.log.write("QUERY_RESULT", packet, address, stats, status=status)
            if not self.quiet:
                print(
                    f"QUERY session={packet.session_id} test={packet.test_id} "
                    f"result={status.name}",
                    flush=True,
                )
            return packet.reply(MessageType.QUERY_RESULT_REPLY, status).encode()

        print(
            f"DROP from={address[0]}:{address[1]} unexpected={packet.message_type.name}",
            file=sys.stderr,
        )
        return None

    def serve_forever(self) -> None:
        try:
            while not self.stop_event.is_set():
                try:
                    data, address = self.socket.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError:
                    if self.stop_event.is_set():
                        break
                    raise
                response = self.handle_datagram(data, address)
                if response is not None:
                    self.socket.sendto(response, address)
        finally:
            self.close()

    def close(self) -> None:
        self.stop_event.set()
        try:
            self.socket.close()
        except OSError:
            pass
        self.log.close()


def local_ipv4_addresses() -> Iterable[str]:
    addresses = set()
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except socket.gaierror:
        infos = []
    for info in infos:
        address = info[4][0]
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            continue
        if not parsed.is_loopback and not parsed.is_link_local:
            addresses.add(address)
    return sorted(addresses)


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    default_log = Path(__file__).with_name("local_udp_probe_raw.tsv")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="0.0.0.0", help="IPv4 interface (default: all)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--log", type=Path, default=default_log)
    parser.add_argument("--ttl-seconds", type=float, default=24 * 60 * 60)
    parser.add_argument("--max-results", type=int, default=100_000)
    parser.add_argument("--quiet", action="store_true", help="only print startup and errors")
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    if not (0 <= args.port <= 65535):
        raise SystemExit("--port must be between 0 and 65535")
    server = ProbeServer(
        bind=args.bind,
        port=args.port,
        log_path=args.log,
        ttl_seconds=args.ttl_seconds,
        max_results=args.max_results,
        quiet=args.quiet,
    )
    actual_port = server.socket.getsockname()[1]
    endpoints = list(local_ipv4_addresses())
    print(f"Adaptive Wi-Fi Probe UDP server listening on {args.bind}:{actual_port}")
    if endpoints:
        print("ESP32 destination(s): " + ", ".join(f"{ip}:{actual_port}" for ip in endpoints))
    else:
        print("ESP32 destination: use this computer's LAN IPv4 address")
    print(f"Packet size: {PACKET.size} bytes; log: {args.log.resolve()}", flush=True)

    def request_stop(_signum: int, _frame: object) -> None:
        server.stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

import tempfile
import time
import unittest
from pathlib import Path

from local_udp_probe_server import (
    MessageType,
    ProbePacket,
    ProbeServer,
    ResultStatus,
    ResultTable,
)


def packet(message_type=MessageType.PROBE_SEND, *, test_id=20, sequence=10):
    return ProbePacket(
        message_type=message_type,
        session_id=10,
        test_id=test_id,
        sequence=sequence,
        profile_id=3,
        test_kind=2,
        candidate_id=25,
    )


class PacketTests(unittest.TestCase):
    def test_round_trip(self):
        original = packet()
        self.assertEqual(ProbePacket.decode(original.encode()), original)

    def test_rejects_malformed_packet(self):
        with self.assertRaises(ValueError):
            ProbePacket.decode(b"not-a-packet")


class ResultTableTests(unittest.TestCase):
    def test_duplicate_is_counted_once_logically(self):
        table = ResultTable(ttl_seconds=60, max_results=10)
        duplicate, stats, _ = table.record(packet(), 1.0)
        self.assertFalse(duplicate)
        duplicate, stats, _ = table.record(packet(), 2.0)
        self.assertTrue(duplicate)
        self.assertEqual(stats.received, 1)
        self.assertEqual(stats.duplicates, 1)

    def test_ttl_and_count_are_bounded(self):
        table = ResultTable(ttl_seconds=2, max_results=2)
        table.record(packet(test_id=1), 1.0)
        table.record(packet(test_id=2, sequence=11), 2.0)
        table.record(packet(test_id=3, sequence=12), 3.0)
        self.assertEqual(len(table.results), 2)
        found, _ = table.query(packet(MessageType.QUERY_RESULT, test_id=2), 5.0)
        self.assertFalse(found)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.server = ProbeServer(
            bind="127.0.0.1",
            port=0,
            log_path=Path(self.tempdir.name) / "raw.tsv",
            ttl_seconds=60,
            max_results=10,
            quiet=True,
        )

    def tearDown(self):
        self.server.close()
        self.tempdir.cleanup()

    def test_ack_and_deferred_query(self):
        address = ("127.0.0.1", 40000)
        ack = ProbePacket.decode(self.server.handle_datagram(packet().encode(), address))
        self.assertEqual(ack.message_type, MessageType.PROBE_ACK)
        self.assertEqual(ack.status, ResultStatus.RECEIVED)

        query = packet(MessageType.QUERY_RESULT)
        reply = ProbePacket.decode(self.server.handle_datagram(query.encode(), address))
        self.assertEqual(reply.message_type, MessageType.QUERY_RESULT_REPLY)
        self.assertEqual(reply.status, ResultStatus.RECEIVED)

        missing = packet(MessageType.QUERY_RESULT, test_id=999)
        reply = ProbePacket.decode(self.server.handle_datagram(missing.encode(), address))
        self.assertEqual(reply.status, ResultStatus.NOT_FOUND)


if __name__ == "__main__":
    unittest.main()

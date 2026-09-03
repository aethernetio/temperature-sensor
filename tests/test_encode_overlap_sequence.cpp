/*
 * Copyright 2026 Aethernet Inc.
 *
 * Host unit tests for encode-overlap HOT sequencing rules.
 */

#include <cassert>
#include <cstdio>

#include "prepared_send/encode_overlap_sequence.h"

namespace {

using temp_sensor::encode_overlap::Sequencer;
using temp_sensor::encode_overlap::Stage;

int g_failures = 0;

void Expect(bool cond, char const* msg) {
  if (!cond) {
    std::printf("FAIL: %s\n", msg);
    ++g_failures;
  }
}

void test_EncodeDuringAssociationAllowed() {
  Sequencer s;
  Expect(s.StartWifi(), "start wifi");
  Expect(s.EncodePacket(), "encode while associating");
  Expect(s.stage == Stage::kEncoded, "stage encoded");
  Expect(s.nonce_consumed, "nonce consumed by encode");
}

void test_SocketOnlyAfterNetworkReady() {
  Sequencer s;
  Expect(s.StartWifi(), "start wifi");
  Expect(s.EncodePacket(), "encode");
  Expect(!s.CreateSocket(), "socket before ready must fail");
  Expect(s.MarkNetworkReady(), "network ready");
  Expect(s.CanCreateSocket(), "can create socket");
  Expect(s.CreateSocket(), "create socket");
  Expect(s.socket_created, "socket created");
}

void test_StaticArpBeforeSendto() {
  Sequencer s;
  Expect(s.StartWifi(), "start wifi");
  Expect(s.EncodePacket(), "encode");
  Expect(s.MarkNetworkReady(), "ready");
  Expect(s.InstallStaticArp(), "arp");
  Expect(s.arp_installed, "arp installed");
  Expect(s.CreateSocket(), "socket");
  Expect(s.Sendto(), "sendto");
  Expect(s.stage == Stage::kSent, "sent");
}

void test_SendtoRequiresSocket() {
  Sequencer s;
  Expect(s.StartWifi(), "start wifi");
  Expect(s.MarkNetworkReady(), "ready");
  Expect(!s.Sendto(), "sendto without socket");
  Expect(s.CreateSocket(), "socket");
  Expect(s.CanSendto(), "can sendto");
  Expect(s.Sendto(), "sendto");
}

void test_PreDeadlineOrdering() {
  // PRE is applied after network-ready and before send; sequencer only
  // enforces that send cannot precede socket/network readiness.
  Sequencer s;
  Expect(s.StartWifi(), "start");
  Expect(s.EncodePacket(), "encode early");
  Expect(s.MarkNetworkReady(), "ready");
  // PRE wait would happen here in firmware (WaitUntilPreDeadline).
  Expect(s.InstallStaticArp(), "arp");
  Expect(s.CreateSocket(), "socket");
  Expect(s.Sendto(), "send after PRE");
}

void test_FailedAssociationConsumesNonceNoResend() {
  Sequencer s;
  Expect(s.StartWifi(), "start");
  Expect(s.EncodePacket(), "encode");
  Expect(s.FailAssociationAfterEncode(), "assoc fail after encode");
  Expect(s.nonce_consumed, "nonce spent");
  Expect(!s.CreateSocket(), "no socket after fail");
  Expect(!s.Sendto(), "no resend same nonce");
}

void test_EncodedPacketLifetime() {
  Sequencer s;
  Expect(s.StartWifi(), "start");
  Expect(s.EncodePacket(), "encode");
  Expect(s.EncodedPacketSurvivesUntilSend(), "survives after encode");
  Expect(s.MarkNetworkReady(), "ready");
  Expect(s.EncodedPacketSurvivesUntilSend(), "survives after ready");
  Expect(s.CreateSocket(), "socket");
  Expect(s.EncodedPacketSurvivesUntilSend(), "survives until sendto");
  Expect(s.Sendto(), "send");
  Expect(s.EncodedPacketSurvivesUntilSend(), "still marked after send");
}

}  // namespace

int main() {
  test_EncodeDuringAssociationAllowed();
  test_SocketOnlyAfterNetworkReady();
  test_StaticArpBeforeSendto();
  test_SendtoRequiresSocket();
  test_PreDeadlineOrdering();
  test_FailedAssociationConsumesNonceNoResend();
  test_EncodedPacketLifetime();
  if (g_failures != 0) {
    std::printf("%d failure(s)\n", g_failures);
    return 1;
  }
  std::printf("encode_overlap_sequence: all tests passed\n");
  return 0;
}

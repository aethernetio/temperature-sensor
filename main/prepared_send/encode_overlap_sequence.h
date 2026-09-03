/*
 * Copyright 2026 Aethernet Inc.
 *
 * Host-testable sequencing rules for encode-during-association HOT path.
 * Production overlap may encode while association runs, but must not create
 * the UDP socket or send until network-ready (+ ARP when required).
 */

#ifndef TEMP_SENSOR_ENCODE_OVERLAP_SEQUENCE_H_
#define TEMP_SENSOR_ENCODE_OVERLAP_SEQUENCE_H_

#include <cstdint>

namespace temp_sensor::encode_overlap {

enum class Stage : std::uint8_t {
  kIdle = 0,
  kWifiStarted = 1,
  kEncoded = 2,
  kNetworkReady = 3,
  kArpReady = 4,
  kSocketReady = 5,
  kSent = 6,
  kFailed = 7,
};

struct Sequencer {
  Stage stage{Stage::kIdle};
  bool nonce_consumed{false};
  bool socket_created{false};
  bool arp_installed{false};

  bool StartWifi() {
    if (stage != Stage::kIdle) {
      return false;
    }
    stage = Stage::kWifiStarted;
    return true;
  }

  bool EncodePacket() {
    if (stage != Stage::kWifiStarted && stage != Stage::kNetworkReady &&
        stage != Stage::kArpReady) {
      return false;
    }
    // Encoding may run while association is still in progress.
    if (stage == Stage::kWifiStarted) {
      stage = Stage::kEncoded;
    }
    nonce_consumed = true;
    return true;
  }

  bool MarkNetworkReady() {
    if (stage != Stage::kWifiStarted && stage != Stage::kEncoded) {
      return false;
    }
    stage = Stage::kNetworkReady;
    return true;
  }

  bool InstallStaticArp() {
    if (stage != Stage::kNetworkReady && stage != Stage::kEncoded) {
      return false;
    }
    if (stage == Stage::kEncoded) {
      return false;  // must be network-ready first
    }
    arp_installed = true;
    stage = Stage::kArpReady;
    return true;
  }

  bool CreateSocket() {
    // Socket must not be created before network-ready.
    if (stage != Stage::kNetworkReady && stage != Stage::kArpReady) {
      return false;
    }
    socket_created = true;
    stage = Stage::kSocketReady;
    return true;
  }

  bool Sendto() {
    if (stage != Stage::kSocketReady) {
      return false;
    }
    if (!socket_created) {
      return false;
    }
    stage = Stage::kSent;
    return true;
  }

  bool FailAssociationAfterEncode() {
    if (!nonce_consumed) {
      return false;
    }
    stage = Stage::kFailed;
    // Nonce is spent; caller must not retry the same packet.
    return true;
  }

  bool CanCreateSocket() const {
    return stage == Stage::kNetworkReady || stage == Stage::kArpReady;
  }

  bool CanSendto() const { return stage == Stage::kSocketReady; }

  bool EncodedPacketSurvivesUntilSend() const {
    return nonce_consumed &&
           (stage == Stage::kEncoded || stage == Stage::kNetworkReady ||
            stage == Stage::kArpReady || stage == Stage::kSocketReady ||
            stage == Stage::kSent);
  }
};

}  // namespace temp_sensor::encode_overlap

#endif  // TEMP_SENSOR_ENCODE_OVERLAP_SEQUENCE_H_

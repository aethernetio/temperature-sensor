# Æther HOT 100x / 2 S / PPK / TCP receiver

Integration branch: `diag/aether-hot-100x-2s-ppk-tcp-rx` (parent baseline `8f7b8d5293c132b6512318170286eaaff9f96a84`).

## Firmware

- CMake: `-D AETHER_DIAG_AETHER_HOT_100X_2S_PPK=1`
- Prepared reserve: **128** nonces (`AETHER_PREPARED_HOT_RESERVE`)
- HOT: **100** attempts, **2 s** start-to-start (`AETHER_POWER_BENCH_HOT_SLEEP_MS=2000`)
- Wi-Fi fast path aligned with cached UDP baseline (P4 + pre 50 ms / post 200 ms / 1M / MAC 3,3 / BSSID + static IP + gateway ARP)
- Payload: `ReliabilityPayload` (`0xDB`) with `run_id` + `seq` (FULL_READY seq=0, HOT seq=1..100)
- Thermometer 2 GPIO sleep shutdown before HOT deep sleep (same as UDP fullcapture)

## Desktop receiver

- Target: `aether_hot_tcp_receiver` (`temperature_receiver/CMakeLists.txt`)
- Flags: `--state-dir`, `--require-tcp`, `--expected`, `--tsv`
- Emits: `RECEIVER_UID=`, `RX_TRANSPORT=TCP`, `RX_TCP_VERIFIED=yes` (after 5 s stable TCP)

## Runner

```text
python experiments/run_aether_hot_100x_2s_ppk_tcp_rx.py
python experiments/run_aether_hot_100x_2s_ppk_tcp_rx.py --smoke-only
```

Build dir: `build-aether-hot-100x-2s-ppk`. PPK segmentation reuses `ppk_stream_cycle_segmenter.py` (UDP-independent).

## Raw UDP comparison (baseline, not including FULL prep)

| metric | RAW UDP cached |
|--------|----------------|
| median | 760 ms, 53.65 mC, 160.94 mJ |
| p95 | 2772 ms, 249.45 mC, 748.36 mJ |
| delivery | 299/300 |

Æther HOT measured values: *pending hardware run* (see `experiments/power_factor_results/aether_hot_100x_2s_ppk_tcp_rx.json` after run).

## aether-client-cpp

Pinned in receiver CMake: `157aadbec8e7b852d0f89274307ff7cb8103e5f7` — **changed=no** in temperature-sensor repo.

# Prepared Wi-Fi single-factor bisect (ESP32-C6, silent 13×20)

Hardware: ESP32-C6, ESP-IDF v6.0.2, COM7, flash size **4MB**  
Aether: `exp/esp32c6-wifi-lifecycle-diag` @ `157aadbec8e7b852d0f89274307ff7cb8103e5f7` (**unchanged**)  
Firmware: `AE_EXP_PREPARED_WIFI_BISECT=1`, silent console (`CONFIG_ESP_CONSOLE_NONE`, log level 0)  
Receiver: `prepared_wifi_cache_rx_v1` / UID `5aade50f-00d9-4624-b097-e203cdcf1e38`  
Policy: Wi-Fi 4 b/g/n, `WIFI_PS_NONE`, DHCP+GOT_IP baseline; 200 ms pre-delay (except B0); 300 ms post-hold; 1 s prepared gap.

## Hang diagnosis (pre-full-run)

Silent flash previously showed **0 FULL / 0 PREPARED** for ~22 minutes.

Root causes fixed (harness / flash only; bisect factors unchanged):

1. **Flash size mismatch**: flashing with `--flash-size 2MB` while the app partition is `0x300000` caused a boot loop (`partition … exceeds flash chip size`). Chip is 4MB → use `--flash-size 4MB`.
2. **Deferred SelectClient/Write work after `WaitUntil`**: callbacks armed deferred stages that only ran on the next loop wake, stalling after `SELECT_BEGIN`. Fixed by running deferred work immediately after `Update()`, before `WaitUntil`.
3. **No nested `Write()` from Write/Update callbacks**: META/cache freeze and Prepare/Save/Release stay deferred into the main loop/state machine.
4. **200 ms settle** after `ReleaseFullAetherWifiForHotPath()` before the first bisect STA init.

Smoke (B1 × 2 prepared, temporary USB stage markers) reached `BISECT_VARIANT_DONE B1` with **2 PREPARED** on the receiver, then console/logging were turned back off for the silent full run.

## Results (one silent 13×20)

All variants: `WifiReady=Encode=Sendto=Nonce=20` (device completed every reconnect/encode/sendto; losses are delivery).

| Variant | Single change | Delivered/20 | Missing | Median_ms | Verdict |
|---|---|---:|---:|---:|---|
| B0 | no cache, 0 ms pre-delay | 15/20 | 5 | 2170 | DEGRADES |
| B1 | no cache, 200 ms pre-delay | 18/20 | 2 | 2390 | OK |
| C1 | BSSID only | 20/20 | 0 | 2390 | OK |
| C2 | CHANNEL only | 20/20 | 0 | 2390 | OK |
| C3 | BSSID+CHANNEL | 19/20 | 1 | 2390 | OK |
| C4 | FAST_SCAN only | 18/20 | 2 | 2390 | OK |
| C5 | STATIC_IP only | 17/20 | 3 | 850 | DEGRADES |
| C6 | STATIC_IP+ARP (dep C5) | 20/20 | 0 | 850 | OK |
| C7 | BSSID+STATIC_IP | 19/20 | 1 | 850 | OK |
| C8 | CHANNEL+STATIC_IP | 20/20 | 0 | 850 | OK |
| P1 | PS_MAX_MODEM | 20/20 | 0 | 2400 | OK |
| P2 | AMPDU_OFF | 18/20 | 2 | 2390 | OK |
| P3 | FIXED_1M | 20/20 | 0 | 2430 | OK |

Totals: prepared delivered **244/260**; final **1/1**; out_of_order **0**.  
(Receiver also counted 14 FULL vs 13 variants — one extra FULL from registration/start sequencing; not used for factor verdicts.)

## CHANNEL HYPOTHESIS

```
B1 no cache = 18/20
C1 BSSID only = 20/20
C2 channel only = 20/20
C3 BSSID+channel = 19/20
C7 BSSID+static IP = 19/20
C8 channel+static IP = 20/20
Does cached channel independently correlate with loss? NO
```

Channel match counts (requested vs actual when channel factor set): C2 19/0, C3 19/0, C8 19/0 (mismatches 0).

## Notes

- Static-IP variants (C5–C8) show ~850 ms median vs ~2.4 s for DHCP reconnects — expected speedup; C5 alone still loses more packets than C6 (ARP) / C8.
- B0 (0 ms pre-delay) is worse than B1 (200 ms) → keep 200 ms pre-delay in main tests.
- No production Wi-Fi combo invented from this table; report only.

## Artifacts

- Receiver log: `experiments/prepared_wifi_bisect_full_rx.log`
- TSV: `experiments/PREPARED_WIFI_SINGLE_FACTOR_BISECT.tsv`

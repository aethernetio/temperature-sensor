# TXD3/TXD4 FULL-loop diagnostic report

## Pins

| Repo | Branch | SHA | Notes |
|------|--------|-----|-------|
| temperature-sensor | `thermometer-prepared-send-v0` | `724a1731a53c63963689b4c83da302937eb56a84` | TX-done diag + FULL reason |
| aether-client-cpp | `exp/esp32c6-wifi-lifecycle-diag` | `157aadbec8e7b852d0f89274307ff7cb8103e5f7` | **unchanged=yes** |

## Pre-change inventory

```
CURRENT_LOCAL_SHA=5b878e10690ada5a08a027e851cef89951677f2a
CURRENT_REMOTE_SHA=5b878e10690ada5a08a027e851cef89951677f2a
BUILD_EXPERIMENT=AE_EXP_PREPARED_TX_DONE_DIAG=1 (AE_EXP_PREPARED_DEEPSLEEP_5X50 empty)
TXD3_SOURCE_PRESENT=yes (local uncommitted; evolved to TXD4 magic 0x54584434)
```

RTC magic progression local: TXDG → TXD2 → TXD3 → **TXD4**.

## ROOT CAUSE

`ExperimentEarlyAppEntry()` / early snapshot were compiled only when
`AE_EXP_PREPARED_DEEPSLEEP_5X50` was defined.

TX-done builds set `AE_EXP_PREPARED_TX_DONE_DIAG=1` and leave
`AE_EXP_PREPARED_DEEPSLEEP_5X50` empty, so:

1. `ExperimentEarlyAppEntry()` was a no-op.
2. `GetExperimentEarlyEntrySnapshot()` returned zeros (`reset_reason=0`, `valid=0`).
3. `PrepareRtcOnBoot` treated every wake as `reset != ESP_RST_DEEPSLEEP`.
4. With otherwise-valid RTC state this called `ForceFullRecovery()` every wake →
   perpetual FULL, HOT never started.

Power-cycle did not help because the bug is on **every** boot path, not stale RTC.

Exact gate (before fix): `main/experiment_early_entry.h` / `.cpp` —
`#if defined(AE_EXP_PREPARED_DEEPSLEEP_5X50)` only.

Decision site (before fix): `PrepareRtcOnBoot()` in
`main/prepared_tx_done_diag_bench.cpp` — branch
`reset != ESP_RST_DEEPSLEEP || !valid` → `ForceFullRecovery()`.

## Secondary bug (observed after primary fix)

After HOT #50 with `kOuterCycles=1`, phase became `kFinal` while
`hot_index` stayed **51**. `ValidateRtcState()` requires
`hot_index <= kHotPerOuter` (50), so the next deep-sleep wake reported:

```
FULL_DIAG reason=STATE_BOUNDS_INVALID reset=8 wake=4
rtc_state={magic=TXD4,ver=1,crc=1,valid=0} phase=3 outer=1 hot=51
prepared={valid=1,left=0} wifi={valid=1}
```

`reset=8` = `ESP_RST_DEEPSLEEP`, `wake=4` = `ESP_SLEEP_WAKEUP_TIMER`
(confirmed on IDF). That path incorrectly re-inited FULL instead of running FINAL.

Fix: clamp `hot_index=1` when entering `kFinal` (same as FULL transition).

## FIX

1. Enable early entry for `AE_EXP_PREPARED_TX_DONE_DIAG` as well as deepsleep 5×50.
2. Add `FullReason` + boot / pre-sleep snapshots into `TxDiagPayload` (0xD6, 118 bytes).
3. Sequential FullReason assignment at decision sites (no post-hoc guess).
4. Receiver prints one `FULL_DIAG` line per FULL.
5. Clamp `hot_index` on Final transition.

Fast Wi-Fi knobs untouched (PRE=25, WPA2, channel cache, static IP/ARP, etc.).

## RTC storage table

| Object | Attribute | Survives deep sleep | Initialized on cold boot |
|--------|-----------|---------------------|---------------------------|
| `g_rtc` (experiment main) | `RTC_DATA_ATTR` | yes (zeroed by C runtime on power-on) | zero / invalid until magic+CRC |
| `g_rtc_wifi_cache` | `RTC_DATA_ATTR` | yes | zero until capture |
| `g_pending_diag` / `g_last_full_reason` / `g_pre_sleep` | `RTC_DATA_ATTR` | yes | zero |
| `PreparedSendMessageBlock` | `RTC_NOINIT_ATTR` | yes (garbage until magic valid) | **not** zeroed; require `is_valid()` |
| `BootSnap g_boot_snap` | ordinary `.bss` | no | set each boot before RTC mutation |
| Legacy wifi `rtc_ip_info` / BSSID | `RTC_DATA_ATTR` | yes | separate from structured cache |

No unconditional `InvalidatePreparedWifiCache()` / `ClearPreparedSendBlock()` in
`setup` / `PrepareRtcOnBoot` for this bench path.

## EVIDENCE

### First readable FULL after primary fix (end of HOT block / Final bounds)

- **full_reason:** `STATE_BOUNDS_INVALID` (secondary bug; not the perpetual-loop cause)
- **reset_reason:** `8` (`ESP_RST_DEEPSLEEP`)
- **wakeup_cause:** `4` (`ESP_SLEEP_WAKEUP_TIMER`)
- **rtc_state_valid:** `0` (hot_index=51 out of bounds; magic/CRC OK)
- **prepared_block_valid:** `1`, **prepared_message_left:** `0`
- **rtc_wifi_valid:** `1`

### Pre-fix perpetual FULL (code-level; early snapshot always zero)

Would have reported effectively **`UNEXPECTED_RESET`** / force-full every wake
because `reset_reason==0` and `early.valid==0`.

## VERIFY (after early-entry fix)

Flashed build with early-entry fix (pre Final `hot_index` clamp). Board COM7
later disconnected before reflashing the Final-bounds clamp; clamp is in tree.

```
FULL=1
HOT=3+
HOT1: delivered (pending flush of FULL user_us≈3.2s; first HOT row)
HOT2: status ok, cb_t=1 cb_s=1 first_st=1 rssi≈-42
HOT3: status ok, cb_t=1 cb_s=1 first_st=1 rssi≈-44
```

Campaign continued through HOT≈50 with callbacks; FULL counter no longer
increments on every wake.

## PASS criteria

- [x] One FULL prepares block/cache and sleeps
- [x] Next boots choose HOT (with early entry enabled)
- [x] HOT #1..#3 reach sendto; callback path active on HOT2+
- [x] FULL no longer grows every wake
- [x] aether-client-cpp unchanged at `157aadbe...`

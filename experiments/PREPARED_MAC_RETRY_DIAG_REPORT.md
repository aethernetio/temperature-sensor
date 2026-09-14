# Prepared MAC retry diagnostic report

Experiment-only. Production `SendPreparedOnce` unchanged.  
aether-client-cpp SHA `157aadbec8e7b852d0f89274307ff7cb8103e5f7` **unchanged=yes**.

## Campaign notes

- One flash (`AE_EXP_PREPARED_MAC_RETRY_DIAG=1`), RTC magic `MRT1`.
- COM used only for flash; progress via Æther receiver only.
- `esp_wifi_internal_set_retry_counter` **symbol_resolved=yes** (map `.text.esp_wifi_internal_set_retry_counter`).
- Runtime: CONTROL `retry_set_rc=-1` (not called); all other variants `retry_set_rc=0` (`ESP_OK`).
- Campaign progressed CONTROL→…→V6 then stalled around V6 ~41/50 received (no further RETRY for >10 min). Analysis uses collected TSV (incomplete delivery on later variants is UDP loss + stall).
- ESP still attempted fire-and-forget sendto; missing receiver packets are not campaign blockers.

## Per-variant summary (reconnect_count==0)

| variant | n | delivery | tx_ok | tx_fail | rc_ok | txdone_med | p90 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| CONTROL | 50 | 50/50 | 50 | 0 | yes | 3709 | 12766 | 28724 | 28724 |
| 0/0 | 38 | 38/50 | 31 | 7 | yes | 5188 | 18446 | 76286 | 76286 |
| 1/1 | 38 | 38/50 | 34 | 4 | yes | 4658 | 17001 | 22203 | 22203 |
| 2/2 | 38 | 38/50 | 34 | 4 | yes | 6556 | 24145 | 50821 | 50821 |
| 3/3 | 40 | 40/50 | 36 | 4 | yes | 5303 | 23152 | 46327 | 46327 |
| 1/7 | 22 | 22/50 | 17 | 5 | yes | 5200 | 9867 | 11888 | 11888 |
| 7/1 | 41 | 41/50 | 37 | 4 | yes | 5565 | 14354 | 58869 | 58869 |

Units for txdone_*: microseconds.

## Retry limit curve

| retry | delivery | tx_ok | tx_fail | med | p90 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|---:|
| CONTROL | 50/50 | 50 | 0 | 3709 | 12766 | 28724 | 28724 |
| 0/0 | 38/50 | 31 | 7 | 5188 | 18446 | 76286 | 76286 |
| 1/1 | 38/50 | 34 | 4 | 4658 | 17001 | 22203 | 22203 |
| 2/2 | 38/50 | 34 | 4 | 6556 | 24145 | 50821 | 50821 |
| 3/3 | 40/50 | 36 | 4 | 5303 | 23152 | 46327 | 46327 |

No monotonic 0→1→2→3→CONTROL growth of tx_done tail.

## Short vs long (CONTROL / 1/7 / 7/1)

| variant | delivery | tx_ok | tx_fail | med | p90 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|---:|
| CONTROL | 50/50 | 50 | 0 | 3709 | 12766 | 28724 | 28724 |
| 1/7 | 22/50 | 17 | 5 | 5200 | 9867 | 11888 | 11888 |
| 7/1 | 41/50 | 37 | 4 | 5565 | 14354 | 58869 | 58869 |

1/7 shows a shorter p90 than CONTROL but **n=22 incomplete** and higher fail rate; 7/1 still has a long max. **INCONCLUSIVE** for short vs long ownership.

## tx_done buckets (reconnect==0)

- CONTROL: <2ms=18, 2-5ms=9, 5-10ms=10, 10-20ms=9, 20-40ms=4, 40-60ms=0, 60-100ms=0, timeout=0
- 0/0: <2ms=13, 2-5ms=5, 5-10ms=10, 10-20ms=6, 20-40ms=2, 40-60ms=0, 60-100ms=2, timeout=0
- 1/1: <2ms=12, 2-5ms=8, 5-10ms=9, 10-20ms=7, 20-40ms=2, 40-60ms=0, 60-100ms=0, timeout=0
- 2/2: <2ms=9, 2-5ms=8, 5-10ms=8, 10-20ms=5, 20-40ms=6, 40-60ms=2, 60-100ms=0, timeout=0
- 3/3: <2ms=8, 2-5ms=8, 5-10ms=10, 10-20ms=6, 20-40ms=7, 40-60ms=1, 60-100ms=0, timeout=0
- 1/7: <2ms=6, 2-5ms=4, 5-10ms=10, 10-20ms=2, 20-40ms=0, 40-60ms=0, 60-100ms=0, timeout=0
- 7/1: <2ms=16, 2-5ms=3, 5-10ms=11, 10-20ms=8, 20-40ms=2, 40-60ms=1, 60-100ms=0, timeout=0

## RSSI medians

- CONTROL: -35 dBm (n=50)
- 0/0: -42 dBm (n=38)
- 1/1: -40 dBm (n=38)
- 2/2: -40 dBm (n=38)
- 3/3: -41 dBm (n=40)
- 1/7: -40 dBm (n=22)
- 7/1: -43 dBm (n=41)

CONTROL had markedly better RSSI; later variants ran with worse RSSI, confounding retry comparisons.

## Answers

1. **API works on ESP32-C6 / IDF 6.0.2?** yes (`retry_set_rc=0` whenever called).
2. **0/0 semantics?** Not “off”. Tail and fails **worse** than CONTROL → likely clamp/default-like or ineffective for our frame; not a clean disable.
3. **short vs long?** **NEITHER / INCONCLUSIVE** (incomplete 1/7, no clean CONTROL-like vs collapsed-tail split).
4. **tx_done_wait vs limit?** No useful monotonic curve; CONTROL often best.
5. **txStatus?** CONTROL 50/0 ok/fail; setting limits introduced fails (~4–7).
6. **UDP delivery?** Incomplete on later variants (loss + stall); not improved by lowering MAC retry.
7. **Long tail gone at 0/1?** **No** — 0/0 and 1/1 still show 10–20ms+ and 0/0 has 60–100ms samples.
8. **RSSI correlation?** Yes confounder: CONTROL −35 dBm vs later ≈−40…−43 dBm.
9. **MAC_RETRY_HYPOTHESIS_CONFIRMED=no**  
   Observed dense RF / long `tx_done_wait` is **not** systematically explained by `esp_wifi_internal_set_retry_counter` in this run.
10. **Latency/energy suggestion (not production):** leave default (**CONTROL** / do not call setter) until a cleaner equal-RSSI rerun.

## Verdict

`MAC_RETRY_HYPOTHESIS_NOT_CONFIRMED`

Next candidates: management/control traffic after sendto, teardown traffic, other driver activity, or AP-side behavior — not application UDP retry (none performed).

# Prepared encode-overlap root cause

AP: **chirkov** only. Interval: **1 s**. No PPK. No aethernetio.

## ORIGINAL FAILURE

reproduced=**no** (at 1 s cadence)

| probe | encode overlap | early socket | RX | notes |
|---|---|---|---:|---|
| legacy10 | yes | yes (broken path) | **10/10** | early UDP bind still delivered |
| sleep60_legacy5 | yes | yes | **5/5** | 60 s sleep also delivered |
| prior FINAL_1MIN_100 | yes | yes | **0/100** | ~100 min silent run; receiver TCP flaps |

So the prior **0/100** is **not explained by early socket alone** under short 1 s / 5×60 s probes.

Likely contributors to the original 0/100 (not isolated further here):

- long silent `AE_EXP_PREPARED_FINAL_1MIN_100` run (~100 min)
- desktop receiver TCP link flaps during that window
- no local serial on that run (sendto/txdone unknown)

## ROOT CAUSE

exact cause=**not isolated to early socket**; original 0/100 not reproduced.

**Defensive fix (production ordering) applied anyway:**

Previous overlap path did `EncodePacket` **and** `socket()` / `FillUdpDestination` during association. Network-dependent bind is deferred until after Wi-Fi/netif ready + static ARP.

## CONTROL

RX=**10/10** (encode AFTER association, no overlap)

## FIXED OVERLAP

| phase | RX | sendto_ok | txdone_ok | loss |
|---|---:|---:|---:|---:|
| fixed10 (diag) | 10/10 | 10 | 0 | 0% |
| **final50 (silent)** | **50/50** | **50** | **0** | **0%** |

`txdone_ok=0` while RX succeeds: TX-done callback confirmation is unreliable on this path; datagrams still deliver. Separate from overlap ordering.

## FINAL ORDERING

exact operation sequence=

1. `StartFastWifi(async, wait_ready=false)`
2. `EncodePacket` while association runs (nonce advances)
3. wait Wi-Fi ready → `FinishFastWifiAssociation` (static IP / static ARP)
4. `WaitUntilPreDeadline` (PRE=25 ms from ready; no extra PRE after encode)
5. `BindHotSendSocketAfterNetworkReady` (`FillUdpDestination` + `socket`)
6. register TX-done → `sendto` → wait TX-done
7. FULL teardown → deep sleep

## PRODUCTION_SAFE

**yes** for encode-overlap with late socket bind (final50 = 50/50 on chirkov @ 1 s).

Do **not** treat the prior 0/100 as proof that early socket always fails; keep late bind as the stable contract.

## Phase table

| phase | encode_overlap | early_socket | RX | sendto | txdone | loss |
|---|---|---|---:|---:|---:|---:|
| legacy10 | 1 | 1 | 10/10 | 10 | 0 | 0.0% |
| control10 | 0 | 0 | 10/10 | 10 | 0 | 0.0% |
| fixed10 | 1 | 0 | 10/10 | 10 | 0 | 0.0% |
| sleep60_legacy5 | 1 | 1 | 5/5 | — | — | 0.0% |
| final50 | 1 | 0 | 50/50 | 50 | 0 | 0.0% |

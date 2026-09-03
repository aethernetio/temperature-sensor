# Prepared encode-overlap root cause

Run id: `20260903_115821`
AP: chirkov only. Interval: 1 s (except sleep60_legacy5 at 60 s). No PPK.

## ORIGINAL FAILURE
reproduced=no
RX=10/10 (legacy10 early socket at 1 s did **not** reproduce the historical 0/100)
sendto_ok=10 txdone_ok=0

Hypothesis under test: UDP socket (and destination bind) created during association,
before netif/IP/ARP readiness, leaving sendto on a stale/invalid early socket.

Result: early-socket-alone was **not** proven. All short legacy/control/fixed arms
delivered 100% RX. sleep60_legacy5 also delivered 5/5 with early socket at 60 s.
Original 0/100 is more likely long-run / receiver-TCP related (unproven here).

## ROOT CAUSE
exact cause=not proven as early-socket alone; original 0/100 unreproduced on chirkov short arms (likely long-run/receiver-TCP). Kept defensive ordering: EncodePacket may run during association; UDP socket only after network ready.

## CONTROL
RX=10/10 (encode AFTER association)

## FIXED OVERLAP
RX=50/50
sendto_ok=50
txdone_ok=0
loss=0.0%

## FINAL ORDERING
exact operation sequence=
1. StartFastWifi(async, no wait)
2. EncodePacket while association in progress (nonce advanced)
3. wait Wi-Fi ready + FinishFastWifiAssociation (static IP/ARP)
4. WaitUntilPreDeadline(PRE=25 ms from ready)
5. BindHotSendSocketAfterNetworkReady (socket + sockaddr)
6. register TX-done, sendto, wait TX-done
7. FULL teardown, deep sleep

PRODUCTION_SAFE=yes

## Phase table
| phase | encode_overlap | early_socket | RX | sendto | txdone | loss |
|---|---|---|---:|---:|---:|---:|
| legacy10 | 1 | 1 | 10/10 | 10 | 0 | 0.0% |
| control10 | 0 | 0 | 10/10 | 10 | 0 | 0.0% |
| fixed10 | 1 | 0 | 10/10 | 10 | 0 | 0.0% |
| sleep60_legacy5 | 1 | 1 | 5/5 | 0 | 0 | 0.0% |
| final50 | 1 | 0 | 50/50 | 50 | 0 | 0.0% |

Notes:
- sleep60_legacy5 was silent (no serial OVLP_HOT lines); sendto/txdone counters from serial are 0, but RX=5/5.
- final50 was silent; BENCH_SUMMARY reported sendto_ok=50 txdone_ok=0; RX=50/50.
- Fix retained for production safety despite unreproduced early-socket failure mode.

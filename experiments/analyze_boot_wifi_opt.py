import csv
from pathlib import Path
from collections import defaultdict

NAMES = {
    0: "D0_CONTROL",
    1: "D1_STORAGE_RAM",
    2: "D2_NVS_OFF",
    3: "D3_RAM_NVS_OFF",
    4: "G1_HT20",
    5: "H1_CS_OFF",
    6: "H2_CS_ON",
    7: "E1_TX_HALF",
    8: "E2_TX_MIN",
    9: "E3_RX_HALF",
    10: "E4_RX_MIN",
}
SETTINGS = {
    0: "baseline",
    1: "WIFI_STORAGE_RAM",
    2: "nvs_enable=0",
    3: "RAM+nvs_off",
    4: "force HT20",
    5: "dynamic_cs=false",
    6: "dynamic_cs=true",
    7: "dyn_tx=16",
    8: "dyn_tx=8",
    9: "rx 5/16",
    10: "rx 3/8",
}

p = Path(r"C:\Users\nickc\Projects\temperature-sensor-prepared\experiments\prepared_boot_wifi_opt.tsv")
rows = list(csv.DictReader(p.open(encoding="utf-8"), delimiter="\t"))


def iu(x):
    try:
        return int(x)
    except Exception:
        return 0


hots = [r for r in rows if iu(r["kind"]) == 2]
by = defaultdict(list)
for r in hots:
    by[iu(r["variant"])].append(r)


def med(xs):
    xs = sorted(xs)
    if not xs:
        return 0
    return xs[len(xs) // 2]


def p90(xs):
    xs = sorted(xs)
    if not xs:
        return 0
    return xs[int((len(xs) - 1) * 0.9)]


lines = []
hdr = (
    "variant\tsetting\tdelivery/30\twake_overhead_med\twifi_init_med\t"
    "connect_med\ttxdone_med\thot_user_med\tp90\tmax\theap_delta\tnotes"
)
lines.append(hdr)
print(hdr)
results = []
for vid in range(11):
    rs = by.get(vid, [])
    n = len(rs)
    ok = sum(1 for r in rs if iu(r["first_status"]) == 1)
    wake = [iu(r["sleep_overhead_us"]) for r in rs if iu(r["sleep_overhead_us"]) > 0]
    init = [iu(r["wifi_init_us"]) for r in rs if iu(r["wifi_init_us"]) > 0]
    conn = [iu(r["connect_us"]) for r in rs if iu(r["connect_us"]) > 0]
    tx = [iu(r["txdone_us"]) for r in rs]
    user = [
        iu(r["user_us"])
        for r in rs
        if iu(r["user_us"]) > 0 and iu(r["user_us"]) < 2000000
    ]
    heap = [
        iu(r["heap_before"]) - iu(r["heap_after"])
        for r in rs
        if iu(r["heap_before"]) > 0
    ]
    bo = sum(1 for r in rs if iu(r["brownout"]))
    notes = []
    if bo:
        notes.append(f"brownout={bo}")
    if n < 30:
        notes.append(f"n={n}")
    notes.append(f"txok={ok}")
    line = (
        f"{NAMES[vid]}\t{SETTINGS[vid]}\t{n}/30\t{med(wake)}\t{med(init)}\t"
        f"{med(conn)}\t{med(tx)}\t{med(user)}\t{p90(user)}\t"
        f"{(max(user) if user else 0)}\t{med(heap)}\t{';'.join(notes) or '-'}"
    )
    lines.append(line)
    print(line)
    results.append(
        dict(
            vid=vid,
            name=NAMES[vid],
            setting=SETTINGS[vid],
            n=n,
            wake=med(wake),
            init=med(init),
            conn=med(conn),
            tx=med(tx),
            user=med(user),
            p90=p90(user),
            mx=max(user) if user else 0,
            heap=med(heap),
            ok=ok,
        )
    )

by_user = sorted(results, key=lambda d: (d["user"] if d["user"] else 9e9, d["conn"]))
by_wake = sorted(results, key=lambda d: d["wake"] if d["wake"] else 9e9)
by_ram = sorted(results, key=lambda d: d["heap"] if d["heap"] else 9e9)
by_conn = sorted(results, key=lambda d: d["conn"] if d["conn"] else 9e9)

print("\n=== WINNERS ===")
print("hot_user:", by_user[0]["name"], by_user[0]["user"], "us")
print("wake_ov:", by_wake[0]["name"], by_wake[0]["wake"], "us")
print("connect:", by_conn[0]["name"], by_conn[0]["conn"], "us")
print("RAM(lowest heap_delta):", by_ram[0]["name"], by_ram[0]["heap"])

out = Path(
    r"C:\Users\nickc\Projects\temperature-sensor-prepared\experiments\prepared_boot_wifi_opt_summary.tsv"
)
out.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("wrote", out)

# section deltas vs D0
d0 = results[0]
print("\nvs D0_CONTROL:")
for d in results:
    du = d["user"] - d0["user"]
    dc = d["conn"] - d0["conn"]
    print(
        f"  {d['name']}: user {d['user']} ({du:+d}) conn {d['conn']} ({dc:+d}) "
        f"init {d['init']} wake {d['wake']} heap_delta {d['heap']} n={d['n']}"
    )

# recommend combined
cands = [d for d in results if d["n"] >= 24]
best = min(cands, key=lambda d: d["user"])
d_best = min(results[0:4], key=lambda d: d["user"] if d["user"] else 9e9)
g = results[4]
h_best = min(results[5:7], key=lambda d: d["user"] if d["user"] else 9e9)
e_best = min(results[7:11], key=lambda d: d["user"] if d["user"] else 9e9)
e_ram = min(results[7:11], key=lambda d: d["heap"] if d["heap"] else 9e9)

print("\nsection picks:")
print(" D:", d_best["name"], d_best["user"])
print(" G:", g["name"], g["user"], f"vsD0 {g['user']-d0['user']:+d}")
print(" H:", h_best["name"], h_best["user"])
print(" E time:", e_best["name"], e_best["user"])
print(" E ram:", e_ram["name"], e_ram["heap"])
print(" overall time:", best["name"], best["user"])

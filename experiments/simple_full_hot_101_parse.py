"""Parse firmware serial + HOT RTC dumps for simple_full_hot_101."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

RE_RTC_BEGIN = re.compile(
    r"HOT_RTC_TABLE_BEGIN "
    r"(?:run_id=(\d+) )?"
    r"planned=(\d+) "
    r"(?:started=(\d+) )?"
    r"next=(\d+)"
    r"(?: finish=([^\n]+))?"
    r"(?: table=(\S+))?",
    re.I,
)
RE_ROW_LEGACY = re.compile(
    r"HOT_RTC_ROW n=(\d+) got_ip=(\d+) encode=(\d+) nonce=(\d+) "
    r"sendto=(\d+) err=(\d+) state=(\S+)",
    re.I,
)
RE_ROW_EXT = re.compile(
    r"HOT_RTC_ROW n=(\d+) got_ip=(\d+) encode_called=(\d+) encode_ok=(\d+) "
    r"nonce=(\d+) sendto_called=(\d+) sendto=(\d+) err=(\d+) state=(\S+)",
    re.I,
)


@dataclass
class RtcDumpBlock:
    run_id: int | None
    planned: int
    started: int | None
    next_message: int
    finish: str | None
    table: str | None
    rows: dict[int, dict] = field(default_factory=dict)
    complete: bool = False


def parse_rtc_row_line(line: str) -> tuple[int, dict] | None:
    m = RE_ROW_EXT.search(line)
    if m:
        return int(m.group(1)), {
            "got_ip": int(m.group(2)),
            "encode_called": int(m.group(3)),
            "encode_ok": int(m.group(4)),
            "encode": int(m.group(4)),
            "nonce": int(m.group(5)),
            "sendto_called": int(m.group(6)),
            "sendto": int(m.group(7)),
            "err": int(m.group(8)),
            "state": m.group(9).upper(),
        }
    m = RE_ROW_LEGACY.search(line)
    if m:
        enc = int(m.group(3))
        snd = int(m.group(5))
        return int(m.group(1)), {
            "got_ip": int(m.group(2)),
            "encode": enc,
            "encode_called": enc,
            "encode_ok": enc,
            "nonce": int(m.group(4)),
            "sendto": snd,
            "sendto_called": snd,
            "err": int(m.group(6)),
            "state": m.group(7).upper(),
        }
    return None


def extract_rtc_dumps(text: str, *, hot_count: int = 100) -> list[RtcDumpBlock]:
    blocks: list[RtcDumpBlock] = []
    current: RtcDumpBlock | None = None
    last_n = 1 + hot_count
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("SERIAL_"):
            continue
        bm = RE_RTC_BEGIN.search(line)
        if bm:
            if current is not None and current.rows:
                blocks.append(current)
            current = RtcDumpBlock(
                run_id=int(bm.group(1)) if bm.group(1) else None,
                planned=int(bm.group(2)),
                started=int(bm.group(3)) if bm.group(3) else None,
                next_message=int(bm.group(4)),
                finish=bm.group(5).strip() if bm.group(5) else None,
                table=bm.group(6).upper() if bm.group(6) else None,
            )
            continue
        if line == "HOT_RTC_TABLE_END":
            if current is not None:
                current.complete = True
                blocks.append(current)
                current = None
            continue
        if current is not None:
            pr = parse_rtc_row_line(line)
            if pr:
                n, row = pr
                if 2 <= n <= last_n:
                    current.rows[n] = row
    if current is not None and current.rows:
        blocks.append(current)
    return blocks


def pick_authoritative_rtc_dump(
    blocks: list[RtcDumpBlock], *, hot_count: int = 100
) -> RtcDumpBlock | None:
    last_n = 1 + hot_count
    expected = set(range(2, last_n + 1))
    best: RtcDumpBlock | None = None
    for b in blocks:
        if not b.complete:
            continue
        if b.table == "UNAVAILABLE":
            continue
        if b.planned != hot_count:
            continue
        if set(b.rows.keys()) != expected:
            continue
        best = b
    return best


def merge_rtc_into_fw_parse(
    fw: dict, text: str, *, hot_count: int = 100
) -> dict:
    blocks = extract_rtc_dumps(text, hot_count=hot_count)
    fw = dict(fw)
    fw["hot_rtc_dump_blocks"] = len(blocks)
    chosen = pick_authoritative_rtc_dump(blocks, hot_count=hot_count)
    if chosen is None:
        if not fw.get("hot_rtc_rows"):
            fw["hot_rtc_rows"] = {}
        fw["hot_rtc_table_complete"] = False
        fw["hot_rtc_run_id"] = None
        return fw
    fw["hot_rtc_rows"] = dict(chosen.rows)
    fw["hot_rtc_table_complete"] = True
    fw["hot_rtc_run_id"] = chosen.run_id
    fw["hot_rtc_planned"] = chosen.planned
    fw["hot_rtc_started"] = chosen.started
    fw["hot_rtc_finish"] = chosen.finish
    return fw

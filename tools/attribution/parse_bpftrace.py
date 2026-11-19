"""Parse bpftrace map dumps from the root trace-bpf mode.

bpftrace prints maps at exit as:

    @enter[fio]: 12345
    @sw[fio, iou-wrk-1234]: 7
    @dwell[iou-wrk-1234]: 987654321

Keys are comm names or comma-separated key tuples. Values are counts, or
nanoseconds for @dwell. Event loss is reported by bpftrace on stderr
("Lost N events") and must travel with every derived number.
"""

from __future__ import annotations

import re


def parse_maps(raw: str) -> dict:
    maps: dict = {}
    for line in raw.splitlines():
        m = re.match(r"\s*@(\w+)(?:\[(.*)\])?:\s+(\d+)\s*$", line)
        if not m:
            continue
        name, key, value = m.group(1), m.group(2), int(m.group(3))
        bucket = maps.setdefault(name, {})
        if key is None:
            bucket[("",)] = value
        else:
            bucket[tuple(p.strip() for p in key.split(","))] = value
    return maps


def detect_lost(stderr: str) -> int | None:
    total = 0
    found = False
    for m in re.finditer(r"Lost (\d+) events", stderr):
        total += int(m.group(1))
        found = True
    return total if found else None


def _key_tuple(key) -> tuple:
    return key if isinstance(key, tuple) else (key,)


def _sum(maps: dict, name: str, pred=None) -> int:
    total = 0
    for key, value in (maps.get(name) or {}).items():
        if pred is None or pred(_key_tuple(key)):
            total += value
    return total


def summarize(maps: dict, ios: int | None, comm: str = "fio") -> dict:
    """Normalize the counters to per-op values. Workers are identified by
    the kernel naming convention iou-wrk-<pid>; only their dwell is
    summed (their own @enter/@futex keys do not exist because syscalls
    happen on the submitting thread in these backends)."""
    def worker(k: tuple) -> bool:
        return k[0].startswith("iou-wrk")

    values = {
        "enters": _sum(maps, "enter", lambda k: k[0] == comm),
        "submits": _sum(maps, "submit", lambda k: k[0] == comm),
        "punts": _sum(maps, "punt", lambda k: k[0] == comm),
        "cqe_events": _sum(maps, "cqe"),
        "futex": _sum(maps, "futex", lambda k: k[0] == comm),
        "fio_dwell_ns": _sum(maps, "dwell", lambda k: k[0] == comm),
        "worker_dwell_ns": _sum(maps, "dwell", worker),
    }
    out = {"comm": comm, "ios": ios, "counts": values,
           "per_op": {}, "dwell_ms": {}}
    if ios:
        for name, count in values.items():
            if name.endswith("_ns"):
                continue
            out["per_op"][name] = round(count / ios, 6)
        out["dwell_ms"] = {
            "fio": round(values["fio_dwell_ns"] / 1e6, 1),
            "iou_wrk": round(values["worker_dwell_ns"] / 1e6, 1),
        }
        out["worker_dwell_us_per_op"] = round(
            values["worker_dwell_ns"] / 1e3 / ios, 3)
    return out

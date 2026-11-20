"""Parse bpftrace map dumps from the root trace-bpf mode.

bpftrace prints maps at exit as:

    @enter[fio]: 12345
    @sw[fio, iou-wrk-1234]: 7
    @dwell[iou-wrk-1234]: 987654321

Keys are comm names or comma-separated key tuples. Values are counts, or
nanoseconds for @dwell. Event loss is reported by bpftrace on stderr
("Lost N events") and must travel with every derived number; the absence
of that line is not proof of zero loss.

Counters are comm-keyed HOST_ISOLATED_AGGREGATE evidence: they carry no
pid/tgid, and the only foreign-process screen is the runner's pre-flight
`pgrep -x fio`. @cqe is comm-filtered like the other maps; other comms are
reported in separate `*_system_wide` fields and never folded into this
run's per-op values.
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
        k = ("",) if key is None else tuple(
            p.strip() for p in key.split(","))
        # bpftrace may print one line per CPU for the same key; aggregate.
        bucket[k] = bucket.get(k, 0) + value
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


IO_URING_COUNTERS = (
    "enters", "submits", "punts", "cqe_events",
    "cqe_events_non_fio_comms_system_wide",
    "iou_wrk_dwell_ns_system_wide", "iou_wrk_cqe_events_system_wide",
)
REQUIRED_IO_URING_MAPS = ("enter", "cqe")


def _has_comm(maps: dict, name: str, comm: str) -> bool:
    return any(_key_tuple(k)[0] == comm for k in (maps.get(name) or {}))


def summarize(maps: dict, ios: int | None, comm: str = "fio",
              io_uring_applicable: bool = True) -> dict:
    """Normalize the counters to per-op values. Workers are identified by
    the kernel naming convention iou-wrk-<pid>; only their dwell is
    summed (their own @enter/@futex keys do not exist because syscalls
    happen on the submitting thread in these backends).

    io_uring_applicable=False is the sluice-threadpool case: the io_uring
    maps are structurally absent, so their zero is NOT a measured zero.
    Those counters are reported as not-applicable and never enter per_op.
    """
    def worker(k: tuple) -> bool:
        return k[0].startswith("iou-wrk")

    values = {
        "enters": _sum(maps, "enter", lambda k: k[0] == comm),
        "submits": _sum(maps, "submit", lambda k: k[0] == comm),
        "punts": _sum(maps, "punt", lambda k: k[0] == comm),
        # io_uring_complete is system-wide: an unfiltered @cqe sum would
        # add every other io_uring comm on the host (and iou-wrk) to this
        # run's cqe/op, so cqe is comm-filtered like the rest.
        "cqe_events": _sum(maps, "cqe", lambda k: k[0] == comm),
        "cqe_events_non_fio_comms_system_wide": _sum(
            maps, "cqe", lambda k: k[0] != comm),
        "futex": _sum(maps, "futex", lambda k: k[0] == comm),
        "fio_dwell_ns": _sum(maps, "dwell", lambda k: k[0] == comm),
        # iou-wrk keys are system-wide comm matches (no ring association
        # without #622-style correlation): counted, never attributed.
        "iou_wrk_dwell_ns_system_wide": _sum(maps, "dwell", worker),
        "iou_wrk_cqe_events_system_wide": _sum(maps, "cqe", worker),
    }
    fio_keyed = any(_key_tuple(k)[0] == comm
                    for bucket in maps.values() for k in bucket)
    not_applicable = []
    required_missing = []
    if not io_uring_applicable:
        not_applicable = list(IO_URING_COUNTERS)
        for name in not_applicable:
            values[name] = None
    else:
        required_missing = [name for name in REQUIRED_IO_URING_MAPS
                            if not _has_comm(maps, name, comm)]
    out = {"comm": comm, "ios": ios, "counts": values,
           "per_op": {}, "dwell_ms": {},
           "scope": "HOST_ISOLATED_AGGREGATE",
           "fio_keyed": fio_keyed,
           "not_applicable": not_applicable,
           "required_missing": required_missing,
           "scope_note": "comm-keyed counters; isolation is only the "
                         "pre-flight `pgrep -x fio` gate, not a pid filter",
           "attribution_note": "iou-wrk keys are system-wide comm "
                               "matches; not attributed to this run"}
    if ios:
        zero_after_rounding = []
        for name, count in values.items():
            if count is None:
                continue
            # Nanosecond dwells and *_system_wide counters are not part of
            # this run's per-op namespace (they would otherwise be divided
            # by this run's ios and read as a per-op cost).
            if name.endswith("_ns") or "_ns_" in name \
                    or name.endswith("_system_wide"):
                continue
            per_op = round(count / ios, 6)
            out["per_op"][name] = per_op
            if count and per_op == 0.0:
                zero_after_rounding.append(name)
        out["counts_nonzero_but_per_op_zero"] = zero_after_rounding
        out["dwell_ms"] = {
            "fio": round(values["fio_dwell_ns"] / 1e6, 1),
            "iou_wrk_system_wide": round(
                (values["iou_wrk_dwell_ns_system_wide"] or 0) / 1e6, 1),
        }
        out["fio_dwell_us_per_op"] = round(
            values["fio_dwell_ns"] / 1e3 / ios, 3)
    return out

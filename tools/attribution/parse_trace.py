"""Parse trace receipts and guard request-join integrity.

Non-root substitute for eBPF (this round): strace -c -f summaries give
syscall COUNTS (io_uring_enter, futex, pread64...) for the straced process
tree. These are counted facts about a *perturbed* run -- the runner always
pairs them with a bare control run so the perturbation is itself measured.

Join guards implement the campaign rules: lost events, duplicated request
identity, or ambiguous keys downgrade any per-request join to AMBIGUOUS --
never a fabricated precise timeline.
"""

from __future__ import annotations

import re


def parse_strace_summary(raw: str) -> dict:
    """strace -c footer -> {syscall: {"calls": int, "errors": int|None,
    "seconds": float|None}}. The header/footer time columns are wall facts
    of a perturbed run, not clean latencies."""
    syscalls: dict = {}
    for line in raw.splitlines():
        m = re.match(
            r"\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+(\d+)\s+(\d+)\s+(\S+)\s*(.*)$",
            line)
        if m:
            seconds, _usec_per_call, _total_us, calls, errors, name = \
                m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6)
            try:
                syscalls[name] = {"calls": int(calls),
                                  "errors": int(errors),
                                  "seconds": float(seconds)}
            except ValueError:
                continue
    return syscalls


def parse_io_uring_enter_lines(raw: str) -> dict:
    """Full strace lines of io_uring_enter -> to_submit distribution and
    min-complete arguments: the H2 submission-shape evidence."""
    to_submit: dict = {}
    enters = 0
    for line in raw.splitlines():
        if "io_uring_enter(" not in line:
            continue
        m = re.search(r"io_uring_enter\(\s*\d+,\s*(\d+),\s*(\d+)", line)
        if not m:
            continue
        enters += 1
        key = (int(m.group(1)), int(m.group(2)))
        to_submit[key] = to_submit.get(key, 0) + 1
    return {"enters": enters, "to_submit_min_complete": to_submit}


class JoinIntegrity:
    """Per-request join admission (rules from #622/#623 join evidence)."""

    def __init__(self) -> None:
        self.lost_events: int | None = None
        self.duplicate_keys: int = 0

    def observe_lost_events(self, lost: int | None) -> None:
        self.lost_events = lost if lost is None else \
            (self.lost_events or 0) + lost

    def observe_keys(self, keys) -> None:
        seen: set = set()
        dup = 0
        for key in keys:
            if key in seen:
                dup += 1
            seen.add(key)
        self.duplicate_keys += dup

    def verdict(self) -> dict:
        if self.lost_events:
            return {"join": "INVALID", "reason":
                    "lost events (%s): precise per-request joins refused"
                    % self.lost_events}
        if self.duplicate_keys:
            return {"join": "AMBIGUOUS", "reason":
                    "duplicate request identity (%d): joins cannot be "
                    "1:1" % self.duplicate_keys}
        return {"join": "OK", "reason": "no loss, unique keys"}


def detect_lost_events(text: str) -> int | None:
    """bpftrace 'Lost N events' receipts; None when nothing was reported."""
    total = 0
    found = False
    for m in re.finditer(r"Lost (\d+) events", text):
        total += int(m.group(1))
        found = True
    return total if found else None

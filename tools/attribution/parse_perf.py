"""Parse raw perf receipts.

perf stat -x, line shape (perf 7.x):
    "<count>,<unit>,<event>,<time_running_ns>,<pct>,"
    "<count>,<unit>,<event>,<time_enabled_ns>,<time_running_ns>,<pct>,"
Values may be "<not counted>" or "<not supported>" -- those are capability
facts, never zeros. pct < 100 means the event was multiplexed (scaled):
such counts are SCALED and excluded from exact per-op tables.

perf report --stdio: we extract the sample-count header and the flat
percentage table. Percentages are SAMPLED shares; callers must never fold
them into counted per-op tables.
"""

from __future__ import annotations

import re


def parse_perf_stat_csv(raw: str) -> dict:
    """-> {event: {"value": int|None, "unit": str, "time_running_pct": float,
                   "kind": "counted"|"scaled"|"not_counted"|"not_supported"}}.

    perf -x, column order is fixed: value, unit, event, then either
    [running_ns, pct] (not multiplexed) or [enabled_ns, running_ns, pct]
    (multiplexed). The pct column carries no '%' suffix in CSV mode, and
    the unit column can hold 'msec' -- hence positional parsing, not name
    matching. A repeated event keeps the last line (per-CPU lines precede
    the aggregated total).
    """
    events: dict = {}
    for line in raw.splitlines():
        line = line.rstrip(",")
        if not line or "," not in line:
            continue
        fields = line.split(",")
        if len(fields) < 4:
            continue
        value_str, unit, event = fields[0], fields[1], fields[2]
        rest = fields[3:]
        # Drop trailing empty columns from the rstrip-joined format.
        while rest and rest[-1] == "":
            rest.pop()
        pct = None
        if rest:
            try:
                pct = float(rest[-1])
            except ValueError:
                pct = None
        if value_str.startswith("<"):
            kind = ("not_counted" if "counted" in value_str
                    else "not_supported")
            entry = {"value": None, "unit": unit,
                     "time_running_pct": pct, "kind": kind}
            events[event] = entry
            continue
        try:
            value = int(value_str)
        except ValueError:
            # Time-style events print decimals ("1881.0" msec); keep them
            # as floats so task-clock per-op math stays exact.
            try:
                value = float(value_str)
            except ValueError:
                continue
        kind = "counted" if (pct is None or pct >= 100.0) else "scaled"
        events[event] = {"value": value, "unit": unit,
                         "time_running_pct": pct, "kind": kind}
    return events


def exact_value(events: dict, event: str) -> int | None:
    """Value only when the count is exact (counted, never scaled)."""
    row = events.get(event)
    if row is None or row["kind"] != "counted":
        return None
    return row["value"]


def parse_perf_report(raw: str) -> dict:
    """-> {"samples": int|None, "event_lines": [(pct, samples, symbol)],
           "overhead_note": str} -- SAMPLED data, marked as such."""
    samples = None
    # perf prints "10K" / "1M" for large counts -- expand the suffix.
    m = re.search(r"# Samples: ([\d.]+)([KMG]?) of event\s+'([^']+)'", raw)
    if m:
        mult = {"": 1, "K": 1000, "M": 1_000_000,
                "G": 1_000_000_000}[m.group(2)]
        samples = int(float(m.group(1)) * mult)
    event_lines = []
    in_table = False
    for line in raw.splitlines():
        if line.startswith("# Overhead"):
            in_table = True
            continue
        if not in_table:
            continue
        if line.startswith("#") and "...." in line:
            continue
        m = re.match(r"\s*([\d.]+)%\s+([\d.]+)?\s*(.*)", line)
        if m:
            pct = float(m.group(1))
            rest = m.group(3) or ""
            sym = rest.strip()
            if sym and not sym.startswith("#"):
                event_lines.append((pct, sym))
    return {"samples": samples, "flat_symbols": event_lines,
            "class": "SAMPLED"}

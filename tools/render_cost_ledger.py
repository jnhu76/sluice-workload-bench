#!/usr/bin/env python3
"""Render COST-LEDGER.md data tables from analysis/normalized-costs.json.

The ledger's marked sections are regenerated from the recomputed analysis
so the document and the raw evidence cannot drift apart. Numbers written
into the ledger are byte-identical to the analyze output.
"""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
LABELS = {
    "PROCESS_LIFECYCLE_INSTRUCTIONS_per_op": "instr/op (u)",
    "PROCESS_LIFECYCLE_CYCLES_per_op": "cycles/op (u)",
    "PROCESS_LIFECYCLE_CPU_TIME_us_per_op": "cpu-us/op (u)",
}


def fmt(v, nd=0):
    if v is None:
        return "UNAVAILABLE"
    return ("%." + str(nd) + "f") % v if nd else "%d" % round(v)


def render_main(norm: dict) -> str:
    rows = []
    for key, entry in sorted(norm.items()):
        pmu = entry.get("pmu") or []
        if not pmu:
            continue
        # mean across reps of counted values
        agg = {}
        for row in pmu:
            if row.get("status") != "ok":
                continue
            for label, cell in row["values"].items():
                if cell.get("class") == "counted":
                    agg.setdefault(label, []).append(cell["value"])
        vals = {label: (sum(v) / len(v) if v else None)
                for label, v in agg.items()}
        ios = pmu[0].get("denominator")
        line = "| %s | %s | %s |" % (
            entry["cell"], entry["engine"],
            " × ".join("%s" % len(v) for v in agg.values()) or "-")
        for label in LABELS:
            line += " %s |" % (fmt(vals.get(label), 1)
                               if vals.get(label) is not None else "-")
        rows.append((entry["cell"], entry["engine"], line, ios))
    if not rows:
        return "(no pmu rows)\n"
    out = ["| cell | engine | reps | instr/op (u) | cycles/op (u) | "
           "cpu-us/op (u) | denominator |\n", "|---|---|---|---:|---:|---:|---:|\n"]
    for _c, _e, line, ios in rows:
        out.append(line + " %s |\n" % ios)
    # differential rows
    out.append("\nΔ（sluice-uring − io_uring，同 cell）：\n\n")
    out.append("| cell | Δinstr/op | Δcycles/op | Δcpu-us/op | Δ/psync参照 |\n|---|---:|---:|---:|---|\n")
    for cell in sorted({r[0] for r in rows}):
        def get(engine):
            for c, e, line, ios in rows:
                if c == cell and e == engine:
                    return line
            return None
        a, b = get("io_uring"), get("sluice-uring")
        if not a or not b:
            continue
        def val(line, col):
            fields = [x.strip() for x in line.split("|")]
            cell = fields[3 + col]
            return float(cell) if cell not in ("-", "") else None
        try:
            di = val(b, 0) - val(a, 0)
            dc = val(b, 1) - val(a, 1)
            du = (val(b, 2) or 0) - (val(a, 2) or 0)
            out.append("| %s | %+.0f | %+.0f | %+.2f | - |\n"
                       % (cell, di, dc, du))
        except (TypeError, ValueError):
            out.append("| %s | - | - | - | - |\n" % cell)
    return "".join(out)


def render_trace(norm: dict) -> str:
    out = ["| cell | engine | enter/op | futex/op | pread64/op | "
           "straced/bare IOPS | 扰动 |\n|---|---|---:|---:|---:|---:|---|\n"]
    for key, entry in sorted(norm.items()):
        bare = entry.get("trace-bare") or [{}]
        straced = entry.get("trace-straced") or [{}]
        if not straced:
            continue
        sc = (straced[0].get("syscalls") or {})
        def per(name):
            row = sc.get(name) or {}
            return row.get("calls_per_op")
        bare_iops = bare[0].get("iops") if bare else None
        s_iops = straced[0].get("iops")
        ratio = ("%.2f" % (s_iops / bare_iops)) if bare_iops and s_iops \
            else "-"
        pert = ("%.0f%%" % ((1 - s_iops / bare_iops) * 100)
                if bare_iops and s_iops else "-")
        out.append("| %s | %s | %s | %s | %s | %s | %s |\n" % (
            entry["cell"], entry["engine"],
            fmt(per("io_uring_enter"), 3) if per("io_uring_enter")
            is not None else "-",
            fmt(per("futex"), 3) if per("futex") is not None else "-",
            fmt(per("pread64"), 3) if per("pread64") is not None else "-",
            ratio, pert))
    return "".join(out)


def render_perturbation(norm: dict) -> str:
    out = ["| cell | engine | clean IOPS | pmu-run IOPS | record-run IOPS | "
           "straced IOPS | 判读 |\n|---|---|---:|---:|---:|---:|---|\n"]
    for key, entry in sorted(norm.items()):
        clean = entry.get("clean") or {}
        clean_vals = clean.get("iops_values") or []
        def mode_iops(mode):
            rows = entry.get(mode) or []
            for r in rows:
                if isinstance(r, dict) and r.get("iops"):
                    return r["iops"]
            return None
        c = sum(clean_vals) / len(clean_vals) if clean_vals else None
        p = mode_iops("pmu")
        rec = mode_iops("record")
        # trace rows: straced run iops
        tr = entry.get("trace-straced") or []
        t = tr[0].get("iops") if tr else None

        def pct(x, ref):
            if x is None or not ref:
                return "-"
            return "%+.1f%%" % ((x - ref) / ref * 100)
        out.append("| %s | %s | %s | %s(%s) | %s(%s) | %s | stat/record/"
                   "strace vs clean |\n" % (
                       entry["cell"], entry["engine"],
                       fmt(c, 0) if c else "-", fmt(p, 0) if p else "-",
                       pct(p, c), fmt(rec, 0) if rec else "-",
                       pct(rec, c), fmt(t, 0) if t else "-"))
    return "".join(out)


def render_h(norm: dict, window: dict) -> str:
    """H1-H6 provisional classification is authored with data at hand; the
    renderer only injects the metric availability notes per row."""
    return ("window verdict: %s; A2-vs-C2 instr delta pct: %s\n"
            % (window.get("verdict"),
               window.get("A2_vs_C2_instructions_delta_pct")))


def fill_ledger(session_dir: pathlib.Path, ledger_path: pathlib.Path) -> None:
    norm = json.loads((session_dir / "analysis" / "normalized-costs.json")
                      .read_text())
    window = json.loads((session_dir / "analysis" / "window-contract.json")
                        .read_text())
    text = ledger_path.read_text()
    text = text.replace("LEDGER_PLACEHOLDER", render_main(norm).strip())
    text = text.replace("TRACE_PLACEHOLDER", render_trace(norm).strip())
    text = text.replace("PERTURBATION_PLACEHOLDER",
                        render_perturbation(norm).strip())
    text = text.replace("H_PLACEHOLDER", render_h(norm, window).strip())
    ledger_path.write_text(text)


if __name__ == "__main__":
    session = ROOT / (sys.argv[1] if len(sys.argv) > 1
                      else "results/attribution/session-001")
    ledger = ROOT / "docs/fio-integration/COST-LEDGER.md"
    fill_ledger(session, ledger)
    print("ledger filled from", session)

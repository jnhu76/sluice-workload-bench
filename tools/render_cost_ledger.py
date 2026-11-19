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
        pmu = [r for r in (entry.get("pmu") or [])
               if r.get("status") == "ok"]
        if not pmu:
            continue
        agg = {}
        for row in pmu:
            for label, cell in row["values"].items():
                if cell.get("class") == "counted":
                    agg.setdefault(label, []).append(cell["value"])
        vals = {label: (sum(v) / len(v) if v else None)
                for label, v in agg.items()}
        ios = pmu[0].get("denominator")
        rows.append({"cell": entry["cell"], "engine": entry["engine"],
                     "reps": len(pmu), "ios": ios, "vals": vals})
    if not rows:
        return "(no pmu rows)\n"
    out = ["| cell | engine | reps | instr/op (u) | cycles/op (u) | "
           "cpu-us/op (u) | denominator |\n",
           "|---|---|---:|---:|---:|---:|---:|\n"]
    for r in rows:
        out.append("| %s | %s | %d | %s | %s | %s | %s |\n" % (
            r["cell"], r["engine"], r["reps"],
            fmt(r["vals"].get(list(LABELS)[0]), 1),
            fmt(r["vals"].get(list(LABELS)[1]), 1),
            fmt(r["vals"].get(list(LABELS)[2]), 1),
            r["ios"]))
    out.append("\nΔ（sluice-uring − io_uring，同 cell；正 = sluice 更贵）：\n\n")
    out.append("| cell | Δinstr/op | Δinstr% | Δcycles/op | "
               "Δcpu-us/op |\n|---|---:|---:|---:|---:|\n")
    by_cell = {}
    for r in rows:
        by_cell.setdefault(r["cell"], {})[r["engine"]] = r
    for cell in sorted(by_cell):
        a = by_cell[cell].get("io_uring")
        b = by_cell[cell].get("sluice-uring")
        if not a or not b:
            continue
        labels = list(LABELS)
        av = [a["vals"].get(l) for l in labels]
        bv = [b["vals"].get(l) for l in labels]
        if None in av or None in bv:
            out.append("| %s | - | - | - | - |\n" % cell)
            continue
        out.append("| %s | %+.1f | %+.1f%% | %+.1f | %+.2f |\n" % (
            cell, bv[0] - av[0], (bv[0] - av[0]) / av[0] * 100,
            bv[1] - av[1], bv[2] - av[2]))
    return "".join(out)


def render_trace(norm: dict) -> str:
    out = ["| cell | engine | enter/op | futex/op | pread64/op | "
           "straced/bare IOPS | 扰动 |\n|---|---|---:|---:|---:|---:|---|\n"]
    for key, entry in sorted(norm.items()):
        bare = entry.get("trace-bare") or [{}]
        straced = entry.get("trace-straced") or []
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
    """Observer perturbation, computed from each mode's own fio JSON IOPS.
    clean0 (ramp=0) is the reference: only its window matches the pmu and
    record runs (all ramp=0). clean (ramp=2) is shown for reference but
    ranks differently by construction."""
    out = ["| cell | engine | clean0 IOPS (ref) | pmu vs clean0 | "
           "record vs clean0 | trace-straced vs trace-bare |\n"
           "|---|---|---:|---:|---:|---:|\n"]
    for key, entry in sorted(norm.items()):
        clean0 = entry.get("clean0") or {}
        vals = clean0.get("iops_values") or []
        ref = sum(vals) / len(vals) if vals else None

        def mode_iops(mode):
            rows = entry.get(mode) or []
            iops = [r.get("iops") for r in rows
                    if isinstance(r, dict) and r.get("iops")]
            return sum(iops) / len(iops) if iops else None

        def pct(x, base):
            if x is None or not base:
                return "-"
            return "%+.1f%%" % ((x - base) / base * 100)

        pmu = mode_iops("pmu")
        rec = mode_iops("record")
        bare = mode_iops("trace-bare")
        straced = mode_iops("trace-straced")
        out.append("| %s | %s | %s | %s (%s) | %s (%s) | %s (%s) |\n" % (
            entry["cell"], entry["engine"],
            fmt(ref, 0) if ref else "-",
            fmt(pmu, 0) if pmu else "-", pct(pmu, ref),
            fmt(rec, 0) if rec else "-", pct(rec, ref),
            fmt(straced, 0) if straced else "-", pct(straced, bare)))
    return "".join(out)


def render_rusage(norm: dict) -> str:
    """clean0 (ramp=0, window-matched) rusage per-op lines."""
    lines = []
    for key, entry in sorted(norm.items()):
        clean0 = entry.get("clean0") or {}
        rows = clean0.get("rusage_per_op") or []
        if not rows:
            continue
        def mean(field):
            vals = [r[field] for r in rows if r.get(field) is not None]
            return sum(vals) / len(vals) if vals else None
        ctx = mean("ctx_switches_per_op")
        cpu_u = mean("cpu_user_us_per_op")
        cpu_s = mean("cpu_sys_us_per_op")
        cpu_t = mean("cpu_total_us_per_op")
        lines.append("  - %s/%s：ctx-switch/op %.5f；CPU user/sys/total "
                     "%.2f / %.2f / %.2f µs/op" % (
                         entry["cell"], entry["engine"],
                         ctx if ctx is not None else float("nan"),
                         cpu_u if cpu_u is not None else float("nan"),
                         cpu_s if cpu_s is not None else float("nan"),
                         cpu_t if cpu_t is not None else float("nan")))
    return "\n".join(lines) if lines else "  (no clean0 rusage rows)"


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
    text = text.replace("RUSAGE_PLACEHOLDER", render_rusage(norm).strip())
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

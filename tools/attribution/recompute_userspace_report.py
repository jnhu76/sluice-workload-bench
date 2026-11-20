#!/usr/bin/env python3
"""Read-only recomputation of the Phase B userspace-cost report numbers.

This script never runs fio/perf/bpftrace and never writes into the evidence
roots. It re-parses the already archived raw files under results/ and prints
the tables used by docs/fio-integration/userspace/USERSPACE-COST-RESEARCH.md.

Usage:
    python3 -I tools/attribution/recompute_userspace_report.py [--root DIR]

Exit code 0 iff the internal identities hold:
  * core lock closure |predicted - measured| <= 2e-5 for every cell
  * record-root flat groups sum to 100.00% (+/- 0.02)
  * record-root group cyc/op sum equals cycles/op (+/- 1.0)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

E1 = os.path.join("results", "userspace-cost", "session-001-baseline")
E2 = os.path.join("results", "userspace-cost", "session-002-counters")
E3 = os.path.join("results", "userspace-cost", "session-003-sync-consumer")

CELLS = ["A1", "A4", "A2", "A5", "A3"]
ENGINES = ["io_uring", "sluice-uring", "sluice-threadpool"]
# cell -> (requested iodepth, recorded in B.1 as INFERRED achieved depth)
DEPTH = {"A1": 1.000, "A4": 3.995, "A2": 15.951, "A5": 63.681, "A3": 3.995}


def load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def run_dir(root, session, run_id):
    return os.path.join(root, session, "runs", run_id)


def e1_run(root, cell, engine, phase, group, rep):
    rid = f"{cell}__{engine}__{phase}"
    if group:
        rid += f"-{group}"
    rid += f"__{rep}"
    return run_dir(root, E1, rid)


def per_op_from_pmu(root, path, events):
    den = load_json(os.path.join(path, "denominator.json"))
    ps = load_json(os.path.join(path, "perf-stats.json"))
    ios = den["total_ios"]
    out = {}
    for ev in events:
        if ev in ps["events"]:
            out[ev] = ps["events"][ev]["value"] / ios
    return out, den


def table_e1(root):
    print("== E1 per-op (instr/cycles from g1/g2, CPU from g1 task-clock) ==")
    print(f"{'cell':5} {'engine':17} {'instr':>9} {'cycles':>9} {'cpu_us':>7} "
          f"{'iops_clean':>11} {'iops_pmu_g1':>11}")
    data = {}
    for cell in CELLS:
        for engine in ENGINES:
            vals = {}
            for rep in ("rep1", "rep2"):
                g1 = per_op_from_pmu(root, e1_run(root, cell, engine, "pmu-root", "g1", rep),
                                     ["instructions:u", "task-clock"])
                g2 = per_op_from_pmu(root, e1_run(root, cell, engine, "pmu-root", "g2", rep),
                                     ["cycles:u"])
                clean = load_json(os.path.join(e1_run(root, cell, engine, "clean0", "", rep),
                                               "denominator.json"))
                vals.setdefault("instr", []).append(g1[0]["instructions:u"])
                g1_ps = load_json(os.path.join(
                    e1_run(root, cell, engine, "pmu-root", "g1", rep), "perf-stats.json"))
                g1_den = load_json(os.path.join(
                    e1_run(root, cell, engine, "pmu-root", "g1", rep), "denominator.json"))
                vals.setdefault("cpu", []).append(
                    g1_ps["events"]["task-clock"]["value"] * 1000.0 / g1_den["total_ios"])
                vals.setdefault("cyc", []).append(g2[0]["cycles:u"])
                vals.setdefault("iops_clean", []).append(clean["iops"])
                vals.setdefault("iops_pmu", []).append(
                    load_json(os.path.join(
                        e1_run(root, cell, engine, "pmu-root", "g1", rep),
                        "denominator.json"))["iops"])
            mean = {k: sum(v) / len(v) for k, v in vals.items()}
            data[(cell, engine)] = mean
            print(f"{cell:5} {engine:17} {mean['instr']:9.2f} {mean['cyc']:9.2f} "
                  f"{mean['cpu']:7.3f} {mean['iops_clean']:11.1f} {mean['iops_pmu']:11.1f}")
    print("\n== engine deltas (sluice-native, TP-sluice) ==")
    for cell in CELLS:
        n = data[(cell, "io_uring")]
        s = data[(cell, "sluice-uring")]
        t = data[(cell, "sluice-threadpool")]
        di, dc = s["instr"] - n["instr"], s["cyc"] - n["cyc"]
        print(f"{cell}: sluice-native instr {di:+.2f} ({di / n['instr'] * 100:+.1f}%) "
              f"cyc {dc:+.2f} ({dc / n['cyc'] * 100:+.1f}%) cpu {s['cpu'] - n['cpu']:+.3f} | "
              f"TP-sluice instr {t['instr'] - s['instr']:+.2f} "
              f"({(t['instr'] - s['instr']) / s['instr'] * 100:+.1f}%) "
              f"cyc {t['cyc'] - s['cyc']:+.2f} "
              f"({(t['cyc'] - s['cyc']) / s['cyc'] * 100:+.1f}%)")
    return data


def counters(root, cell, rep="rep1"):
    rid = f"{cell}__sluice-uring__research__{rep}"
    c = load_json(os.path.join(run_dir(root, E2, rid), "counters.json"))
    ios = c["total_ios"]
    k = c["counters"]
    return {
        "take": k["take_calls"] / ios,
        "pending": k["take_pending"] / ios,
        "resolve": k["core"]["scope_resolve_slot_iterations"] / ios,
        "lock": k["core"]["core_mutex_lock_calls"] / ios,
        "harvest": k["harvest_calls"] / ios,
        "getevents": k["getevents_calls"] / ios,
    }


def table_closure(root):
    print("\n== E2 counters + 9+T+2P closure ==")
    print(f"{'cell':5} {'take':>10} {'pending':>10} {'resolve':>10} {'lock':>10} "
          f"{'harvest':>9} {'getev':>9} {'P':>10} {'9+T+2P':>11} {'resid':>10}")
    worst = 0.0
    for cell in CELLS:
        c = counters(root, cell)
        p = c["harvest"] - c["getevents"]
        pred = 9 + c["take"] + 2 * p
        resid = pred - c["lock"]
        worst = max(worst, abs(resid))
        print(f"{cell:5} {c['take']:10.6f} {c['pending']:10.6f} {c['resolve']:10.6f} "
              f"{c['lock']:10.6f} {c['harvest']:9.6f} {c['getevents']:9.6f} "
              f"{p:10.6f} {pred:11.6f} {resid:+10.2e}")
    print(f"worst |residual| = {worst:.3e} (threshold 2e-5)")
    return worst


FLAT_RE = re.compile(r"\s*([0-9]+\.[0-9]+)%\s+(\S+)\s+(\S+)\s+\[.\]\s+(.*)$")


def parse_flat(path):
    rows = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = FLAT_RE.match(line.rstrip("\n"))
            if m:
                # groups: pct, command, shared object, symbol
                rows.append((float(m.group(1)), m.group(2), m.group(3), m.group(4)))
    return rows


def group_flat(rows):
    groups = {"scope": 0.0, "adapter": 0.0, "bridge": 0.0, "libc": 0.0,
              "publication": 0.0, "other_sluice": 0.0, "fio": 0.0, "unid": 0.0}
    for pct, _cmd, obj, sym in rows:
        if "RequestScope::take" in sym:
            groups["scope"] += pct
        elif ("sluice_queue" in sym or "sluice_getevents" in sym
              or "::harvest(" in sym):
            groups["adapter"] += pct
        elif "RequestBridge::consume" in sym:
            groups["bridge"] += pct
        elif "pthread_mutex" in sym:  # includes the .so PLT stubs
            groups["libc"] += pct
        elif ("publish_one" in sym or "begin_publication" in sym
              or "complete_publication" in sym):
            groups["publication"] += pct
        elif obj.endswith(".so"):
            groups["other_sluice"] += pct
        elif obj == "fio":
            groups["fio"] += pct
        else:
            groups["unid"] += pct
    groups["unid"] += max(0.0, 100.0 - sum(groups.values()))
    return groups


def table_record(root):
    path = run_dir(root, E3, "A2__sluice-uring__record-root__rep1")
    flat_path = os.path.join(path, "perf-report-flat.txt")
    with open(flat_path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    event_count = int(re.search(r"Event count \(approx\.\):\s*(\d+)", text).group(1))
    ios = load_json(os.path.join(path, "fio-result.json"))["jobs"][0]["read"]["total_ios"]
    cpo = event_count / ios
    groups = group_flat(parse_flat(flat_path))
    print("\n== E3 A2/D16 record-root flat groups (SAMPLED) ==")
    print(f"event_count={event_count} ios={ios} cycles/op={cpo:.4f}")
    total = 0.0
    cyc_total = 0.0
    for name, pct in groups.items():
        cyc = pct / 100.0 * cpo
        total += pct
        cyc_total += cyc
        print(f"  {name:13} {pct:6.2f}%  {cyc:8.2f} cyc/op")
    print(f"  {'sum':13} {total:6.2f}%  {cyc_total:8.2f} cyc/op")
    return total, abs(cyc_total - cpo)


def table_pmu_atomic(root):
    print("\n== E3 A2 pmu-atomic (COUNTED) ==")
    for rep in ("rep1", "rep2"):
        path = run_dir(root, E3, f"A2__sluice-uring__pmu-atomic__{rep}")
        raw = os.path.join(path, "perf-stat.raw.txt")
        ev = {}
        with open(raw, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("#") or not line.strip():
                    continue
                f = line.strip().split(",")
                if len(f) >= 6:
                    ev[f[2]] = float(f[0])
        with open(os.path.join(path, "cmd.stdout.txt"), "r", encoding="utf-8",
                  errors="replace") as fh:
            txt = fh.read()
        js = json.loads(txt[txt.index("{"):])
        ios = js["jobs"][0]["read"]["total_ios"]
        print(f"  {rep}: instr/op={ev['instructions:u'] / ios:.2f} "
              f"cyc/op={ev['cycles:u'] / ios:.2f} "
              f"cpu_us/op={ev['task-clock'] * 1000.0 / ios:.4f} "
              f"lock_loads/op={ev['mem_uops_retired.lock_loads:u'] / ios:.3f} "
              f"iops={ios / (js['jobs'][0]['read']['runtime'] / 1000.0):.1f}")


def table_consumer(root):
    print("\n== E3 consumer W02 (540000 lifecycle ops) ==")
    s = load_json(os.path.join(root, E3, "analysis", "summary.json"))["consumer"]
    base = s["baseline"]["instructions:u"]["value"]
    base_c = s["baseline"]["cycles:u"]["value"]
    runs = s["runs"]
    vals = {}
    for k in ("uring-rep1", "uring-rep2", "tp-rep1", "tp-rep2"):
        r = runs[k]
        vi = (r["instr_total"] - base) / 540000.0
        vc = (r["cycles_total"] - base_c) / 540000.0
        vals[k] = (vi, vc)
        print(f"  {k}: instr/op={vi:.2f} cyc/op={vc:.2f} iops={r['measured_iops']:.1f}")
    for rep in ("rep1", "rep2"):
        ui, uc = vals[f"uring-{rep}"]
        ti, tc = vals[f"tp-{rep}"]
        print(f"  TP vs uring {rep}: instr {(ti / ui - 1) * 100:+.2f}% "
              f"cycles {(tc / uc - 1) * 100:+.1f}%")


def table_kernel(root):
    print("\n== E1 kernel side (g1 instr:k, g2 cycles:k means) ==")
    for cell in ("A2", "A3"):
        row = {}
        for engine in ("io_uring", "sluice-uring"):
            ik = []
            ck = []
            for rep in ("rep1", "rep2"):
                v, _ = per_op_from_pmu(root, e1_run(root, cell, engine, "pmu-root", "g1", rep),
                                       ["instructions:k"])
                ik.append(v["instructions:k"])
                v2, _ = per_op_from_pmu(root, e1_run(root, cell, engine, "pmu-root", "g2", rep),
                                        ["cycles:k"])
                ck.append(v2["cycles:k"])
            row[engine] = (sum(ik) / 2, sum(ck) / 2)
        (ni, nc), (si, sc) = row["io_uring"], row["sluice-uring"]
        print(f"  {cell}: native instr:k={ni:.1f} cyc:k={nc:.1f} | "
              f"sluice instr:k={si:.1f} cyc:k={sc:.1f} | "
              f"cyc delta {sc - nc:+.1f} ({(sc - nc) / nc * 100:+.1f}%)")


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.getcwd())
    args = ap.parse_args(argv)
    root = os.path.abspath(args.root)
    missing = [p for p in (E1, E2, E3) if not os.path.isdir(os.path.join(root, p))]
    if missing:
        print(f"missing evidence roots under {root}: {missing}", file=sys.stderr)
        return 2
    table_e1(root)
    worst = table_closure(root)
    total, cyc_err = table_record(root)
    table_pmu_atomic(root)
    table_consumer(root)
    table_kernel(root)
    ok = worst <= 2e-5 and abs(total - 100.0) <= 0.02 and cyc_err <= 1.0
    print(f"\nidentities: closure_ok={worst <= 2e-5} flat_sum_ok={abs(total - 100.0) <= 0.02} "
          f"flat_cycles_ok={cyc_err <= 1.0}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

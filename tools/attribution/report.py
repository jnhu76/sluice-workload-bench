"""analyze: recompute normalized costs from raw session artifacts only.

Everything is derived from files on disk (fio JSON originals, perf stat
CSVs, strace summaries, run meta) -- no runtime state survives between the
measurement and this recomputation. Rules enforced here (each has a test):

  * per-op values only when the counting window matches the denominator
    (Option B lifecycle / Option C fixed-count); otherwise REFUSED.
  * SCALED / not-counted perf events never enter exact per-op tables.
  * kernel-scope rows stay UNAVAILABLE unless a :k receipt exists --
    never zero.
  * sampled (perf record) percentages stay in a SAMPLED section.
  * clat stays "completion latency (clat)", distinct from total lat.
  * runs of different modes / engine revisions never merge silently.
  * F3-class cells never appear in rankings.
"""

from __future__ import annotations

import json
import pathlib
import re

from . import parse_perf, parse_trace

RANKED_EXCLUDED_PREFIXES = ("F3",)  # environment-confounded, never ranked
MODES = ("clean", "pmu", "pmu-aux1", "pmu-aux2", "record",
         "trace-bare", "trace-straced", "trace-shape")


def parse_fio_json(raw: str) -> dict | None:
    start = raw.find("{")
    if start < 0:
        return None
    try:
        doc = json.loads(raw[start:])
        job = doc["jobs"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    return job


def lifecycle_ios(job: dict, bs_bytes: int) -> int | None:
    """Option B denominator: every io the process completed, ramp included.
    io_bytes is cumulative for the job; uniform bs cells divide exactly."""
    total = 0
    found = False
    for d in ("read", "write"):
        section = job.get(d) or {}
        io_bytes = section.get("io_bytes") or 0
        if io_bytes:
            found = True
            total += io_bytes
    if not found:
        return None
    if total % bs_bytes:
        return None  # non-uniform io sizes: refuse exact division
    return total // bs_bytes


def fio_metrics(raw: str) -> dict | None:
    job = parse_fio_json(raw)
    if job is None:
        return None
    bs_min = None
    opts = job.get("job options") or {}
    bs_txt = str(opts.get("bs") or "4k")
    m = re.match(r"(\d+)([kmKM]?)", bs_txt)
    if m:
        mult = {"": 1, "k": 1024, "m": 1024 * 1024}.get(m.group(2).lower(), 1)
        bs_min = int(m.group(1)) * mult
    out = {"job_name": job.get("jobname"), "bs_bytes": bs_min,
           "usr_cpu": job.get("usr_cpu"), "sys_cpu": job.get("sys_cpu"),
           "iodepth_level": job.get("iodepth_level"),
           "lifecycle_ios": lifecycle_ios(job, bs_min) if bs_min else None,
           "directions": {}}
    for d in ("read", "write"):
        section = job.get(d) or {}
        if not section.get("iops"):
            continue
        clat_ns = section.get("clat_ns")
        scale = 1.0
        pct = {}
        if clat_ns:
            pct = clat_ns.get("percentile") or {}
        else:
            clat_us = section.get("clat_us") or {}
            pct = clat_us.get("percentile") or {}
            scale = 1000.0

        def p(key):
            v = pct.get(key)
            return round(v * scale / 1000.0, 3) if v is not None else None

        out["directions"][d] = {
            "iops": round(section["iops"], 3),
            "bw_bytes_per_s": section.get("bw_bytes"),
            # Labels are part of the contract: these are COMPLETION latency
            # percentiles, not total-latency ones.
            "clat_p50_us": p("50.000000"),
            "clat_p95_us": p("95.000000"),
            "clat_p99_us": p("99.000000"),
            "io_bytes": section.get("io_bytes"),
        }
        lat_ns = section.get("lat_ns")
        if lat_ns:
            lp = lat_ns.get("percentile") or {}
            out["directions"][d]["lat_p99_us"] = round(
                lp.get("99.000000", 0) / 1000.0, 3) if \
                lp.get("99.000000") is not None else None
    return out


def load_run(run_dir: pathlib.Path) -> dict | None:
    meta_p = run_dir / "meta.json"
    fio_p = run_dir / "fio-result.json"
    if not meta_p.exists():
        return None
    meta = json.loads(meta_p.read_text())
    run = {"run_id": run_dir.name, "dir": str(run_dir), "meta": meta,
           "metrics": None, "perf": None, "trace": None}
    if fio_p.exists():
        run["metrics"] = fio_metrics(fio_p.read_text())
    perf_p = run_dir / "perf-stat.raw.txt"
    if perf_p.exists():
        run["perf"] = parse_perf.parse_perf_stat_csv(perf_p.read_text())
    strace_p = run_dir / "strace-summary.txt"
    if strace_p.exists():
        run["trace"] = parse_trace.parse_strace_summary(
            strace_p.read_text())
    shape_p = run_dir / "enter-shape.txt"
    if shape_p.exists():
        run["enter_shape"] = parse_trace.parse_io_uring_enter_lines(
            shape_p.read_text())
    report_p = run_dir / "perf-report-flat.txt"
    if not report_p.exists():
        report_p = run_dir / "perf-report.txt"
    if report_p.exists():
        run["perf_report"] = parse_perf.parse_perf_report(
            report_p.read_text())
    return run


def split_run_id(run_id: str) -> dict:
    """<cell>__<engine>__<mode>__rep<N> with optional trailing r (retry)."""
    parts = run_id.split("__")
    if len(parts) != 4:
        return {}
    rep = parts[3]
    m = re.match(r"rep(\d+)(r?)", rep)
    return {"cell": parts[0], "engine": parts[1], "mode": parts[2],
            "repeat": int(m.group(1)) if m else None,
            "retry": bool(m and m.group(2))}


def per_op_row(run: dict, window: str) -> dict:
    """Build the per-op row for one pmu run under a denominator policy.
    Returns {'status': 'ok'|'REFUSED', ...} -- refused rows carry the
    reason and never a numeric per-op value."""
    parts = split_run_id(run["run_id"])
    metrics = run["metrics"] or {}
    ios = metrics.get("lifecycle_ios")
    perf = run["perf"] or {}
    row = {"status": "REFUSED", "window": window,
           "denominator": None, "values": {}}
    if not ios or ios <= 0:
        row["reason"] = ("no lifecycle denominator (io_bytes missing or "
                         "non-uniform bs)")
        return row
    row["denominator"] = ios
    wanted = {
        "PROCESS_LIFECYCLE_INSTRUCTIONS_per_op": "instructions:u",
        "PROCESS_LIFECYCLE_CYCLES_per_op": "cycles:u",
        "PROCESS_LIFECYCLE_CPU_TIME_us_per_op": "task-clock:u",
    }
    have_exact = False
    for label, event in wanted.items():
        value = parse_perf.exact_value(perf, event)
        if value is None:
            ev = perf.get(event)
            if ev and ev["kind"] in ("not_counted", "not_supported"):
                row["values"][label] = {"value": None,
                                        "class": "UNAVAILABLE"}
            elif ev and ev["kind"] == "scaled":
                row["values"][label] = {
                    "value": None, "class": "SCALED",
                    "time_running_pct": ev["time_running_pct"],
                    "note": "multiplexed count excluded from exact table"}
            else:
                row["values"][label] = {"value": None,
                                        "class": "NO_RECEIPT"}
            continue
        have_exact = True
        per_op = value / ios
        if "CPU_TIME" in label:
            row["values"][label] = {"value": round(per_op * 1000.0, 3),
                                    "class": "counted",
                                    "scope": "user", "unit": "us"}
        else:
            row["values"][label] = {"value": round(per_op, 3),
                                    "class": "counted", "scope": "user"}
    row["status"] = "ok" if have_exact else "REFUSED"
    if not have_exact:
        row["reason"] = "no exact (unmultiplexed, counted) core event"
    return row


def analyze_session(session_dir: pathlib.Path) -> dict:
    """Full recompute. Writes analysis/*.json, returns the analysis doc."""
    session_dir = session_dir.resolve()
    runs_dir = session_dir / "runs"
    analysis_dir = session_dir / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    capability = None
    cap_path = session_dir / "capability.json"
    if cap_path.exists():
        capability = json.loads(cap_path.read_text())

    runs = []
    sha_mismatch = []
    revision = None
    binrev_path = session_dir / "binary-manifest.json"
    if binrev_path.exists():
        revision = json.loads(binrev_path.read_text()).get("sluice_revision")
    for run_dir in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
        run = load_run(run_dir)
        if run is None:
            continue
        meta = run["meta"]
        if meta.get("discarded"):
            run["discarded"] = True
        load_line = meta.get("engine_load")
        if load_line:
            if revision and load_line[0] != revision:
                sha_mismatch.append({"run_id": run["run_id"],
                                     "engine_rev": load_line[0],
                                     "session_rev": revision})
        runs.append(run)

    # Group by (cell, engine) keeping modes SEPARATE (no cross-mode merge).
    cells: dict = {}
    for run in runs:
        parts = split_run_id(run["run_id"])
        if not parts or run.get("discarded"):
            continue
        key = (parts["cell"], parts["engine"])
        cells.setdefault(key, {}).setdefault(
            "modes", {}).setdefault(parts["mode"], []).append(run)

    normalized = {}
    for (cell, engine), group in sorted(cells.items()):
        entry = {"cell": cell, "engine": engine}
        if "clean" in group["modes"]:
            entry["clean"] = _aggregate_clean(group["modes"]["clean"])
        for mode in ("pmu", "pmu-aux1", "pmu-aux2"):
            if mode in group["modes"]:
                rows = []
                for run in group["modes"][mode]:
                    row = per_op_row(run, "process_lifecycle"
                                     if mode == "pmu" else "aux")
                    row["run_id"] = run["run_id"]
                    _attach_aux(row, run, mode)
                    rows.append(row)
                entry[mode] = rows
        if "record" in group["modes"]:
            entry["record"] = [
                {"run_id": r["run_id"], "class": "SAMPLED",
                 "perf_report": r.get("perf_report")}
                for r in group["modes"]["record"]]
        for tmode in ("trace-bare", "trace-straced", "trace-shape"):
            if tmode in group["modes"]:
                entry[tmode] = [
                    _trace_row(r) for r in group["modes"][tmode]]
        normalized["%s/%s" % (cell, engine)] = entry

    window = _window_contract(cells)
    (analysis_dir / "window-contract.json").write_text(
        json.dumps(window, indent=2, sort_keys=True) + "\n")
    (analysis_dir / "normalized-costs.json").write_text(
        json.dumps(normalized, indent=2, sort_keys=True) + "\n")

    ranking = _ranking(normalized)
    analysis = {
        "schema": 1,
        "session": str(session_dir.name),
        "runs_total": len(runs),
        "runs_discarded": sum(1 for r in runs if r.get("discarded")),
        "sha_mismatch": sha_mismatch,
        "sha_mismatch_free": not sha_mismatch,
        "cpu_accounting_verdict":
            (capability or {}).get("cpu_accounting_verdict",
                                   "UNKNOWN_NO_CAPABILITY_FILE"),
        "window_contract": window["verdict"],
        "normalized": normalized,
        "ranking": ranking,
        "invariants": {
            "f3_excluded_from_ranking": all(
                not e["cell"].startswith(RANKED_EXCLUDED_PREFIXES)
                for e in ranking),
            "modes_never_merged": True,
            "sampled_isolated": True,
        },
    }
    (analysis_dir / "analysis.json").write_text(
        json.dumps(analysis, indent=2, sort_keys=True) + "\n")
    return analysis


def _aggregate_clean(clean_runs: list) -> dict:
    out = {"runs": [], "iops_values": []}
    for run in clean_runs:
        m = run["metrics"] or {}
        direction = (m.get("directions") or {}).get("read") or {}
        out["runs"].append({
            "run_id": run["run_id"],
            "iops": direction.get("iops"),
            "clat_p50_us": direction.get("clat_p50_us"),
            "clat_p95_us": direction.get("clat_p95_us"),
            "clat_p99_us": direction.get("clat_p99_us"),
            "lifecycle_ios": m.get("lifecycle_ios"),
            "rusage": run["meta"].get("rusage"),
            "usr_cpu": m.get("usr_cpu"), "sys_cpu": m.get("sys_cpu"),
        })
        if direction.get("iops"):
            out["iops_values"].append(direction["iops"])
    vals = out["iops_values"]
    if len(vals) >= 2:
        spread = (max(vals) - min(vals)) / (sum(vals) / len(vals)) * 100
        out["observed_spread_pct"] = round(spread, 3)
        out["spread_class"] = "HIGH_SPREAD" if spread > 15 else "ok"
    return out


def _attach_aux(row: dict, run: dict, mode: str) -> None:
    if mode == "pmu":
        return
    perf = run["perf"] or {}
    metrics = run["metrics"] or {}
    ios = metrics.get("lifecycle_ios")
    aux = {}
    for event, entry in perf.items():
        if entry["kind"] == "counted":
            aux[event] = {"per_op": round(entry["value"] / ios, 6)
                          if ios else None,
                          "class": "counted"}
        elif entry["kind"] == "scaled":
            aux[event] = {"per_op": None, "class": "SCALED",
                          "time_running_pct": entry["time_running_pct"]}
        else:
            aux[event] = {"per_op": None, "class": "UNAVAILABLE"}
    row["aux_events"] = aux


def _trace_row(run: dict) -> dict:
    m = run["metrics"] or {}
    ios = m.get("lifecycle_ios")
    out = {"run_id": run["run_id"], "lifecycle_ios": ios,
           "iops": ((m.get("directions") or {}).get("read") or {})
           .get("iops"),
           "label": "STRACE_DIAGNOSTIC"}
    if run.get("trace"):
        counts = {}
        for name, entry in run["trace"].items():
            counts[name] = {"calls": entry["calls"],
                            "calls_per_op": round(entry["calls"] / ios, 6)
                            if ios else None}
        out["syscalls"] = counts
    if run.get("enter_shape"):
        out["enter_shape"] = run["enter_shape"]
    return out


def _window_contract(cells: dict) -> dict:
    """Option B vs Option C: lifecycle per-op vs fixed-count per-op."""
    verdict = {"option_B": "present" if any(
        "pmu" in group["modes"] for group in cells.values()) else "absent"}
    comparisons = []
    for (cell, engine), group in cells.items():
        if cell != "C2":
            continue
        for mode in ("clean", "pmu"):
            if mode in group["modes"]:
                verdict["option_C_%s" % mode] = "present"
    # Numeric comparison happens in the COST-LEDGER step using this data.
    verdict["verdict"] = "PROCESS_LIFECYCLE_PRIMARY"
    b_instr = _pick_per_op(cells, "A2", "pmu", 
                           "PROCESS_LIFECYCLE_INSTRUCTIONS_per_op")
    c_instr = _pick_per_op(cells, "C2", "pmu",
                           "PROCESS_LIFECYCLE_INSTRUCTIONS_per_op")
    if b_instr is not None and c_instr is not None and c_instr:
        delta_pct = abs(b_instr - c_instr) / c_instr * 100
        verdict["A2_vs_C2_instructions_delta_pct"] = round(delta_pct, 3)
        verdict["startup_share_bound_ok"] = delta_pct <= 10.0
        if delta_pct > 10.0:
            verdict["verdict"] = "LIFECYCLE_ONLY_NO_STEADY_STATE_EXTRAPOLATION"
    return verdict


def _pick_per_op(cells: dict, cell: str, mode: str, label: str):
    group = cells.get((cell, "io_uring"))
    if not group or mode not in group.get("modes", {}):
        return None
    for run in group["modes"][mode]:
        row = per_op_row(run, "process_lifecycle")
        if row["status"] == "ok":
            v = row["values"].get(label, {})
            if v.get("class") == "counted":
                return v.get("value")
    return None


def _ranking(normalized: dict) -> list:
    """Ranking across engines per cell, clean-mode IOPS only. F3-class and
    non-clean modes never enter."""
    table = []
    for key, entry in normalized.items():
        if entry["cell"].startswith(RANKED_EXCLUDED_PREFIXES):
            continue
        clean = entry.get("clean") or {}
        vals = clean.get("iops_values") or []
        if not vals:
            continue
        table.append({"cell": entry["cell"], "engine": entry["engine"],
                      "mean_iops": round(sum(vals) / len(vals), 1),
                      "observed_spread_pct": clean.get(
                          "observed_spread_pct")})
    return table

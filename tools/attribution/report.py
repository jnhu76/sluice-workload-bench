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

from . import parse_bpftrace, parse_perf, parse_trace

RANKED_EXCLUDED_PREFIXES = ("F3",)  # environment-confounded, never ranked
MODES = ("clean", "clean0", "pmu", "pmu-aux1", "pmu-aux2",
         "pmu-root", "record", "record-root", "record-root-kernel",
         "trace-bare", "trace-straced", "trace-shape", "trace-bpf")


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
    bs_txt = str(opts.get("bs") or "")
    # Only a single uniform block size can serve as the lifecycle
    # denominator. Mixed/range forms ("4k:64k", "4k,64k", "4k-64k") are
    # refused instead of silently taking the first token.
    if bs_txt and not re.search(r"[:,\-]", bs_txt):
        m = re.fullmatch(r"(\d+)([kmKM]?)", bs_txt.strip())
        if m:
            mult = {"": 1, "k": 1024,
                    "m": 1024 * 1024}.get(m.group(2).lower(), 1)
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
    try:
        meta = json.loads(meta_p.read_text())
    except (ValueError, OSError) as exc:
        # A corrupted receipt must be excluded, not crash the recompute.
        return {"run_id": run_dir.name, "dir": str(run_dir), "meta": {},
                "job_text": "", "metrics": None, "perf": None,
                "trace": None, "discarded": True, "unreadable": True,
                "unreadable_reason": "malformed meta.json: %s" % exc}
    job_p = run_dir / "fio-job.fio"
    job_text = job_p.read_text() if job_p.exists() else ""
    run = {"run_id": run_dir.name, "dir": str(run_dir), "meta": meta,
           "job_text": job_text, "metrics": None, "perf": None,
           "trace": None}
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
    bpf_p = run_dir / "bpftrace.stdout.txt"
    if bpf_p.exists():
        err_p = run_dir / "bpftrace.stderr.txt"
        stderr = err_p.read_text() if err_p.exists() else ""
        attach = ""
        for line in stderr.splitlines():
            if line.strip().startswith("Attached "):
                attach = line.strip()
                break
        run["bpftrace"] = {
            "maps": parse_bpftrace.parse_maps(bpf_p.read_text()),
            "lost_events": parse_bpftrace.detect_lost(stderr),
            "stderr": stderr,
            "attach_line": attach,
        }
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


def parse_ramp_time(job_text: str) -> int | None:
    """Anchored ramp_time parser. A substring test for "ramp_time=0" also
    matches "ramp_time=0.5", which would silently accept a mismatched
    window; every per-op row needs its OWN job receipt to say 0."""
    m = re.search(r"(?m)^\s*ramp_time\s*=\s*(\d+)\s*$", job_text or "")
    return int(m.group(1)) if m else None


def per_op_row(run: dict, window: str) -> dict:
    """Build the per-op row for one pmu run under a denominator policy.
    Returns {'status': 'ok'|'REFUSED', ...} -- refused rows carry the
    reason and never a numeric per-op value."""
    parts = split_run_id(run["run_id"])
    metrics = run["metrics"] or {}
    ios = metrics.get("lifecycle_ios")
    perf = run["perf"] or {}
    row = {"status": "REFUSED", "window": window,
           "window_verified": False, "denominator": None, "values": {}}
    ramp = parse_ramp_time(run.get("job_text") or "")
    if ramp is None:
        row["reason"] = ("no ramp_time receipt in the run's job text; "
                         "measurement window cannot be verified")
        return row
    if ramp != 0:
        row["reason"] = ("ramp_time=%d: perf window includes ramp but the "
                         "io_bytes denominator does not; per-op refused"
                         % ramp)
        return row
    row["window_verified"] = True
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
            # task-clock is NOT user-only: on this stack it equals rusage
            # user+sys per op exactly (measured cross-check), i.e. process
            # tree CPU time; it still excludes io-wq/kworker threads.
            row["values"][label] = {
                "value": round(per_op * 1000.0, 3),
                "class": "counted", "scope": "task_total",
                "unit": "us",
                "scope_note": "user+sys of the fio process tree; excludes "
                              "io-wq/kworkers (PARTIAL_CPU_ACCOUNTING)"}
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
    if not runs_dir.is_dir():
        raise SystemExit("analyze: no runs directory at %s" % runs_dir)
    analysis_dir = session_dir / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    capability = None
    capability_error = None
    cap_path = session_dir / "capability.json"
    if cap_path.exists():
        try:
            capability = json.loads(cap_path.read_text())
        except (ValueError, OSError) as exc:
            capability_error = "malformed capability.json: %s" % exc

    runs = []
    sha_mismatch = []
    revision = None
    manifest = None
    manifest_error = None
    manifest_path = session_dir / "binary-manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
            revision = manifest.get("sluice_revision")
        except (ValueError, OSError) as exc:
            manifest_error = "malformed binary-manifest.json: %s" % exc
    for run_dir in sorted(p for p in runs_dir.iterdir()
                          if p.is_dir() and not p.name.startswith(".")):
        run = load_run(run_dir)
        if run is None:
            continue
        meta = run["meta"]
        if meta.get("discarded") or meta.get("status") != "ok":
            # Failed receipts stay on disk (evidence discipline) but never
            # enter the recomputation.
            run["discarded"] = True
        load_line = meta.get("engine_load")
        if load_line:
            if revision and load_line[0] != revision:
                sha_mismatch.append({"run_id": run["run_id"],
                                     "engine_rev": load_line[0],
                                     "session_rev": revision})
        runs.append(run)

    # Evidence gate: record/record-root attribution compares a diagnostic
    # binary against the release baseline, so the manifest and its
    # .text-section identity proof are REQUIRED for those modes. A missing
    # or unproven manifest is not "clean" -- it is fail-closed.
    record_modes = {"record", "record-root", "record-root-k"}
    uses_record = any(
        (split_run_id(r["run_id"]).get("mode") in record_modes)
        and not r.get("discarded") for r in runs)
    text_mismatch = []
    engines = (manifest or {}).get("engines") or {}
    # Only external .so engines have a sym-twin proof; io_uring/psync are
    # covered by the diagnostic fio binary hash instead.
    external_engines = {"sluice-uring", "sluice-threadpool"}
    used_engines = {split_run_id(r["run_id"]).get("engine")
                    for r in runs
                    if split_run_id(r["run_id"]).get("mode") in record_modes}
    for engine in sorted(e for e in used_engines
                         if e and e in external_engines):
        row = engines.get(engine) or {}
        if row.get("text_section_match") is not True:
            text_mismatch.append(engine)
    fio_diag = (manifest or {}).get("fio_diag") or {}
    fio_diag_proven = bool(fio_diag.get("sha256"))
    provenance_missing = [
        r["run_id"] for r in runs
        if split_run_id(r["run_id"]).get("mode") in record_modes
        and not r.get("discarded")
        and split_run_id(r["run_id"]).get("engine") in external_engines
        and not r["meta"].get("engine_load")]
    if not runs:
        manifest_gate = "NO_RUNS"
    elif uses_record and manifest is None:
        manifest_gate = "MISSING"
    elif text_mismatch:
        manifest_gate = "TEXT_SECTION_MISMATCH"
    elif uses_record and not fio_diag_proven:
        manifest_gate = "FIO_DIAG_UNPROVEN"
    elif provenance_missing:
        manifest_gate = "ENGINE_LOAD_MISSING"
    elif manifest is not None and manifest_error:
        manifest_gate = "UNREADABLE"
    else:
        manifest_gate = "OK" if manifest is not None else "NOT_REQUIRED"

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
        # clean  = Phase-1-identical baseline (ramp=2; IOPS/latency stats)
        # clean0 = ramp=0 un-instrumented runs; the ONLY clean window that
        #          matches the rusage/lifecycle denominator (per-op rule).
        if "clean" in group["modes"]:
            entry["clean"] = _aggregate_clean(group["modes"]["clean"],
                                              window_matched=False)
        if "clean0" in group["modes"]:
            entry["clean0"] = _aggregate_clean(group["modes"]["clean0"],
                                               window_matched=True)
        for mode in ("pmu", "pmu-aux1", "pmu-aux2"):
            if mode in group["modes"]:
                rows = []
                for run in group["modes"][mode]:
                    row = per_op_row(run, "process_lifecycle"
                                     if mode == "pmu" else "aux")
                    row["run_id"] = run["run_id"]
                    direction = (((run["metrics"] or {}).get("directions")
                                  or {}).get("read") or {})
                    row["iops"] = direction.get("iops")
                    row["clat_p99_us"] = direction.get("clat_p99_us")
                    _attach_aux(row, run, mode)
                    if mode != "pmu":
                        # Aux runs carry no core events: their validity is
                        # decided by the aux events, not by the (always
                        # REFUSED) main-event row.
                        aux = row.get("aux_events") or {}
                        if any(v.get("class") == "counted"
                               for v in aux.values()):
                            row["main_status"] = row["status"]
                            row["main_reason"] = row.pop("reason", None)
                            row["status"] = "ok"
                    rows.append(row)
                entry[mode] = rows
        if "record" in group["modes"]:
            entry["record"] = []
            for r in group["modes"]["record"]:
                direction = (((r["metrics"] or {}).get("directions")
                              or {}).get("read") or {})
                entry["record"].append(
                    {"run_id": r["run_id"], "class": "SAMPLED",
                     "iops": direction.get("iops"),
                     "perf_report": r.get("perf_report")})
        for tmode in ("trace-bare", "trace-straced", "trace-shape"):
            if tmode in group["modes"]:
                entry[tmode] = [
                    _trace_row(r) for r in group["modes"][tmode]]
        # Root PMU: every counted event, scope derived from the :u/:k
        # suffix; kernel rows are now REAL counts for the measured scopes
        # (capability FULL_FOR_MEASURED_SCOPES: :k PMU is measured,
        # io-wq/kworker and IRQ/softirq are not).
        for mode in ("pmu-root-g1", "pmu-root-g2", "pmu-root-g3"):
            if mode in group["modes"]:
                rows = []
                for run in group["modes"][mode]:
                    row = _root_pmu_row(run)
                    row["run_id"] = run["run_id"]
                    rows.append(row)
                entry.setdefault("pmu-root", []).extend(rows)
        for mode in ("record-root", "record-root-k"):
            if mode in group["modes"]:
                key = ("record-root" if mode == "record-root"
                       else "record-root-kernel")
                entry[key] = []
                for r in group["modes"][mode]:
                    entry[key].append({
                        "run_id": r["run_id"],
                        "class": ("SAMPLED" if mode == "record-root"
                                  else "SAMPLED_KERNEL"),
                        "perf_report": r.get("perf_report")})
        if "trace-bpf" in group["modes"]:
            entry["trace-bpf"] = [_bpf_row(r) for r in
                                  group["modes"]["trace-bpf"]]
        normalized["%s/%s" % (cell, engine)] = entry

    window = _window_contract(cells)
    (analysis_dir / "window-contract.json").write_text(
        json.dumps(window, indent=2, sort_keys=True) + "\n")
    (analysis_dir / "normalized-costs.json").write_text(
        json.dumps(normalized, indent=2, sort_keys=True) + "\n")

    ranking = _ranking(normalized)
    comparisons = _comparisons(ranking)
    # Authoritative per-run index rebuilt from run dirs (the append-only
    # runs-manifest.json accumulated duplicates across repair invocations).
    index = []
    excluded_unparsed = []
    for run in runs:
        parts = split_run_id(run["run_id"])
        unparsed = not parts
        if unparsed and not run.get("discarded"):
            excluded_unparsed.append(run["run_id"])
        index.append({
            "run_id": run["run_id"],
            "cell": parts.get("cell"), "engine": parts.get("engine"),
            "mode": parts.get("mode"), "repeat": parts.get("repeat"),
            "retry": parts.get("retry"),
            "status": (run["meta"].get("status")),
            "discarded": bool(run.get("discarded")),
            "excluded_reason": ("unparseable run id / prep / superseded"
                                if unparsed else None),
            "dir": str(pathlib.Path(run["dir"]).name),
        })
    (analysis_dir / "runs-index.json").write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n")

    # Computed invariants (audit finding: these were hardcoded before).
    mode_by_run: dict = {}
    merged = False
    for entry in normalized.values():
        for mode in MODES:
            for row in (entry.get(mode) or []):
                rid = row.get("run_id") if isinstance(row, dict) else None
                if not rid:
                    continue
                if rid in mode_by_run and mode_by_run[rid] != mode:
                    merged = True
                mode_by_run[rid] = mode
    sampled_isolated = all(
        str(row.get("class", "")).startswith("SAMPLED")
        and "values" not in row and "per_op" not in row
        for entry in normalized.values()
        for mode in ("record", "record-root", "record-root-kernel")
        for row in (entry.get(mode) or []))
    ebpf_rows = [row for entry in normalized.values()
                 for row in (entry.get("trace-bpf") or [])]
    ebpf = {
        "rows": len(ebpf_rows),
        "counted_runs": [r["run_id"] for r in ebpf_rows
                         if r.get("label") == "EBPF_COUNTED"],
        "lossy_runs": [r["run_id"] for r in ebpf_rows
                       if r.get("label") == "EBPF_LOSSY"],
        "unavailable_runs": [r["run_id"] for r in ebpf_rows
                             if r.get("label") == "EBPF_UNAVAILABLE"],
        "scope": "HOST_ISOLATED_AGGREGATE",
    }
    sha_mismatch_free = (not sha_mismatch and not manifest_error
                         and manifest_gate in ("OK", "NOT_REQUIRED"))
    analysis = {
        "schema": 1,
        "session": str(session_dir.name),
        "runs_total": len(runs),
        "runs_discarded": sum(1 for r in runs if r.get("discarded")),
        "runs_unparsed_excluded": excluded_unparsed,
        "sha_mismatch": sha_mismatch,
        "sha_mismatch_text": text_mismatch,
        "sha_mismatch_provenance": provenance_missing,
        "sha_mismatch_free": sha_mismatch_free,
        "binary_manifest": {
            "present": manifest is not None,
            "gate": manifest_gate,
            "error": manifest_error,
            "sluice_revision": revision,
            "record_modes_present": uses_record,
            "fio_diag_proven": fio_diag_proven,
        },
        "cpu_accounting_verdict":
            (capability or {}).get("cpu_accounting_verdict",
                                   "UNKNOWN_NO_CAPABILITY_FILE"),
        "cpu_accounting_scopes":
            (capability or {}).get("cpu_accounting_scopes", {}),
        "capability_error": capability_error,
        "window_contract": window["verdict"],
        "window_contract_detail": window,
        "normalized": normalized,
        "ebpf": ebpf,
        "ranking": ranking,
        "comparisons": comparisons,
        "invariants": {
            "f3_excluded_from_ranking": all(
                not e["cell"].startswith(RANKED_EXCLUDED_PREFIXES)
                for e in ranking),
            "psync_excluded_from_ranking": all(
                e["engine"] != "psync" for e in ranking),
            "modes_never_merged": not merged,
            "sampled_isolated": sampled_isolated,
            "ebpf_lossy_not_exact": all(
                r.get("label") != "EBPF_LOSSY"
                or r.get("summary", {}).get("dwell_invalid") is True
                for r in ebpf_rows),
        },
    }
    (analysis_dir / "analysis.json").write_text(
        json.dumps(analysis, indent=2, sort_keys=True) + "\n")
    return analysis


def _aggregate_clean(clean_runs: list, window_matched: bool) -> dict:
    """window_matched=True is only valid for ramp=0 runs: rusage covers the
    whole process while fio's io_bytes covers the post-ramp window, so for
    ramp=2 clean runs a per-op rusage division would be inflated by the
    ramp fraction (~20% here). Those rows stay raw values, no per-op."""
    out = {"runs": [], "iops_values": [], "window_matched": True}
    if not window_matched:
        out["window_matched"] = False
        out["per_op_note"] = (
            "CLEAN_RAMP2_LIFECYCLE_MISMATCH: rusage window includes ramp; "
            "io_bytes excludes it -- rusage per-op is computed only for "
            "runs whose own job text says ramp_time=0 (e.g. C2 clean)")
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
    # Scheduling counters: rusage is exact and unaffected by the perf
    # exclude_kernel restriction (scope = fio process tree incl. workers).
    # Per-run window check (not the invocation label): a clean run is only
    # window-matched when its OWN job text carries ramp_time=0.
    per_op = []
    for run in clean_runs:
        # Anchored parse: "ramp_time=0.5" must NOT be treated as ramp=0.
        if parse_ramp_time(run.get("job_text") or "") != 0:
            continue
        m = run["metrics"] or {}
        ios = m.get("lifecycle_ios")
        ru = run["meta"].get("rusage") or {}
        if ios and ru:
            per_op.append({
                "run_id": run["run_id"],
                "ctx_switches_per_op":
                    round((ru["ctx_voluntary"] + ru["ctx_involuntary"])
                          / ios, 6),
                "cpu_user_us_per_op": round(
                    ru["cpu_user_s"] * 1e6 / ios, 4),
                "cpu_sys_us_per_op": round(
                    ru["cpu_sys_s"] * 1e6 / ios, 4),
                "cpu_total_us_per_op": round(
                    (ru["cpu_user_s"] + ru["cpu_sys_s"]) * 1e6 / ios, 4),
            })
    if per_op:
        out["rusage_per_op"] = per_op
    out["window_matched"] = bool(per_op) and len(per_op) == len(clean_runs)
    if per_op and not out["window_matched"]:
        out["per_op_note"] = (out.get("per_op_note", "") +
                              " (partially window-matched: only ramp_time=0 "
                              "runs carry rusage per-op)").strip()
    return out


SCHED_SW_EVENTS = ("context-switches", "context-switches:u",
                   "cpu-migrations", "cpu-migrations:u")


def _attach_aux(row: dict, run: dict, mode: str) -> None:
    if mode == "pmu":
        return
    perf = run["perf"] or {}
    metrics = run["metrics"] or {}
    ios = metrics.get("lifecycle_ios")
    aux = {}
    for event, entry in perf.items():
        if entry["kind"] == "counted":
            # paranoid=2 applies exclude_kernel, so kernel-occurring SW
            # events read 0 regardless of real activity. A zero there is
            # INVALID as a count, never "no switches happened" (rusage is
            # the authoritative scheduling counter).
            if event in SCHED_SW_EVENTS and not entry["value"]:
                aux[event] = {
                    "per_op": None, "class": "INVALID_ZERO_UNDER_"
                    "EXCLUDE_KERNEL",
                    "note": "use rusage ctx counters instead"}
                continue
            aux[event] = {"per_op": round(entry["value"] / ios, 6)
                          if ios else None,
                          "class": "counted"}
        elif entry["kind"] == "scaled":
            aux[event] = {"per_op": None, "class": "SCALED",
                          "time_running_pct": entry["time_running_pct"]}
        else:
            aux[event] = {"per_op": None, "class": "UNAVAILABLE"}
    row["aux_events"] = aux


def _root_pmu_row(run: dict) -> dict:
    metrics = run["metrics"] or {}
    ios = metrics.get("lifecycle_ios")
    direction = ((metrics.get("directions") or {}).get("read") or {})
    row = {"status": "REFUSED", "window": "process_lifecycle",
           "window_verified": False,
           "denominator_kind": "lifecycle_ios",
           "label": "PROCESS_LIFECYCLE_*_per_op",
           "scope_note": "task-clock/task_total = user+sys of the fio "
                         "process tree (not user-only); :k counts cover "
                         "that process tree only, not io-wq/kworker or "
                         "IRQ/softirq; excludes io-wq/kworkers",
           "denominator": ios, "iops": direction.get("iops"),
           "values": {}}
    ramp = parse_ramp_time(run.get("job_text") or "")
    if ramp is None:
        row["reason"] = ("no ramp_time receipt; measurement window cannot "
                         "be verified")
        return row
    if ramp != 0:
        row["reason"] = ("ramp_time=%d: perf window includes ramp but the "
                         "io_bytes denominator does not; per-op refused"
                         % ramp)
        return row
    row["window_verified"] = True
    if not ios:
        row["reason"] = "no lifecycle denominator"
        return row
    for event, entry in (run["perf"] or {}).items():
        scope = ("kernel" if event.endswith(":k")
                 else "user" if event.endswith(":u") else "task_total")
        if entry["kind"] == "counted":
            value = entry["value"] / ios
            if event.startswith("task-clock"):
                row["values"][event] = {
                    "per_op": round(value * 1000.0, 3), "unit": "us",
                    "class": "counted", "scope": "task_total"}
            else:
                row["values"][event] = {
                    "per_op": round(value, 4), "class": "counted",
                    "scope": scope}
        elif entry["kind"] == "scaled":
            row["values"][event] = {"per_op": None, "class": "SCALED",
                                    "time_running_pct":
                                    entry["time_running_pct"]}
        else:
            row["values"][event] = {"per_op": None,
                                    "class": "UNAVAILABLE"}
    row["status"] = ("ok" if any(v.get("class") == "counted"
                                 for v in row["values"].values())
                     else "REFUSED")
    return row


def _bpf_row(run: dict) -> dict:
    metrics = run["metrics"] or {}
    ios = metrics.get("lifecycle_ios")
    direction = ((metrics.get("directions") or {}).get("read") or {})
    receipt = run.get("bpftrace")
    maps = (receipt or {}).get("maps") or {}
    lost = (receipt or {}).get("lost_events")
    attach = (receipt or {}).get("attach_line") or ""
    base = {"run_id": run["run_id"], "lifecycle_ios": ios,
            "iops": direction.get("iops"), "lost_events": lost,
            "attach_line": attach,
            "join": "NOT_ATTEMPTED (aggregate counts only)"}
    if not receipt:
        # Missing map dump/receipt: never a counted zero.
        base.update({"label": "EBPF_UNAVAILABLE", "summary": {},
                     "scope": "UNAVAILABLE",
                     "reliability": "no bpftrace map dump; counts "
                                    "UNAVAILABLE"})
        return base
    if not maps:
        base.update({"label": "EBPF_UNAVAILABLE", "summary": {},
                     "scope": "UNAVAILABLE",
                     "reliability": "empty bpftrace map dump (killed or "
                                    "failed before exit); counts UNAVAILABLE"})
        return base
    if not attach:
        base.update({"label": "EBPF_UNAVAILABLE", "summary": {},
                     "scope": "UNAVAILABLE",
                     "reliability": "no 'Attached N probes' receipt; attach "
                                    "could not be proven; counts UNAVAILABLE"})
        return base
    parts = split_run_id(run["run_id"])
    io_uring_applicable = parts.get("engine") != "sluice-threadpool"
    summary = parse_bpftrace.summarize(
        maps, ios, io_uring_applicable=io_uring_applicable)
    if not summary.get("fio_keyed"):
        base.update({"label": "EBPF_UNAVAILABLE", "summary": summary,
                     "scope": "UNAVAILABLE",
                     "reliability": "no fio-keyed counter in the map dump; "
                                    "other comms only, counts UNAVAILABLE"})
        return base
    if summary.get("required_missing"):
        base.update({"label": "EBPF_UNAVAILABLE", "summary": summary,
                     "scope": "UNAVAILABLE",
                     "reliability": "required io_uring maps missing: %s; "
                                    "counts UNAVAILABLE"
                                    % ",".join(summary["required_missing"])})
        return base
    reliability = ("comm-aggregated counts (HOST_ISOLATED_AGGREGATE); "
                   "no Lost-events line reported (not proof of zero loss)")
    if lost:
        # #623 coverage class CAPTURE_DROPPED: counts become lower bounds,
        # dwell-derived values are invalid, and no precise claim is allowed.
        summary = {"per_op_lower_bound": (summary or {}).get("per_op"),
                   "dwell_invalid": True,
                   "counts": (summary or {}).get("counts")}
        base.update({
            "label": "EBPF_LOSSY", "summary": summary,
            "scope": "HOST_ISOLATED_AGGREGATE",
            "reliability": "CAPTURE_DROPPED: %d events lost; counters are "
                           "lower bounds, dwell INVALID" % lost})
        return base
    meta = run.get("meta") or {}
    if meta.get("bpf_retry_outside_window"):
        base.update({"label": "EBPF_UNAVAILABLE", "summary": summary,
                     "scope": "UNAVAILABLE",
                     "reliability": "accepted run is a retry outside the "
                                    "traced attempt; aggregate counts are "
                                    "not attributable"})
        return base
    window_s = meta.get("bpf_window_s")
    window_basis = "META"
    if window_s is None:
        # Legacy trace-bpf runs did not persist bpf_window_s. The runner's
        # window formula is runtime + ramp + 4; reconstruct it conservatively
        # from the frozen job text (the job's ramp_time=0).
        job_text = run.get("job_text") or ""
        m = re.search(r"^runtime=(\d+)", job_text, re.M)
        r = re.search(r"^ramp_time=(\d+)", job_text, re.M)
        if m:
            window_s = int(m.group(1)) + (int(r.group(1)) if r else 0) + 4
            window_basis = "RECONSTRUCTED_FROM_JOB"
    wall_s = meta.get("wall_s")
    if window_s is None or wall_s is None:
        base.update({"label": "EBPF_WINDOW_UNVERIFIED", "summary": summary,
                     "scope": "HOST_ISOLATED_AGGREGATE",
                     "reliability": "no bpf_window_s/wall_s receipt and no "
                                    "job runtime to reconstruct; trace "
                                    "coverage unverified"})
        return base
    attach_s = meta.get("bpf_attach_s")
    attach_basis = "RECORDED"
    if attach_s is None:
        # Legacy runs did not record attach latency. The window has a fixed
        # 4 s tail; 1 s is a conservative allowance, not a receipt.
        attach_s = 1.0
        attach_basis = "ASSUMED_1S"
    if attach_s + wall_s > window_s:
        base.update({"label": "EBPF_WINDOW_OVERSHOOT", "summary": summary,
                     "scope": "HOST_ISOLATED_AGGREGATE",
                     "reliability": "trace window=%ss < attach=%ss + fio "
                                    "wall=%ss; counts are lower bounds and "
                                    "per-op (full lifecycle_ios) is biased "
                                    "low" % (window_s, attach_s, wall_s)})
        return base
    base.update({"label": "EBPF_COUNTED", "summary": summary,
                 "scope": "HOST_ISOLATED_AGGREGATE",
                 "reliability": reliability,
                 "window": {"window_s": window_s, "wall_s": wall_s,
                            "attach_s": attach_s,
                            "attach_basis": attach_basis,
                            "window_basis": window_basis}})
    return base


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


OPTION_B_MODES = ("pmu", "pmu-root-g1", "pmu-root-g2", "pmu-root-g3")


def _direction_vs_startup(wall_b, wall_c, per_op_b, per_op_c) -> str:
    """If C2 runs longer it amortizes startup better, so its per-op value
    should be LOWER. The reverse (longer C2 yet higher per-op, or shorter
    C2 yet lower per-op) is the opposite of startup amortization."""
    if None in (wall_b, wall_c, per_op_b, per_op_c):
        return "UNKNOWN"
    if wall_c > wall_b:
        return "OPPOSITE" if per_op_c >= per_op_b else "CONSISTENT"
    if wall_c < wall_b:
        return "OPPOSITE" if per_op_c <= per_op_b else "CONSISTENT"
    return "UNKNOWN"


def _mean_wall(cells: dict, cell: str, engine: str, mode: str):
    group = cells.get((cell, engine))
    if not group or mode not in group.get("modes", {}):
        return None
    vals = [run["meta"].get("wall_s") for run in group["modes"][mode]
            if run["meta"].get("wall_s") is not None]
    return sum(vals) / len(vals) if vals else None


def _per_op_class(engine: str, delta: float) -> str:
    """Only native io_uring may use lifecycle_primary, and only inside the
    pre-registered 10% cross-mode gate. sluice-uring and threadpool carry
    cross-mode uncertainty (7.4%/4.1%) even below 10%, so they are always
    lifecycle-only."""
    return ("lifecycle_primary"
            if engine == "io_uring" and delta <= 10.0
            else "lifecycle_only")


def _window_contract(cells: dict) -> dict:
    """Option B vs Option C, computed PER ENGINE. The delta is |B-C|/C
    using the mean of all valid reps. The A2-vs-C2 difference changes the
    time basis as well as the window, so it cannot identify startup cost
    on its own: the direction check and the mechanism label are part of
    the contract."""
    option_b = any(mode in group["modes"]
                   for group in cells.values()
                   for mode in OPTION_B_MODES)
    option_c = any(cell == "C2" and "pmu" in group["modes"]
                   for (cell, _engine), group in cells.items())
    verdict = {
        "option_B": "present" if option_b else "absent",
        "option_C": "present" if option_c else "absent",
        "option_C_kind": "FIXED_IO_SIZE_COUNT" if option_c else "ABSENT",
        "window": "ramp_time=0 process_lifecycle",
        "denominator": "lifecycle_ios (read.io_bytes/bs, same run)",
    }
    per_engine = {}
    for engine in ("io_uring", "sluice-uring", "sluice-threadpool"):
        b = _mean_per_op(cells, "A2", engine, "pmu")
        c = _mean_per_op(cells, "C2", engine, "pmu")
        if b is None or c is None or not c:
            continue
        wall_b = _mean_wall(cells, "A2", engine, "pmu")
        wall_c = _mean_wall(cells, "C2", engine, "pmu")
        delta = round(abs(b - c) / c * 100, 3)
        direction = _direction_vs_startup(wall_b, wall_c, b, c)
        per_engine[engine] = {
            "A2_instr_per_op": round(b, 3),
            "C2_instr_per_op": round(c, 3),
            "A2_wall_s": None if wall_b is None else round(wall_b, 3),
            "C2_wall_s": None if wall_c is None else round(wall_c, 3),
            "delta_pct_of_C": delta,
            "direction_vs_startup": direction,
            "cross_mode_mechanism": (
                "MODE_OR_TIMING_EFFECT_NOT_IDENTIFIED"
                if direction == "OPPOSITE" else
                "STARTUP_AMORTIZATION_CONSISTENT"
                if direction == "CONSISTENT" else "NOT_IDENTIFIED"),
            "cross_mode_uncertainty_pct": delta,
            "per_op_class": _per_op_class(engine, delta),
            "C2_reps": _count_ok(cells, "C2", engine, "pmu"),
            "A2_reps": _count_ok(cells, "A2", engine, "pmu"),
        }
    verdict["per_engine"] = per_engine
    native = per_engine.get("io_uring", {})
    native_delta = native.get("delta_pct_of_C")
    if native_delta is not None:
        verdict["startup_share_bound_pct"] = native_delta
        verdict["startup_share_bound_ok"] = native_delta <= 10.0
        verdict["startup_share_bound_scope"] = \
            "NATIVE_TOTAL_CROSS_MODE_DIFF_UPPER_BOUND"
    if native_delta is not None and native_delta <= 10.0:
        verdict["verdict"] = "PROCESS_LIFECYCLE_PRIMARY_NATIVE_VERIFIED"
        verdict["sluice_cross_mode_pct"] = per_engine.get(
            "sluice-uring", {}).get("delta_pct_of_C")
        verdict["note"] = (
            "native 的 |A2-C2|/C <=10% 只说明 native 的总跨模式差不超过 "
            "10%，不是“启动/收尾 <=1%”的证明，也不是 sluice/TP 的精度；"
            "C2 同时改变时间基准，差值不能单独识别启动成本。sluice/TP "
            "的跨模式差按 MODE_OR_TIMING_EFFECT_NOT_IDENTIFIED 处理，其 "
            "per-op 值携带同量级的跨模式不确定度。")
    elif per_engine:
        verdict["verdict"] = "LIFECYCLE_ONLY_NO_STEADY_STATE_EXTRAPOLATION"
        verdict["reason"] = ("native A2/C2 difference exceeds the "
                             "pre-registered 10% gate")
    else:
        verdict["verdict"] = "LIFECYCLE_ONLY_NO_STEADY_STATE_EXTRAPOLATION"
        verdict["reason"] = ("no C2 fixed-count cross-check in this "
                             "session; Option B is lifecycle-only")
    return verdict


def _mean_per_op(cells: dict, cell: str, engine: str, mode: str):
    group = cells.get((cell, engine))
    if not group or mode not in group.get("modes", {}):
        return None
    values = []
    for run in group["modes"][mode]:
        row = per_op_row(run, "process_lifecycle")
        if row["status"] != "ok":
            continue
        v = row["values"].get("PROCESS_LIFECYCLE_INSTRUCTIONS_per_op", {})
        if v.get("class") == "counted":
            values.append(v["value"])
    return sum(values) / len(values) if values else None


def _count_ok(cells: dict, cell: str, engine: str, mode: str) -> int:
    group = cells.get((cell, engine))
    if not group or mode not in group.get("modes", {}):
        return 0
    return sum(1 for run in group["modes"][mode]
               if per_op_row(run, "process_lifecycle")["status"] == "ok")


def _ranking(normalized: dict) -> list:
    """Ranking across ASYNC engines per cell, clean-mode IOPS only.
    F3-class cells and non-clean modes never enter; psync is a synchronous
    reference (D1-capped), never ranked (COST-ATTRIBUTION-METHOD §2)."""
    table = []
    for key, entry in normalized.items():
        if entry["cell"].startswith(RANKED_EXCLUDED_PREFIXES):
            continue
        if entry["engine"] == "psync":
            continue
        clean = entry.get("clean") or {}
        vals = clean.get("iops_values") or []
        if not vals:
            continue
        table.append({"cell": entry["cell"], "engine": entry["engine"],
                      "mean_iops": round(sum(vals) / len(vals), 1),
                      "observed_spread_pct": clean.get(
                          "observed_spread_pct"),
                      "spread_class": clean.get("spread_class")})
    return table


def _comparisons(ranking: list) -> dict:
    """Pre-registered A/A rule: if a cell's observed spread exceeds 1/3 of
    the engine-to-native IOPS gap, that cell's clean-IOPS ranking is
    INCONCLUSIVE (threshold frozen before measurement; not adjustable)."""
    by_cell: dict = {}
    for row in ranking:
        by_cell.setdefault(row["cell"], []).append(row)
    out = {}
    for cell, rows in sorted(by_cell.items()):
        native = next((r for r in rows if r["engine"] == "io_uring"), None)
        if native is None or not native["mean_iops"]:
            continue
        for row in rows:
            if row["engine"] == "io_uring":
                continue
            gap_pct = abs(row["mean_iops"] - native["mean_iops"]) \
                / native["mean_iops"] * 100
            spread = row.get("observed_spread_pct")
            third = gap_pct / 3.0
            if spread is None:
                cls = "INCONCLUSIVE_NO_SPREAD"
            elif spread > third:
                cls = "INCONCLUSIVE_AA_SPREAD_RULE"
            else:
                cls = "OK"
            out["%s/%s" % (cell, row["engine"])] = {
                "gap_vs_native_pct": round(gap_pct, 3),
                "spread_pct": spread,
                "spread_rule_threshold_pct": round(third, 3),
                "class": cls,
            }
    return out

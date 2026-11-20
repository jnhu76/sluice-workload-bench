#!/usr/bin/env python3
"""Serial userspace-counter session for sluice-uring on U1-U5.

Session: results/userspace-cost/session-002-counters.

The session is a thin scheduler over attribution.runner.Session:

  * cells U1-U5 are the workload cells frozen by
    results/userspace-cost/session-001-baseline/PROTOCOL.md
    (A1/A4/A2/A5/A3, time_based runtime=10s ramp_time=0);
  * each cell runs release r1 -> research x1 -> research x2 -> release r2
    (ABBA), so the negative control and the instrumented arm see the same
    file/cache state one cell at a time;
  * research runs append the adapter/core counters to
    <run>/counters.jsonl, wrapped by `perf stat -e instructions:u,
    task-clock` for the perturbation arm;
  * each cell also runs one trace-bpf run (bpftrace enter/submit/cqe/punt/
    futex maps) alongside a research fio job; its IOPS is never used for
    the perturbation calculation.

The scan-length histogram (`scan_hist`, schema /2) is an additive
research-only counter added for this session; all schema /1 fields keep
their documented meaning.  Core counters come from the research SDK, so
the scan numbers are runtime counts, not `tickets_taken x capacity`
INFERRED estimates.

The sudo password is read from an environment variable, popped from the
environment immediately, and only ever written to `sudo -S` stdin.  It is
never stored in argv, evidence files, or git.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import statistics
import subprocess
import sys
import time

TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

from attribution import parse_bpftrace  # noqa: E402
from attribution import runner as R  # noqa: E402
from attribution.baseline_session import (  # noqa: E402
    SessionAbort,
    attach_evidence,
    check,
    domain_from,
    sample_env,
)
from attribution.parse_perf import parse_perf_stat_csv  # noqa: E402

SESSION_ID = "session-002-counters"
CELL_U = {"A1": "U1", "A4": "U2", "A2": "U3", "A5": "U4", "A3": "U5"}
CELL_ORDER = ("A1", "A4", "A2", "A5", "A3")
ARMS = ("release", "research")
RELEASE_SO = R.ENGINES_DIR / "fio-sluice-ring.so"
RESEARCH_SO = R.ENGINES_DIR / "fio-sluice-ring-research.so"
PERF_EVENTS = ("instructions:u", "task-clock")
RUN_ORDER = (("release", 1), ("research", 1), ("research", 2),
             ("release", 2))
TRACE_WINDOW_EXTRA_S = 4


# -- identity ---------------------------------------------------------------

def _read_build_id(path: pathlib.Path) -> str | None:
    proc = subprocess.run(["readelf", "-n", str(path)],
                          capture_output=True, text=True)
    for line in proc.stdout.splitlines():
        if "Build ID:" in line:
            return line.split("Build ID:", 1)[1].strip()
    return None


def _text_identity(path: pathlib.Path) -> dict:
    tmp = pathlib.Path("/tmp") / ("sluice-text-" + str(os.getpid()) + ".bin")
    proc = subprocess.run(
        ["objcopy", "-O", "binary", "--only-section=.text", str(path),
         str(tmp)], capture_output=True)
    if proc.returncode != 0 or not tmp.exists():
        return {"size": None, "sha256": None}
    out = {"size": tmp.stat().st_size, "sha256": R.sha256_file(tmp)}
    tmp.unlink(missing_ok=True)
    return out


def binary_identity(path: pathlib.Path) -> dict:
    return {
        "path": str(path),
        "sha256": R.sha256_file(path),
        "build_id": _read_build_id(path),
        "text": _text_identity(path),
        "stripped": R.is_stripped(path),
        "has_research_counters_string": bool(
            subprocess.run(["strings", str(path)], capture_output=True,
                           text=True).stdout.find("RESEARCH_COUNTERS") >= 0),
    }


# -- protocol ---------------------------------------------------------------

def write_protocol(root: pathlib.Path, argv: list, session: R.Session,
                   identities: dict) -> None:
    path = root / "PROTOCOL.md"
    if path.exists():
        return
    lines = [
        "# userspace counters session-002 (frozen before measured runs)",
        "",
        "- session: `%s`" % SESSION_ID,
        "- generated: %s" % time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "- driver: `tools/attribution/counters_session.py`",
        "- argv: `%s`" % " ".join(argv),
        "- sluice_revision: `%s`" % session.revision,
        "- host: %s" % os.uname().nodename,
        "- kernel: %s" % os.uname().release,
        "- fio: `%s` (%s)" % (session.fio, R.FIO_VERSION),
        "- cpu_set: `%s`" % session.cpu_set,
        "- runtime / ramp / file: %ss / 0s / %s"
        % (session.runtime_s, session.file_size),
        "",
        "## 1. Cells (U1-U5 follow session-001-baseline, frozen)",
        "",
        "These U labels are workload cells, not the counter-ratio U1-U5 of",
        "`docs/fio-integration/userspace/EXPERIMENT-PROTOCOL.md` §1.",
        "",
        "| U | cell | rw | bs | iodepth | window |",
        "|---|---|---|---|---|---|",
    ]
    for cell in CELL_ORDER:
        spec = R.CELLS[cell]
        lines.append("| %s | %s | %s | %s | %d | time_based runtime=%ss "
                     "ramp_time=0 |"
                     % (CELL_U[cell], cell, spec["rw"], spec["bs"],
                        spec["iodepth"], session.runtime_s))
    lines += [
        "",
        "Fixed fio options: `numjobs=1 thread=1 direct=0 invalidate=0",
        "randseed=81 group_reporting=1 buffer_pattern=0x5a size=%s`."
        % session.file_size,
        "",
        "## 2. Builds and arms (frozen)",
        "",
        "- release arm: `%s`" % identities["release"]["path"],
        "  sha256 `%s`, build id `%s`, .text %s bytes / `%s`"
        % (identities["release"]["sha256"],
           identities["release"]["build_id"],
           identities["release"]["text"]["size"],
           identities["release"]["text"]["sha256"]),
        "- research arm: `%s`" % identities["research"]["path"],
        "  sha256 `%s`, build id `%s`, .text %s bytes / `%s`"
        % (identities["research"]["sha256"],
           identities["research"]["build_id"],
           identities["research"]["text"]["size"],
           identities["research"]["text"]["sha256"]),
        "- the release `.so` is the negative control; the research `.so`",
        "  is only used for counting and perturbation, never as release",
        "  performance.",
        "",
        "Instrumentation extension for this session (research build only):",
        "`scan_hist[0..64]` counts harvest() calls by the number of slots",
        "examined (`examined > 64` saturates at 64).  The JSON schema is",
        "`sluice-fio-research/2`; every schema `/1` field is unchanged and",
        "`scan_hist` is additive.  The research SDK core counters are the",
        "ones documented in INSTRUMENTATION.md §1.2; they are available,",
        "so core scan numbers are runtime counts and are NOT the",
        "`tickets_taken x capacity` INFERRED bound.",
        "",
        "## 3. Order and windows (frozen)",
        "",
        "Per cell, serially and without interleaving other fio jobs:",
        "",
        "```text",
        "release r1 -> research x1 -> research x2 -> release r2 -> trace-bpf t1",
        "```",
        "",
        "- release/research runs: `time_based=1 runtime=%ss ramp_time=0`,"
        % session.runtime_s,
        "  wrapped by `taskset -c %s` and" % session.cpu_set,
        "  `perf stat -x, -e instructions:u,task-clock`; research runs also",
        "  set `SLUICE_FIO_RESEARCH_COUNTERS_FILE=<run>/counters.jsonl`.",
        "- trace-bpf run: the same research job is executed while a root",
        "  `bpftrace` counts `io_uring_enter`, `io_uring_submit_req`,",
        "  `io_uring_complete`, `io_uring_queue_async_work`, and",
        "  `sys_enter_futex`; the bpftrace window is runtime+%ss."
        % TRACE_WINDOW_EXTRA_S,
        "- sleep 1 s between runs; the cell file is re-written and",
        "  pre-warmed before the cell's first run.",
        "",
        "## 4. Denominators and labels (frozen)",
        "",
        "- op = fio `read.total_ios`; must equal",
        "  `staged_completions` for every research run;",
        "- instr/op = perf `instructions:u` / `read.total_ios`; events with",
        "  time-running < 100%% are SAMPLED and never used as exact;",
        "- counters: COUNTED runtime adapter/core counts;",
        "- bpftrace maps: HOST_ISOLATED_AGGREGATE comm-keyed counts; a run",
        "  with a `Lost N events` receipt is NON_EXACT and its counts are",
        "  lower bounds only;",
        "- two reps are an observed range, not a confidence interval.",
        "",
        "## 5. Stop rules (frozen)",
        "",
        "The EXPERIMENT-PROTOCOL.md §7 tripwires and invariants apply.",
        "In addition, a research run is INVALID if `scan_hist` does not sum",
        "to `harvest_calls` or its weighted sum does not equal",
        "`harvest_slots_examined`; this run's counters are then not used.",
        "`take_empty != 0`, `max_staged > iodepth`, or",
        "`staged_completions != fio.read.total_ios` invalidate the run.",
        "",
        "## 6. Evidence layout",
        "",
        "```text",
        "results/userspace-cost/%s/" % SESSION_ID,
        "  PROTOCOL.md  manifest.json  binary-manifest.json",
        "  environment-start.json  environment-domain.json  environment-end.json",
        "  runs/<cell>__sluice-uring__<arm>__rep<N>/",
        "    fio-job.fio fio-result.json meta.json stderr.txt stdout.txt",
        "    perf-stat.raw.txt counters.jsonl counters.json denominator.json",
        "  runs/<cell>__sluice-uring__trace-bpf__rep1/",
        "    bpf-counts.bt bpftrace.stdout.txt bpftrace.stderr.txt",
        "    bpftrace-counts.json counters.jsonl counters.json",
        "  logs/counts-summary.txt perturbation-summary.txt trace-summary.txt",
        "  logs/invariants.txt build-identity.txt instrumentation-extension.txt",
        "  sha256-manifest.txt",
        "```",
        "",
        "## 7. Uncontrolled items (declared)",
        "",
        "- governor left at the machine default; turbo not pinned;",
        "- `direct=0` warm page cache, single SATA SSD on btrfs;",
        "- research-build atomics perturb execution; only the ABBA",
        "  release/research contrast quantifies that perturbation;",
        "- bpftrace adds observer overhead to the trace run; that run's",
        "  IOPS is reported only as a trace-window fact, never as U5;",
        "- perf `instructions:u` is the fio task's user context only.",
        "",
    ]
    path.write_text("\n".join(lines) + "\n")


# -- jobs -------------------------------------------------------------------

def job_lines(so_path: pathlib.Path, data: pathlib.Path, cell: str,
              runtime_s: int, file_size: str) -> str:
    spec = R.CELLS[cell]
    lines = [
        "[job]",
        "ioengine=external:%s" % so_path,
        "filename=%s" % data,
        "sluice_backend=uring",
        "rw=%s" % spec["rw"],
        "bs=%s" % spec["bs"],
        "size=%s" % file_size,
        "iodepth=%d" % spec["iodepth"],
        "numjobs=1",
        "direct=0",
        "thread=1",
        "group_reporting=1",
        "randseed=%d" % R.RANDSEED,
        "invalidate=0",
        "buffer_pattern=0x5a",
        "time_based=1",
        "runtime=%d" % runtime_s,
        "ramp_time=0",
    ]
    return "\n".join(lines) + "\n"


def run_id(cell: str, arm: str, rep: int) -> str:
    return "%s__sluice-uring__%s__rep%d" % (cell, arm, rep)


# -- counters ---------------------------------------------------------------

COUNTER_FIELDS = (
    "getevents_calls", "harvest_calls", "harvest_slots_examined",
    "take_calls", "take_pending", "take_ready", "take_empty",
    "staged_completions", "max_staged", "submit_read_calls",
    "submit_write_calls", "submit_sync_calls", "submit_rejected_calls",
)
CORE_FIELDS = (
    "scope_resolve_calls", "scope_resolve_slot_iterations",
    "scope_take_calls", "bridge_consume_calls", "core_mutex_lock_calls",
)


def _counter_line(path: pathlib.Path) -> str | None:
    try:
        for line in path.read_text().splitlines():
            if line.startswith("RESEARCH_COUNTERS "):
                return line
    except OSError:
        return None
    return None


def invariants(c: dict, total_ios: int | None, iodepth: int) -> dict:
    core = c.get("core") or {}
    hist = c.get("scan_hist") or []
    checks = {
        "take_split": c["take_calls"] == (c["take_pending"] + c["take_ready"]
                                          + c["take_empty"]),
        "scope_resolve_eq_take": (core.get("scope_resolve_calls")
                                  == c["take_calls"]),
        "scope_take_eq_take": (core.get("scope_take_calls")
                               == c["take_calls"]),
        "bridge_consume_eq_take_empty0": (
            c["take_empty"] != 0
            or core.get("bridge_consume_calls") == c["take_calls"]),
        "staged_eq_take_ready": c["staged_completions"] == c["take_ready"],
        "read_submit_eq_staged": (c["submit_read_calls"]
                                  == c["staged_completions"]),
        "staged_eq_fio_total": (total_ios is not None
                                and c["staged_completions"] == total_ios),
        "max_staged_le_capacity": c["max_staged"] <= iodepth,
        "take_empty_zero": c["take_empty"] == 0,
        "scan_hist_sum_eq_harvest": (sum(hist) == c["harvest_calls"]
                                     if hist else False),
        "scan_hist_weighted_eq_slots": (
            sum(i * v for i, v in enumerate(hist))
            == c["harvest_slots_examined"] if hist else False),
    }
    return checks


def attach_counters(session: R.Session, rec: dict, cell: str) -> dict:
    run_dir = session.accepted_run_dir(rec)
    counters_path = run_dir / "counters.jsonl"
    stderr_path = run_dir / "stderr.txt"
    line = _counter_line(counters_path)
    stderr_line = None
    if stderr_path.exists():
        for text in stderr_path.read_text().splitlines():
            if text.startswith("RESEARCH_COUNTERS "):
                if stderr_line is not None:
                    raise SessionAbort(
                        "S4: multiple RESEARCH_COUNTERS lines in %s"
                        % stderr_path)
                stderr_line = text
    if line is None:
        raise SessionAbort("S4: counters file missing/empty in %s" % run_dir)
    if stderr_line is None:
        raise SessionAbort("S4: RESEARCH_COUNTERS absent from stderr in %s"
                           % run_dir)
    if line != stderr_line:
        raise SessionAbort("S4: stderr and counters file differ in %s"
                           % run_dir)
    c = json.loads(line.split(" ", 1)[1])
    fio_doc = json.loads((run_dir / "fio-result.json").read_text())
    job = fio_doc["jobs"][0]
    total = (job.get("read") or {}).get("total_ios")
    iodepth = int(c.get("iodepth") or R.CELLS[cell]["iodepth"])
    checks = invariants(c, total, iodepth)
    doc = {
        "schema": c.get("schema"),
        "run_id": rec["run_id"],
        "cell": cell,
        "u_label": CELL_U.get(cell),
        "raw_line_file": "counters.jsonl",
        "stderr_matches_file": True,
        "total_ios": total,
        "iodepth": iodepth,
        "counters": c,
        "invariants": checks,
        "all_invariants_pass": all(checks.values()),
    }
    (run_dir / "counters.json").write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n")
    if not all(checks.values()):
        raise SessionAbort("S5: counter invariant failed in %s: %s"
                           % (run_dir, [k for k, v in checks.items()
                                        if not v]))
    return doc


# -- environment ------------------------------------------------------------

def gate_wait(env: dict, domain: dict, where: str,
              timeout_s: int = 180) -> tuple[dict, list]:
    """Like baseline gate(), but a loadavg-only excursion waits instead of
    aborting: our own previous run may still be decaying."""
    problems = check(env, domain)
    if not problems:
        return env, []
    if not all(p.startswith("loadavg") for p in problems):
        raise SessionAbort("environment gate failed at %s: %s"
                           % (where, "; ".join(problems)))
    waited = 0
    settled = env
    while waited < timeout_s:
        time.sleep(10)
        waited += 10
        settled = sample_env()
        problems = check(settled, domain)
        if not problems:
            return settled, ["loadavg pause %ss" % waited]
    raise SessionAbort("loadavg did not settle after %ss at %s: %s"
                       % (waited, where, check(settled, domain)))


# -- run loops --------------------------------------------------------------

def common_extra(session: R.Session, cell: str, arm: str, rep: int,
                 so_path: pathlib.Path, fio_sha: str | None) -> dict:
    return {
        "cell": cell,
        "u_label": CELL_U[cell],
        "cell_spec": R.CELLS[cell],
        "arm": arm,
        "rep": rep,
        "engine_sha256": R.sha256_file(so_path),
        "fio_sha256": fio_sha,
        "sluice_revision": session.revision,
    }


def accepted(session: R.Session, rid: str) -> bool:
    try:
        meta = json.loads((session.runs_dir / rid / "meta.json").read_text())
    except (OSError, ValueError):
        return False
    return meta.get("status") == "ok" and not meta.get("discarded")


def after_run_gate(session: R.Session, rec: dict, domain: dict) -> None:
    after = sample_env()
    settled, notes = gate_wait(after, domain, rec["run_id"] + " after")
    if notes:
        run_dir = session.accepted_run_dir(rec)
        (run_dir / "env-after-settled.json").write_text(
            json.dumps(settled, indent=2, sort_keys=True) + "\n")
        (run_dir / "env-pause.json").write_text(
            json.dumps({"run_id": rec["run_id"], "notes": notes},
                       indent=2, sort_keys=True) + "\n")
        print("pause %s after %s (data kept)" % (notes, rec["run_id"]),
              flush=True)


def measured_run(session: R.Session, cell: str, arm: str, rep: int,
                 data: pathlib.Path, domain: dict,
                 fio_sha: str | None) -> dict | None:
    rid = run_id(cell, arm, rep)
    if getattr(session, "skip_existing", False) and accepted(session, rid):
        print("skip %-48s (accepted)" % rid, flush=True)
        return None
    run_dir = session.runs_dir / rid
    so_path = RELEASE_SO if arm == "release" else RESEARCH_SO
    job = job_lines(so_path, data, cell, session.runtime_s,
                    session.file_size)
    perf_path = run_dir / "perf-stat.raw.txt"
    wrap = ["perf", "stat", "-x,", "-o", str(perf_path),
            "-e", ",".join(PERF_EVENTS)]
    if arm == "research":
        counters_path = run_dir / "counters.jsonl"
        wrap = ["env",
                "SLUICE_FIO_RESEARCH_COUNTERS_FILE=%s" % counters_path] + wrap
    before = sample_env()
    before, notes = gate_wait(before, domain, rid + " before")
    extra = common_extra(session, cell, arm, rep, so_path, fio_sha)
    rec = session.run_with_retry(
        rid, job_text=job, wrap=wrap, expect_backend="uring", extra=extra)
    if rec["status"] != "ok":
        raise SessionAbort("run %s failed: %s"
                           % (rec["run_id"], rec.get("failures")))
    attach_evidence(session, rec, cell, arm, "release" if arm == "release"
                    else "research", before, sample_env(), extra)
    if arm == "research":
        attach_counters(session, rec, cell)
    after_run_gate(session, rec, domain)
    print("ok %-48s wall=%ss" % (rec["run_id"], rec.get("wall_s")),
          flush=True)
    time.sleep(1)
    return rec


def trace_run(session: R.Session, cell: str, data: pathlib.Path,
              domain: dict, fio_sha: str | None) -> dict | None:
    rid = "%s__sluice-uring__trace-bpf__rep1" % cell
    if getattr(session, "skip_existing", False) and accepted(session, rid):
        print("skip %-48s (accepted)" % rid, flush=True)
        return None
    run_dir = session.runs_dir / rid
    run_dir.mkdir(parents=True, exist_ok=True)
    window_s = session.runtime_s + TRACE_WINDOW_EXTRA_S
    before = sample_env()
    before, _ = gate_wait(before, domain, rid + " before")
    script = run_dir / "bpf-counts.bt"
    script.write_text(session.bpf_script(window_s))
    out = open(run_dir / "bpftrace.stdout.txt", "w")
    err = open(run_dir / "bpftrace.stderr.txt", "w")
    proc = subprocess.Popen(
        session._sudo(["timeout", "-s", "INT", str(window_s),
                       "bpftrace", str(script)]),
        stdin=subprocess.PIPE, stdout=out, stderr=err, text=True)
    if session.sudo_pw:
        try:
            proc.stdin.write(session.sudo_pw + "\n")
            proc.stdin.flush()
        except BrokenPipeError:
            proc.terminate()
            proc.wait(timeout=10)
            out.close()
            err.close()
            raise R.EnvironmentDrift("bpftrace exited before sudo password")
    proc.stdin.close()
    attach = session._wait_for_attach(run_dir / "bpftrace.stderr.txt",
                                      proc, timeout=10.0)
    if not attach:
        proc.terminate()
        proc.wait(timeout=10)
        out.close()
        err.close()
        (run_dir / "trace-status.json").write_text(json.dumps(
            {"status": "UNAVAILABLE", "reason": "no attach receipt"},
            indent=2) + "\n")
        print("trace-bpf unavailable on %s (no attach receipt)" % cell,
              flush=True)
        return None
    counters_path = run_dir / "counters.jsonl"
    job = job_lines(RESEARCH_SO, data, cell, session.runtime_s,
                    session.file_size)
    extra = common_extra(session, cell, "trace-bpf", 1, RESEARCH_SO, fio_sha)
    extra["bpf_script"] = script.name
    extra["bpf_attach"] = attach
    extra["bpf_window_s"] = window_s
    rec = session.run_with_retry(
        rid, job_text=job,
        wrap=["env", "SLUICE_FIO_RESEARCH_COUNTERS_FILE=%s" % counters_path],
        own_bpf=True, expect_backend="uring", extra=extra)
    try:
        proc.wait(timeout=window_s + 30)
    except subprocess.TimeoutExpired:
        proc.terminate()
        proc.wait(timeout=10)
    out.close()
    err.close()
    if rec["status"] != "ok":
        raise SessionAbort("trace run %s failed: %s"
                           % (rec["run_id"], rec.get("failures")))
    accepted_dir = session.accepted_run_dir(rec)
    if accepted_dir != run_dir:
        source = session._bpf_source_dir(run_dir)
        for name in ("bpf-counts.bt", "bpftrace.stdout.txt",
                     "bpftrace.stderr.txt"):
            src = source / name
            if src.exists():
                shutil.copy2(src, accepted_dir / name)
    counters = attach_counters(session, rec, cell)
    fio_doc = json.loads((accepted_dir / "fio-result.json").read_text())
    total = (fio_doc["jobs"][0].get("read") or {}).get("total_ios")
    maps = parse_bpftrace.parse_maps(
        (accepted_dir / "bpftrace.stdout.txt").read_text())
    stderr = (accepted_dir / "bpftrace.stderr.txt").read_text()
    lost = parse_bpftrace.detect_lost(stderr)
    summary = parse_bpftrace.summarize(maps, total, comm="fio")
    label = _trace_label(lost)
    doc = {
        "schema": "userspace-trace-bpf/1",
        "run_id": rec["run_id"],
        "cell": cell,
        "u_label": CELL_U[cell],
        "window_s": window_s,
        "attach": attach,
        "bpftrace_returncode": proc.returncode,
        "lost_events": lost,
        "counts_exact": lost == 0,
        "label": label,
        "ios": total,
        "summary": summary,
        "counters": counters,
        "raw_maps": {k: {"|".join(key): v for key, v in bucket.items()}
                     for k, bucket in maps.items()},
    }
    (accepted_dir / "bpftrace-counts.json").write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n")
    meta_path = accepted_dir / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["bpftrace_returncode"] = proc.returncode
    meta["bpf_lost_events"] = lost
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    after_run_gate(session, rec, domain)
    print("ok %-48s lost=%s" % (rec["run_id"], lost), flush=True)
    time.sleep(1)
    return rec


# -- analysis ---------------------------------------------------------------

def _trace_label(lost: int | None) -> str:
    if lost is None:
        return "NO_LOSS_RECEIPT (not certified exact)"
    if lost == 0:
        return "COUNTED (bpftrace reported zero lost events)"
    return "LOST_EVENTS (lower bound, %d events)" % lost


def _fio(run_dir: pathlib.Path) -> dict:
    doc = json.loads((run_dir / "fio-result.json").read_text())
    job = doc["jobs"][0]
    read = job.get("read") or {}
    return {
        "total_ios": read.get("total_ios"),
        "io_bytes": read.get("io_bytes"),
        "iops": read.get("iops"),
        "runtime_ms": read.get("runtime"),
        "usr_cpu_pct": job.get("usr_cpu"),
        "sys_cpu_pct": job.get("sys_cpu"),
        "iodepth_level": job.get("iodepth_level"),
    }


def _perf(run_dir: pathlib.Path) -> dict:
    path = run_dir / "perf-stat.raw.txt"
    if not path.exists():
        return {}
    events = parse_perf_stat_csv(path.read_text())
    instr = events.get("instructions:u") or {}
    task = events.get("task-clock") or events.get("task-clock:u") or {}
    return {
        "instructions_user": instr.get("value"),
        "instructions_kind": instr.get("kind"),
        "instructions_time_running_pct": instr.get("time_running_pct"),
        "task_clock_ms": task.get("value"),
        "task_clock_kind": task.get("kind"),
    }


def _scan_stats(c: dict) -> dict:
    hist = c.get("scan_hist") or []
    total = sum(hist)
    if not total:
        return {}
    def pct(q: float) -> int | None:
        target = q * total
        cum = 0
        for i, v in enumerate(hist):
            cum += v
            if cum >= target:
                return i
        return len(hist) - 1
    return {
        "mean_slots_per_harvest": round(
            c["harvest_slots_examined"] / c["harvest_calls"], 4),
        "p50_slots": pct(0.50),
        "p90_slots": pct(0.90),
        "p99_slots": pct(0.99),
        "max_slots": max(i for i, v in enumerate(hist) if v),
        "histogram": hist,
        "histogram_sum": total,
    }


def _research_metrics(run_dir: pathlib.Path) -> dict:
    doc = json.loads((run_dir / "counters.json").read_text())
    c = doc["counters"]
    core = c.get("core") or {}
    fio = _fio(run_dir)
    perf = _perf(run_dir)
    op = fio["total_ios"]
    if not op:
        raise SessionAbort("no total_ios in %s" % run_dir)
    take = c["take_calls"]
    residual = round(core["core_mutex_lock_calls"] / op
                     - (9.0 + take / op), 6)
    residual_limit = 0.10 * (9.0 + take / op)
    u3_2_label = ("UNEXPLAINED" if abs(residual) > residual_limit
                  else "CONSISTENT_WITH_9_PLUS_T")
    out = {
        "run_id": run_dir.name,
        "cell": doc["cell"],
        "u_label": doc["u_label"],
        "total_ios": op,
        "iops": fio["iops"],
        "getevents_per_op": round(c["getevents_calls"] / op, 6),
        "harvest_per_op": round(c["harvest_calls"] / op, 6),
        "inspected_slots_per_op": round(
            c["harvest_slots_examined"] / op, 6),
        "pending_tickets_per_op": round(c["take_pending"] / op, 6),
        "take_calls_per_op": round(take / op, 6),
        "take_ready": c["take_ready"],
        "take_pending": c["take_pending"],
        "take_empty": c["take_empty"],
        "take_ready_frac": round(c["take_ready"] / take, 6),
        "take_pending_frac": round(c["take_pending"] / take, 6),
        "take_empty_frac": round(c["take_empty"] / take, 6),
        "completions_per_harvest": round(
            c["staged_completions"] / c["harvest_calls"], 6),
        "scan": _scan_stats(c),
        "submit_read_per_op": round(c["submit_read_calls"] / op, 6),
        "submit_write_per_op": round(c["submit_write_calls"] / op, 6),
        "submit_sync_per_op": round(c["submit_sync_calls"] / op, 6),
        "submit_rejected_per_op": round(
            c["submit_rejected_calls"] / op, 6),
        "max_staged": c["max_staged"],
        "max_staged_over_capacity": round(
            c["max_staged"] / max(1, c["iodepth"]), 6),
        "core": {
            "scope_resolve_calls_per_op": round(
                core["scope_resolve_calls"] / op, 6),
            "scope_resolve_slot_iterations_per_op": round(
                core["scope_resolve_slot_iterations"] / op, 6),
            "scope_resolve_slot_iterations_per_take": round(
                core["scope_resolve_slot_iterations"] / take, 6),
            "bridge_consume_per_take": round(
                core["bridge_consume_calls"] / take, 6),
            "core_mutex_lock_calls_per_op": round(
                core["core_mutex_lock_calls"] / op, 6),
            "u3_2_residual_lock_minus_9_plus_take_per_op": round(
                core["core_mutex_lock_calls"] / op
                - (9.0 + take / op), 6),
            "u3_2_residual_limit_per_op": round(residual_limit, 6),
            "u3_2_label": u3_2_label,
            "u3_3_literal_ratio": round(
                core["core_mutex_lock_calls"] / (9.0 + take), 6),
        },
        "instr_user": perf.get("instructions_user"),
        "instr_per_io": (round(perf["instructions_user"] / op, 4)
                         if perf.get("instructions_user") else None),
        "perf": perf,
        "invariants": doc["invariants"],
        "all_invariants_pass": doc["all_invariants_pass"],
    }
    return out


def _release_metrics(run_dir: pathlib.Path) -> dict:
    fio = _fio(run_dir)
    perf = _perf(run_dir)
    op = fio["total_ios"]
    return {
        "run_id": run_dir.name,
        "total_ios": op,
        "io_bytes": fio["io_bytes"],
        "iops": fio["iops"],
        "instr_user": perf.get("instructions_user"),
        "instr_per_io": (round(perf["instructions_user"] / op, 4)
                         if perf.get("instructions_user") and op else None),
        "perf": perf,
    }


def _mean(values: list) -> float | None:
    vals = [v for v in values if v is not None]
    return round(statistics.fmean(vals), 6) if vals else None


def _range_pct(values: list) -> float | None:
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    mean = statistics.fmean(vals)
    return round((max(vals) - min(vals)) / mean * 100.0, 4) if mean else None


def analyse(session: R.Session) -> dict:
    cells: dict = {}
    failures: list = []
    for cell in CELL_ORDER:
        cell_doc = {"u_label": CELL_U[cell], "cell_spec": R.CELLS[cell],
                    "release": [], "research": [], "trace": None}
        for arm, rep in RUN_ORDER:
            rid = run_id(cell, arm, rep)
            run_dir = session.runs_dir / rid
            if not accepted(session, rid):
                failures.append("missing accepted run %s" % rid)
                continue
            if arm == "release":
                cell_doc["release"].append(_release_metrics(run_dir))
            else:
                cell_doc["research"].append(_research_metrics(run_dir))
        trace_dir = session.runs_dir / (
            "%s__sluice-uring__trace-bpf__rep1" % cell)
        trace_doc = trace_dir / "bpftrace-counts.json"
        if trace_doc.exists():
            trace = json.loads(trace_doc.read_text())
            # The label is derived from the loss receipt; refresh it so a
            # re-analysis of old receipts carries the current definition.
            trace["label"] = _trace_label(trace.get("lost_events"))
            trace["counts_exact"] = trace.get("lost_events") == 0
            trace_doc.write_text(
                json.dumps(trace, indent=2, sort_keys=True) + "\n")
            cell_doc["trace"] = trace
        cells[cell] = cell_doc

    table = {}
    for cell, doc in cells.items():
        rel = doc["release"]
        res = doc["research"]
        rel_iops = [r["iops"] for r in rel]
        res_iops = [r["iops"] for r in res]
        rel_instr = [r["instr_per_io"] for r in rel]
        res_instr = [r["instr_per_io"] for r in res]
        rel_mean_iops = _mean(rel_iops)
        res_mean_iops = _mean(res_iops)
        rel_mean_instr = _mean(rel_instr)
        res_mean_instr = _mean(res_instr)
        u5_1 = (round((res_mean_iops - rel_mean_iops) / rel_mean_iops * 100.0,
                      4)
                if rel_mean_iops and res_mean_iops is not None else None)
        u5_2 = (round(res_mean_instr - rel_mean_instr, 4)
                if None not in (rel_mean_instr, res_mean_instr) else None)
        u5_2_pct = (round((res_mean_instr - rel_mean_instr)
                          / rel_mean_instr * 100.0, 4)
                    if rel_mean_instr and res_mean_instr is not None
                    else None)
        table[cell] = {
            "u_label": doc["u_label"],
            "release_iops": rel_iops,
            "research_iops": res_iops,
            "release_iops_mean": rel_mean_iops,
            "research_iops_mean": res_mean_iops,
            "release_instr_per_io": rel_instr,
            "research_instr_per_io": res_instr,
            "release_instr_per_io_mean": rel_mean_instr,
            "research_instr_per_io_mean": res_mean_instr,
            "u5_1_iops_perturbation_pct": u5_1,
            "u5_2_instr_per_io_delta": u5_2,
            "u5_2_instr_per_io_delta_pct": u5_2_pct,
            "aa_range_iops_release_pct": _range_pct(rel_iops),
            "aa_range_iops_research_pct": _range_pct(res_iops),
            "aa_range_instr_release_pct": _range_pct(rel_instr),
            "aa_range_instr_research_pct": _range_pct(res_instr),
        }
        spread = [v for v in (
            table[cell]["aa_range_iops_release_pct"],
            table[cell]["aa_range_iops_research_pct"]) if v is not None]
        if u5_1 is not None and spread:
            table[cell]["u5_1_label"] = (
                "OBSERVED_PERTURBATION"
                if abs(u5_1) > max(spread)
                else "WITHIN_TWO_REPEAT_SPREAD")
        else:
            table[cell]["u5_1_label"] = None
    return {"cells": cells, "table": table, "failures": failures}


def stop_rule_audit(session: R.Session, analysis: dict) -> dict:
    """Evaluate the stop rules against the on-disk receipts, rather than
    asserting them in prose."""
    tripwires = []
    exits = {}
    statuses = {}
    retries = []
    counters_match = True
    for meta_path in sorted((session.root / "runs").glob("*/meta.json")):
        meta = json.loads(meta_path.read_text())
        rid = meta_path.parent.name
        statuses[rid] = meta.get("status")
        exits[rid] = meta.get("fio_exit")
        if meta.get("discarded") or meta.get("status") != "ok":
            retries.append(rid)
        stderr_path = meta_path.parent / "stderr.txt"
        if stderr_path.exists():
            for line in stderr_path.read_text().splitlines():
                if any(frag in line for frag in R.TRIPWIRES):
                    tripwires.append("%s: %s" % (rid, line.strip()))
    for cpath in (session.root / "runs").glob("*/counters.json"):
        doc = json.loads(cpath.read_text())
        counters_match = counters_match and doc.get(
            "stderr_matches_file") is True
    inv_ok = all(m["all_invariants_pass"]
                 for d in analysis["cells"].values()
                 for m in d["research"])
    trace_inv_ok = all(
        d["trace"] is None or d["trace"]["counters"]["all_invariants_pass"]
        for d in analysis["cells"].values())
    aa_max = max((v for t in analysis["table"].values()
                  for v in (t["aa_range_iops_release_pct"],
                            t["aa_range_iops_research_pct"])
                  if v is not None), default=None)
    return {
        "S1_tripwires": {"pass": not tripwires, "hits": tripwires},
        "S2_exit_codes": {
            "pass": all(v == 0 for v in exits.values()),
            "nonzero": {k: v for k, v in exits.items() if v != 0}},
        "S3_semantics_equivalence": {
            "run": False,
            "note": "M-SEM is not part of this counter session; "
                    "session-001-baseline holds the semantics gate"},
        "S4_counters_stderr_match": {
            "pass": counters_match,
            "note": "counters.jsonl line == stderr RESEARCH_COUNTERS line"},
        "S5_invariants": {
            "pass": inv_ok and trace_inv_ok,
            "note": "all research and trace-bpf counter invariants"},
        "S6_max_staged_le_capacity": {
            "pass": all(m["invariants"]["max_staged_le_capacity"]
                        for d in analysis["cells"].values()
                        for m in d["research"])},
        "S7_take_empty_zero": {
            "pass": all(m["invariants"]["take_empty_zero"]
                        for d in analysis["cells"].values()
                        for m in d["research"])},
        "S8_aa_range": {
            "pass": aa_max is not None and aa_max <= 10.0,
            "max_aa_iops_range_pct": aa_max,
            "limit_pct": 10.0,
            "note": "U5 remains valid when every A/A IOPS range <= 10%"},
        "S9_system_exception": {
            "pass": True,
            "note": "no OOM/thermal/background-load abort receipt"},
        "S10_retries": {
            "pass": not retries,
            "discarded_or_failed": retries,
            "failed_run_dirs": sorted(
                p.name for p in (session.root / "runs").glob("*-failed*"))},
        "run_statuses": statuses,
    }


# -- summaries --------------------------------------------------------------

def write_logs(root: pathlib.Path, analysis: dict, identities: dict,
               session: R.Session) -> None:
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    lines = ["U1-U5 userspace counter session (%s)" % SESSION_ID, ""]
    for cell in CELL_ORDER:
        doc = analysis["cells"][cell]
        lines.append("== %s (%s) %s" % (cell, doc["u_label"],
                                        doc["cell_spec"]))
        for m in doc["research"]:
            scan = m["scan"]
            lines += [
                "  research %s: iops=%s total_ios=%s" % (
                    m["run_id"], m["iops"], m["total_ios"]),
                "    getevents/op=%.6f harvest/op=%.6f inspected/op=%.6f"
                % (m["getevents_per_op"], m["harvest_per_op"],
                   m["inspected_slots_per_op"]),
                "    pending tickets/op=%.6f take/op=%.6f"
                " ready/pending/empty=%.6f/%.6f/%.6f"
                % (m["pending_tickets_per_op"], m["take_calls_per_op"],
                   m["take_ready_frac"], m["take_pending_frac"],
                   m["take_empty_frac"]),
                "    completions/harvest=%.6f scan mean/p50/p90/p99/max="
                "%s/%s/%s/%s/%s submit_read/op=%.6f max_staged=%s (%.6f cap)"
                % (m["completions_per_harvest"],
                   scan.get("mean_slots_per_harvest"), scan.get("p50_slots"),
                   scan.get("p90_slots"), scan.get("p99_slots"),
                   scan.get("max_slots"), m["submit_read_per_op"],
                   m["max_staged"], m["max_staged_over_capacity"]),
                "    core: resolve_iters/op=%.6f resolve_iters/take=%.6f"
                " lock/op=%.6f U3.2 residual=%.6f [%s]"
                % (m["core"]["scope_resolve_slot_iterations_per_op"],
                   m["core"]["scope_resolve_slot_iterations_per_take"],
                   m["core"]["core_mutex_lock_calls_per_op"],
                   m["core"]["u3_2_residual_lock_minus_9_plus_take_per_op"],
                   m["core"]["u3_2_label"]),
                "    instr/op=%s invariants=%s"
                % (m["instr_per_io"], m["all_invariants_pass"]),
            ]
        rel = doc["release"]
        if rel:
            lines.append("  release: %s" % ", ".join(
                "%s iops=%s instr/io=%s" % (r["run_id"], r["iops"],
                                           r["instr_per_io"]) for r in rel))
        lines.append("")
    (logs / "counts-summary.txt").write_text("\n".join(lines) + "\n")

    plines = ["U5 perturbation (release vs research, n=2 per arm)", ""]
    for cell, t in analysis["table"].items():
        plines += [
            "== %s (%s)" % (cell, t["u_label"]),
            "  release IOPS: %s mean=%s ; research IOPS: %s mean=%s"
            % (t["release_iops"], t["release_iops_mean"],
               t["research_iops"], t["research_iops_mean"]),
            "  U5.1 IOPS perturbation = %s%% [%s]"
            % (t["u5_1_iops_perturbation_pct"], t["u5_1_label"]),
            "  release instr/IO: %s mean=%s ; research: %s mean=%s"
            % (t["release_instr_per_io"], t["release_instr_per_io_mean"],
               t["research_instr_per_io"],
               t["research_instr_per_io_mean"]),
            "  U5.2 instr/IO delta = %s (%s%%)"
            % (t["u5_2_instr_per_io_delta"],
               t["u5_2_instr_per_io_delta_pct"]),
            "  A/A IOPS range: release=%s%% research=%s%% ;"
            " instr/IO range: release=%s%% research=%s%%"
            % (t["aa_range_iops_release_pct"],
               t["aa_range_iops_research_pct"],
               t["aa_range_instr_release_pct"],
               t["aa_range_instr_research_pct"]),
            "",
        ]
    (logs / "perturbation-summary.txt").write_text("\n".join(plines) + "\n")

    tlines = ["trace-bpf receipts (research build, concurrent with fio)", ""]
    for cell in CELL_ORDER:
        tr = analysis["cells"][cell]["trace"]
        if not tr:
            tlines.append("%s: UNAVAILABLE" % cell)
            continue
        s = tr["summary"]
        per = s.get("per_op") or {}
        counts = s.get("counts") or {}
        tlines += [
            "== %s (%s) lost_events=%s counts_exact=%s ios=%s"
            % (cell, tr["u_label"], tr["lost_events"],
               tr["counts_exact"], tr["ios"]),
            "  label=%s" % tr.get("label"),
            "  enter=%s submit=%s cqe=%s punt=%s futex=%s"
            % (counts.get("enters"), counts.get("submits"),
               counts.get("cqe_events"), counts.get("punts"),
               counts.get("futex")),
            "  per_op enter=%s submit=%s cqe=%s punt=%s futex=%s"
            % (per.get("enters"), per.get("submits"), per.get("cqe_events"),
               per.get("punts"), per.get("futex")),
            "",
        ]
    (logs / "trace-summary.txt").write_text("\n".join(tlines) + "\n")

    ilines = ["counter invariant receipts", ""]
    ok = True
    for cell in CELL_ORDER:
        for m in analysis["cells"][cell]["research"]:
            failed = [k for k, v in m["invariants"].items() if not v]
            ok = ok and not failed
            ilines.append("%s: %s %s" % (
                m["run_id"], "PASS" if not failed else "FAIL",
                "" if not failed else failed))
        tr = analysis["cells"][cell]["trace"]
        if tr:
            failed = [k for k, v in tr["counters"]["invariants"].items()
                      if not v]
            ok = ok and not failed
            ilines.append("%s: %s %s" % (
                tr["run_id"], "PASS" if not failed else "FAIL",
                "" if not failed else failed))
    ilines.append("")
    ilines.append("all_research_invariants_pass=%s" % ok)
    (logs / "invariants.txt").write_text("\n".join(ilines) + "\n")

    blines = ["build identity", ""]
    for arm, ident in identities.items():
        blines += [
            "%s: %s" % (arm, ident["path"]),
            "  sha256=%s" % ident["sha256"],
            "  build_id=%s" % ident["build_id"],
            "  .text size=%s sha256=%s"
            % (ident["text"]["size"], ident["text"]["sha256"]),
            "  stripped=%s research_string=%s"
            % (ident["stripped"], ident["has_research_counters_string"]),
            "",
        ]
    blines += ["sluice_revision=%s" % session.revision,
               "worktree_head=%s" % subprocess.run(
                   ["git", "rev-parse", "HEAD"], cwd=str(R.TOOLS_DIR.parent),
                   capture_output=True, text=True).stdout.strip()]
    (logs / "build-identity.txt").write_text("\n".join(blines) + "\n")

    # Instrumentation diffs promised by PROTOCOL.md §6.  Untracked files
    # (research_counters.hpp, counters_session.py) are rendered with
    # `git diff --no-index /dev/null <file>` so the log is self-contained.
    root = R.TOOLS_DIR.parent
    tracked = ["adapters/fio/src/events.cpp",
               "adapters/fio/src/engine.cpp",
               "adapters/fio/src/queue.cpp",
               "adapters/fio/src/state.hpp",
               "tools/attribution/runner.py",
               "tools/attribution/counters_session.py"]
    parts = []
    for path in tracked:
        proc = subprocess.run(["git", "diff", "HEAD", "--", path],
                              cwd=str(root), capture_output=True, text=True)
        parts.append(proc.stdout)
    for path in ("adapters/fio/src/research_counters.hpp",
                 "tools/attribution/counters_session.py"):
        full = root / path
        if full.exists():
            proc = subprocess.run(
                ["git", "diff", "--no-index", "/dev/null", path],
                cwd=str(root), capture_output=True, text=True)
            parts.append(proc.stdout)
    (logs / "bench-instrumentation-diff.txt").write_text(
        "\n".join(p for p in parts if p) + "\n")

    ext = [
        "scan_hist extension (research build only; release untouched)",
        "",
        "adapters/fio/src/research_counters.hpp:",
    ]
    rc = (root / "adapters/fio/src/research_counters.hpp").read_text()
    ext += ["  " + ln for ln in rc.splitlines()[19:97]]
    ext += ["", "adapters/fio/src/events.cpp:169-170:"]
    ev = (root / "adapters/fio/src/events.cpp").read_text().splitlines()
    ext += ["  " + ln for ln in ev[167:171]]
    ext += [
        "",
        "Semantics: one histogram increment per harvest() call; bucket k =",
        "examined == k; bucket 64 also absorbs examined > 64. Sum(hist) ==",
        "harvest_calls and sum(k*hist[k]) == harvest_slots_examined are",
        "checked as S5 invariants for every research run.",
        "",
        "The research .so also carries the pre-existing adapter/core",
        "counters documented in INSTRUMENTATION.md §1; the release .so",
        "contains no RESEARCH_COUNTERS string.",
    ]
    (logs / "instrumentation-extension.txt").write_text("\n".join(ext) + "\n")


def write_sha_manifest(root: pathlib.Path) -> None:
    lines = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "sha256-manifest.txt":
            lines.append("%s  %s" % (R.sha256_file(path),
                                     path.relative_to(root)))
    (root / "sha256-manifest.txt").write_text("\n".join(lines) + "\n")


# -- main -------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--results-dir",
                    default="results/userspace-cost/%s" % SESSION_ID)
    ap.add_argument("--scratch-dir", default="build/scratch")
    ap.add_argument("--cells", default=",".join(CELL_ORDER))
    ap.add_argument("--runtime", type=int, default=10)
    ap.add_argument("--file-size", default="1G")
    ap.add_argument("--cpu-set", default="0-9")
    ap.add_argument("--fio", default="fio")
    ap.add_argument("--sudo-password-env", default="SLUICE_SUDO_PW")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--skip-trace", action="store_true")
    ap.add_argument("--analyze-only", action="store_true",
                    help="recompute analysis/logs/manifest from accepted raw "
                         "runs without executing fio")
    args = ap.parse_args()

    cells = [c.strip() for c in args.cells.split(",") if c.strip()]
    for cell in cells:
        if cell not in R.CELLS:
            ap.error("unknown cell %s" % cell)

    root = pathlib.Path(__file__).resolve().parents[2]
    results = root / args.results_dir
    scratch = root / args.scratch_dir
    sudo_pw = os.environ.get(args.sudo_password_env)
    if sudo_pw:
        os.environ.pop(args.sudo_password_env, None)
    if not args.skip_trace and not args.analyze_only and not sudo_pw:
        print("trace-bpf requires --sudo-password-env to be set "
              "(or pass --skip-trace)")
        return 2

    session = R.Session(
        results, scratch, cpu_set=args.cpu_set, runtime_s=args.runtime,
        ramp_s=0, file_size=args.file_size, fio=args.fio,
        sudo_pw=sudo_pw)
    session.ensure_layout()
    session.skip_existing = bool(args.resume)
    (root / "logs").mkdir(parents=True, exist_ok=True)

    identities = {
        "release": binary_identity(RELEASE_SO),
        "research": binary_identity(RESEARCH_SO),
    }
    write_protocol(session.root, sys.argv[1:], session, identities)
    if not identities["research"]["has_research_counters_string"]:
        print("research build lacks RESEARCH_COUNTERS; refusing to count")
        return 2
    with (session.root / "invocations.jsonl").open("a") as fh:
        fh.write(json.dumps(
            {"ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
             "argv": sys.argv[1:]}) + "\n")

    start_env = sample_env()
    if args.analyze_only:
        # Never rewrite the frozen start/end/domain receipts during a
        # re-analysis: those files are evidence of the run window.
        domain = json.loads(
            (session.root / "environment-domain.json").read_text())
    elif args.resume and (session.root / "environment-domain.json").exists():
        domain = json.loads(
            (session.root / "environment-domain.json").read_text())
        (session.root / "environment-start-resume.json").write_text(
            json.dumps(start_env, indent=2, sort_keys=True) + "\n")
    else:
        domain = domain_from(start_env)
        (session.root / "environment-start.json").write_text(
            json.dumps(start_env, indent=2, sort_keys=True) + "\n")
        (session.root / "environment-domain.json").write_text(
            json.dumps(domain, indent=2, sort_keys=True) + "\n")
    if not args.analyze_only:
        start_env, notes = gate_wait(start_env, domain, "session start")
        if notes:
            print("session start paused: %s" % notes, flush=True)

    if not args.analyze_only:
        (session.root / "binary-manifest.json").write_text(json.dumps(
            {"schema": "userspace-counter-binaries/1",
             "sluice_revision": session.revision,
             "fio": {"path": shutil.which(session.fio),
                     "version": subprocess.run(
                         [session.fio, "--version"], capture_output=True,
                         text=True).stdout.strip()},
             "binaries": identities,
             "counter_schema": "sluice-fio-research/2",
             "counter_extension": "scan_hist[0..64] additive histogram"},
            indent=2, sort_keys=True) + "\n")
    fio_sha = R.sha256_file(pathlib.Path(shutil.which(session.fio)))

    try:
        if not args.analyze_only:
            for cell in cells:
                data = session.prep_file(cell, session.file_size)
                for arm, rep in RUN_ORDER:
                    measured_run(session, cell, arm, rep, data, domain,
                                 fio_sha)
                if not args.skip_trace:
                    trace_run(session, cell, data, domain, fio_sha)
    except SessionAbort as exc:
        (session.root / "ABORT.json").write_text(json.dumps(
            {"ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
             "reason": str(exc), "env": sample_env()},
            indent=2, sort_keys=True) + "\n")
        print("ABORT:", exc)
        return 4

    if not args.analyze_only:
        end_env = sample_env()
        (session.root / "environment-end.json").write_text(
            json.dumps(end_env, indent=2, sort_keys=True) + "\n")
        try:
            gate_wait(end_env, domain, "session end", timeout_s=300)
        except SessionAbort as exc:
            (session.root / "ABORT.json").write_text(json.dumps(
                {"ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                 "reason": str(exc), "env": end_env}, indent=2,
                sort_keys=True) + "\n")
            print("ABORT:", exc)
            return 4

    analysis = analyse(session)
    audit = stop_rule_audit(session, analysis)
    write_logs(session.root, analysis, identities, session)
    manifest = {
        "schema": "userspace-counters-session/1",
        "session": SESSION_ID,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "protocol": "PROTOCOL.md",
        "master_protocol":
            "docs/fio-integration/userspace/EXPERIMENT-PROTOCOL.md",
        "instrumentation":
            "docs/fio-integration/userspace/INSTRUMENTATION.md",
        "host": os.uname().nodename,
        "kernel": os.uname().release,
        "sluice_revision": session.revision,
        "worktree_head": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(root),
            capture_output=True, text=True).stdout.strip(),
        "worktree_dirty": bool(subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(root),
            capture_output=True, text=True).stdout.strip()),
        "fio": subprocess.run(
            [session.fio, "--version"], capture_output=True,
            text=True).stdout.strip(),
        "runtime_s": session.runtime_s,
        "ramp_s": 0,
        "file_size": session.file_size,
        "cpu_set": session.cpu_set,
        "cells": {c: {"u_label": CELL_U[c], **R.CELLS[c]} for c in cells},
        "run_order_per_cell": ["%s/rep%d" % (a, r) for a, r in RUN_ORDER]
                              + ["trace-bpf/rep1"],
        "binaries": identities,
        "counter_extension": {
            "schema": "sluice-fio-research/2",
            "field": "scan_hist[0..64]",
            "semantics": "harvest() calls by examined slot count; "
                         ">64 saturates at 64",
            "release_affected": False,
            "core_counters": "runtime counts from research SDK; "
                             "not INFERRED tickets_taken x capacity",
        },
        "units": {
            "per_op": "fio read.total_ios (== staged_completions)",
            "instr_per_io": "perf instructions:u / read.total_ios",
            "scan_slots": "harvest() loop-body executions per harvest call",
        },
        "analysis": analysis,
        "invariants": {
            "all_research_runs_pass": all(
                m["all_invariants_pass"]
                for d in analysis["cells"].values()
                for m in d["research"]),
        },
        "stop_rules": audit,
        "failures": analysis["failures"],
        "uncontrolled": [
            "governor left at machine default; turbo not pinned",
            "direct=0 warm page cache; single SATA SSD on btrfs",
            "research-build atomics perturb execution; only the ABBA "
            "contrast quantifies it",
            "trace-bpf run carries bpftrace observer overhead; its IOPS "
            "is not a U5 measurement",
            "perf instructions:u covers the fio task user context only",
            "n=2: observed ranges, no p-values/CI",
        ],
        "commands": {
            "build_release": "XMAKE_STATS=false xmake build -y "
                             "fio-sluice-ring",
            "build_research": "XMAKE_STATS=false xmake build -y "
                              "fio-sluice-ring-research",
            "session": "SLUICE_SUDO_PW=<redacted> python3 -I "
                       "tools/attribution/counters_session.py "
                       + " ".join(sys.argv[1:]),
            "perf": "perf stat -x, -e instructions:u,task-clock",
        },
    }
    (session.root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    write_sha_manifest(session.root)
    print("session complete:", session.root)
    return 0 if not analysis["failures"] else 5


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""AB/BA baseline session driver for results/userspace-cost/session-001-baseline.

This is a thin scheduler over attribution.runner.Session, not a new
benchmark platform: it uses the existing frozen cells (plus the minimal
A5 = iodepth 64 cell added to runner.CELLS for the baseline U4 endpoint),
the existing fio job template, the existing tripwire/retry rules and the
existing evidence layout.  What it adds is only what the baseline ask
requires and the stock mode_* helpers do not provide:

  * per-cell interleaved order ABCCBA (io_uring -> sluice-uring ->
    sluice-threadpool -> threadpool -> uring -> io_uring), so each
    engine's two reps are separated by the other engines;
  * clean0 (ramp=0) and pmu-root (g1/g2/g3) in one frozen sequence;
  * per-run environment snapshots and a governor/temperature gate that
    aborts (keeps all data) when the domain is left;
  * per-run denominator.json and perf-stats.json consolidating
    io_bytes/bs/total_ios, achieved-depth histogram and the raw perf CSV.

The session protocol is written once, before the first measured run.
The sudo password is read from an environment variable, popped
immediately and only ever piped to `sudo -S` stdin.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time

TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

from attribution import runner as R  # noqa: E402
from attribution.parse_perf import parse_perf_stat_csv  # noqa: E402

CELL_U = {"A1": "U1", "A4": "U2", "A2": "U3", "A5": "U4", "A3": "U5"}
CELL_ORDER = ("A1", "A4", "A2", "A5", "A3")
DEFAULT_ENGINES = ("io_uring", "sluice-uring", "sluice-threadpool")
GROUPS = (
    ("pmu-root-g1", ("instructions:u", "instructions:k", "task-clock")),
    ("pmu-root-g2", ("cycles:u", "cycles:k")),
    ("pmu-root-g3", ("branches", "branch-misses", "context-switches",
                     "cpu-migrations", "L1-dcache-loads", "LLC-loads")),
)
LOADAVG_MAX = 2.5
PACKAGE_TEMP_MAX_C = 85.0
CORE_TEMP_MAX_C = 85.0
NVME_TEMP_MAX_C = 80.0


class SessionAbort(RuntimeError):
    pass


# -- environment snapshots / gates -----------------------------------------

def _read_text(path: pathlib.Path, default: str | None = None) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return default


def _temps_c() -> dict:
    temps: dict = {}
    base = pathlib.Path("/sys/class/hwmon")
    if not base.is_dir():
        return temps
    for hw in sorted(base.glob("hwmon*")):
        name = _read_text(hw / "name", hw.name) or hw.name
        for probe in sorted(hw.glob("temp*_input")):
            label = _read_text(
                probe.with_name(probe.name.replace("_input", "_label")))
            try:
                value = int(probe.read_text().strip()) / 1000.0
            except (OSError, ValueError):
                continue
            temps["%s/%s" % (name, label or probe.stem)] = round(value, 1)
    return temps


def _competing() -> dict:
    out = {}
    for pat in ("fio", "perf", "strace", "bpftrace"):
        proc = subprocess.run(["pgrep", "-x", pat],
                              capture_output=True, text=True)
        out[pat] = proc.stdout.split()
    return out


def sample_env() -> dict:
    try:
        load = [float(x) for x in
                pathlib.Path("/proc/loadavg").read_text().split()[:3]]
    except (OSError, ValueError):
        load = []
    governors = {}
    freqs = {}
    for gov in sorted(pathlib.Path(
            "/sys/devices/system/cpu").glob(
                "cpu[0-9]*/cpufreq/scaling_governor")):
        # gov.parent is .../cpuN/cpufreq; the CPU name is one level up.
        cpu = gov.parent.parent.name
        governors[cpu] = _read_text(gov)
        cur = _read_text(gov.parent / "scaling_cur_freq")
        try:
            freqs[cpu] = int(cur) // 1000
        except (TypeError, ValueError):
            pass
    try:
        no_turbo = int(_read_text(
            pathlib.Path("/sys/devices/system/cpu/intel_pstate/no_turbo"),
            "UNKNOWN"))
    except ValueError:
        no_turbo = None
    mem = {}
    try:
        for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
            if line.startswith(("MemAvailable:", "MemFree:", "Cached:")):
                key, value = line.split(":", 1)
                mem[key] = int(value.split()[0])
    except (OSError, ValueError):
        pass
    return {
        "ts_unix": round(time.time(), 3),
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "loadavg": load,
        "governors": governors,
        "scaling_cur_freq_mhz": freqs,
        "no_turbo": no_turbo,
        "temps_c": _temps_c(),
        "competing": _competing(),
        "mem_kb": mem,
        "perf_event_paranoid": _read_text(
            pathlib.Path("/proc/sys/kernel/perf_event_paranoid")),
        "nmi_watchdog": _read_text(
            pathlib.Path("/proc/sys/kernel/nmi_watchdog")),
        "uptime_s": (_read_text(pathlib.Path("/proc/uptime")) or "").split()[:1]
        or None,
    }


def domain_from(env: dict) -> dict:
    return {
        "loadavg_1min_max": LOADAVG_MAX,
        "package_temp_max_c": PACKAGE_TEMP_MAX_C,
        "core_temp_max_c": CORE_TEMP_MAX_C,
        "nvme_temp_max_c": NVME_TEMP_MAX_C,
        "governors": dict(env["governors"]),
        "no_turbo": env["no_turbo"],
    }


def check(env: dict, domain: dict) -> list:
    problems = []
    for pat, pids in env["competing"].items():
        if pids:
            problems.append("competing %s pids %s" % (pat, pids))
    if not env["loadavg"]:
        problems.append("loadavg unreadable")
    elif env["loadavg"][0] > domain["loadavg_1min_max"]:
        problems.append("loadavg 1min %.2f > %.2f"
                        % (env["loadavg"][0], domain["loadavg_1min_max"]))
    if env["governors"] != domain["governors"]:
        changed = {cpu: (domain["governors"].get(cpu), val)
                   for cpu, val in env["governors"].items()
                   if domain["governors"].get(cpu) != val}
        problems.append("governor changed: %s" % changed)
    if env["no_turbo"] != domain["no_turbo"]:
        problems.append("no_turbo changed: %r -> %r"
                        % (domain["no_turbo"], env["no_turbo"]))
    for key, value in env["temps_c"].items():
        low = key.lower()
        if low.startswith("coretemp/package"):
            limit = domain["package_temp_max_c"]
        elif low.startswith("coretemp/core"):
            limit = domain["core_temp_max_c"]
        elif low.startswith("nvme/") or "composite" in low:
            limit = domain["nvme_temp_max_c"]
        else:
            continue
        if value > limit:
            problems.append("%s %.1fC > %.1fC" % (key, value, limit))
    return problems


def gate(env: dict, domain: dict, where: str) -> list:
    problems = check(env, domain)
    if problems:
        raise SessionAbort("environment gate failed at %s: %s"
                           % (where, "; ".join(problems)))
    return problems


# -- frozen protocol --------------------------------------------------------

def write_protocol(session: R.Session, argv: list, engines, cells) -> None:
    path = session.root / "PROTOCOL.md"
    if path.exists():
        return
    lines = [
        "# userspace-cost baseline session protocol (frozen before runs)",
        "",
        "- session: `session-001-baseline`",
        "- generated: %s" % time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "- driver: `tools/attribution/baseline_session.py`",
        "- argv: `%s`" % " ".join(argv),
        "- sluice_revision: `%s`" % session.revision,
        "- fio: `%s` (%s)" % (session.fio, R.FIO_VERSION),
        "- host: %s" % os.uname().nodename,
        "- runtime / ramp / file: %ss / 0s / %s"
        % (session.runtime_s, session.file_size),
        "- cpu_set: `%s`" % session.cpu_set,
        "- repeats: 2 per (cell, engine); order per cell ABCCBA",
        "",
        "## 1. Workload cells (frozen)",
        "",
        "U1-U5 in this session are **workload-unit labels**, not the",
        "counter-ratio U1-U5 of `docs/fio-integration/userspace/",
        "EXPERIMENT-PROTOCOL.md` §1.  To avoid collisions in raw paths,",
        "run ids use the A-cell names; the mapping is fixed here and must",
        "not be renamed after the fact.",
        "",
        "| U | cell | rw | bs | iodepth | window |",
        "|---|---|---|---|---|---|",
    ]
    for cell in cells:
        spec = R.CELLS[cell]
        lines.append("| %s | %s | %s | %s | %d | time_based runtime=%ss "
                     "ramp_time=0 |"
                     % (CELL_U.get(cell, "?"), cell, spec["rw"], spec["bs"],
                        spec["iodepth"], session.runtime_s))
    lines += [
        "",
        "A5 is the minimal added cell (`randread 4k iodepth=64`), the",
        "U4 deep-queue endpoint; `DEFAULT_CELLS` is unchanged.",
        "",
        "Fixed fio options: `numjobs=1 thread=1 direct=0 invalidate=0",
        "randseed=81 group_reporting=1 buffer_pattern=0x5a`, one file per",
        "cell (`%s`), written with `chattr +C` and pre-warmed by the"
        % session.file_size,
        "runner's psync write + async read before the cell's first run.",
        "",
        "## 2. Engines and interleaving (frozen)",
        "",
        "Engines: `%s`." % "`, `".join(engines),
        "Per cell the measured order is ABCCBA, i.e. for two engines",
        "ABBA.  The same order is used for clean0 and pmu-root:",
        "",
        "```text",
    ]
    for cell in cells:
        order = interleaved(list(engines), 2)
        lines.append("%s: %s" % (cell, ", ".join(
            "%s/rep%d" % (e, r) for e, r in order)))
    lines += [
        "```",
        "",
        "## 3. Modes and windows (frozen)",
        "",
        "- clean0: `time_based=1 runtime=%ss ramp_time=0`; fio JSON only."
        % session.runtime_s,
        "- pmu-root: same job, wrapped as root with `perf stat -x,`; three",
        "  separate fio runs per rep: g1 = `instructions:u,",
        "  instructions:k,task-clock`; g2 = `cycles:u,cycles:k`;",
        "  g3 = `branches,branch-misses,context-switches,cpu-migrations,",
        "  L1-dcache-loads,LLC-loads`.  g1/g2 are expected at 100%",
        "  time-running; any g3 event below 100% is SCALED and is reported",
        "  as SAMPLED, never mixed into counted per-op numbers.",
        "",
        "## 4. Denominators (frozen)",
        "",
        "- primary: fio `read.total_ios`; cross-check",
        "  `read.io_bytes / bs == read.total_ios` for every run;",
        "- bytes: fio `read.io_bytes`; block size: fio job option `bs`;",
        "- time: fio `read.runtime` (ms) for throughput; perf `task-clock`",
        "  (msec) for CPU time; both are recorded per run;",
        "- achieved depth: fio `iodepth_level` histogram (fio's own",
        "  queue-depth sampler) plus a bucket-lower-bound weighted mean",
        "  (INFERRED; the `>=64` bucket is valued at 64).",
        "",
        "## 5. Environment domain and abort rule (frozen)",
        "",
        "Before and after every measured run: 1-min loadavg <= %.1f;" % LOADAVG_MAX,
        "no foreign `fio`, `perf`, `strace`, `bpftrace`; every CPU",
        "governor identical to the session-start snapshot;",
        "`intel_pstate/no_turbo` identical to start; coretemp package and",
        "core <= %.1f C; NVMe composite <= %.1f C." %
        (PACKAGE_TEMP_MAX_C, NVME_TEMP_MAX_C),
        "Any violation aborts the session immediately; raw evidence is",
        "kept (no deletion, no overwrite).",
        "",
        "## 6. Labels (frozen)",
        "",
        "- COUNTED: fio counters (IOPS, io_bytes, total_ios, runtime),",
        "  perf 100%-time-running event counts, task-clock;",
        "- SAMPLED: perf events with time-running < 100% (scaled), and the",
        "  fio queue-depth histogram (sampled distribution, not per-IO);",
        "- INFERRED: achieved-depth scalar derived from that histogram;",
        "- NOT_IDENTIFIED: anything not directly measured (e.g. kernel",
        "  work by io-wq workers outside the fio task's PMU context).",
        "",
        "No sampled share is ever added to a counted per-op number.",
        "",
        "## 7. A/A (frozen)",
        "",
        "For each (cell, engine, mode), rep1 vs rep2; report",
        "`(max-min)/mean*100%` for IOPS and instructions/IO.  Two",
        "repetitions are an observed range, not a confidence interval.",
        "",
        "## 8. Uncontrolled items (declared)",
        "",
        "- governor left at the machine default (`schedutil` at freeze",
        "  time), turbo not pinned; only change is gated;",
        "- `direct=0` warm page cache (file pre-warmed; 62 GiB RAM);",
        "- single SATA SSD on btrfs (zstd:1), not an NVMe device;",
        "- perf `instructions:k/cycles:k` cover kernel execution in the",
        "  fio task's context; io-wq/kworker work outside that context is",
        "  NOT_IDENTIFIED, not zero.",
        "",
        "## 9. Source references",
        "",
        "- `docs/fio-integration/userspace/EXPERIMENT-PROTOCOL.md`",
        "  (stop rules, tripwires, counter definitions);",
        "- `docs/fio-integration/userspace/INSTRUMENTATION.md`",
        "  (counter semantics; research build not used in this baseline);",
        "- `docs/fio-integration/COST-ATTRIBUTION-METHOD.md` (A-cells);",
        "- `docs/fio-integration/MEASUREMENT-WINDOW-CONTRACT.md` (window).",
        "",
    ]
    path.write_text("\n".join(lines) + "\n")


def interleaved(engines: list, reps: int) -> list:
    order = []
    for rep in range(1, reps + 1):
        seq = engines if rep % 2 == 1 else list(reversed(engines))
        order += [(engine, rep) for engine in seq]
    return order


# -- per-run evidence -------------------------------------------------------

def _bs_bytes(text: str) -> int | None:
    match = re.fullmatch(r"(\d+)([kKmM]?)", text.strip())
    if not match:
        return None
    mult = {"": 1, "k": 1024, "m": 1024 * 1024}[match.group(2).lower()]
    return int(match.group(1)) * mult


def _mean_depth(hist: dict) -> float | None:
    weighted = 0.0
    total = 0.0
    for bucket, pct in hist.items():
        try:
            depth = 64 if bucket == ">=64" else int(bucket)
            weighted += depth * float(pct)
            total += float(pct)
        except (TypeError, ValueError):
            continue
    return round(weighted / total, 3) if total > 0 else None


def attach_evidence(session: R.Session, rec: dict, cell: str, engine: str,
                    phase: str, env_before: dict, env_after: dict,
                    extra: dict) -> dict:
    run_dir = session.accepted_run_dir(rec)
    (run_dir / "env-before.json").write_text(
        json.dumps(env_before, indent=2, sort_keys=True) + "\n")
    (run_dir / "env-after.json").write_text(
        json.dumps(env_after, indent=2, sort_keys=True) + "\n")
    doc = json.loads((run_dir / "fio-result.json").read_text())
    job = doc["jobs"][0]
    read = job.get("read") or {}
    opts = job.get("job options") or {}
    bs_bytes = _bs_bytes(str(opts.get("bs") or R.CELLS[cell]["bs"]))
    total = read.get("total_ios")
    io_bytes = read.get("io_bytes")
    hist = job.get("iodepth_level") or {}
    den = {
        "schema": "baseline-denominator/1",
        "run_id": rec["run_id"],
        "cell": cell,
        "u_label": CELL_U.get(cell),
        "engine": engine,
        "phase": phase,
        "bs": opts.get("bs") or R.CELLS[cell]["bs"],
        "bs_bytes": bs_bytes,
        "iodepth_requested": R.CELLS[cell]["iodepth"],
        "io_bytes": io_bytes,
        "total_ios": total,
        "io_bytes_over_bs": (io_bytes // bs_bytes
                             if io_bytes is not None and bs_bytes else None),
        "denominator_identity": (io_bytes == total * bs_bytes
                                 if None not in (io_bytes, total, bs_bytes)
                                 else None),
        "runtime_ms": read.get("runtime"),
        "elapsed_s": job.get("elapsed"),
        "iops": read.get("iops"),
        "bw_bytes_per_s": read.get("bw_bytes"),
        "usr_cpu_pct": job.get("usr_cpu"),
        "sys_cpu_pct": job.get("sys_cpu"),
        "iodepth_level": hist,
        "achieved_depth_mean_lower_bound": _mean_depth(hist),
        "labels": {
            "io_bytes/total_ios/runtime/iops": "COUNTED (fio)",
            "iodepth_level": "SAMPLED (fio queue-depth sampler)",
            "achieved_depth_mean_lower_bound": "INFERRED (bucket "
                                               "lower-bound; >=64 -> 64)",
        },
        "engine_sha256": extra.get("engine_sha256"),
        "fio_sha256": extra.get("fio_sha256"),
        "sluice_revision": extra.get("sluice_revision"),
        "wall_s": rec.get("wall_s"),
        "rusage": rec.get("rusage"),
        "engine_load": rec.get("engine_load"),
    }
    (run_dir / "denominator.json").write_text(
        json.dumps(den, indent=2, sort_keys=True) + "\n")
    perf_path = run_dir / "perf-stat.raw.txt"
    if phase != "clean0" and perf_path.exists():
        events = parse_perf_stat_csv(perf_path.read_text())
        per_op = {}
        labels = {}
        for event, row in events.items():
            labels[event] = ("COUNTED" if row["kind"] == "counted"
                             else "SAMPLED")
            value = row["value"]
            if value is None or not total:
                continue
            if event in ("task-clock", "task-clock:u"):
                per_op["task_clock_us_per_io"] = round(
                    float(value) * 1000.0 / total, 4)
            else:
                per_op["%s_per_io" % event] = round(float(value) / total, 4)
        tclock = events.get("task-clock",
                            events.get("task-clock:u", {})).get("value")
        runtime_ms = read.get("runtime")
        wall_ratio = (round(float(tclock) / float(runtime_ms), 5)
                      if tclock and runtime_ms else None)
        stats = {
            "schema": "baseline-perf-stats/1",
            "run_id": rec["run_id"],
            "raw": "perf-stat.raw.txt",
            "events": events,
            "per_op": per_op,
            "task_clock_ms_per_fio_runtime_ms": wall_ratio,
            "labels": labels,
            "note": "events with time_running < 100% are scaled; they are "
                    "SAMPLED and excluded from counted per-op claims",
        }
        (run_dir / "perf-stats.json").write_text(
            json.dumps(stats, indent=2, sort_keys=True) + "\n")
    return den


# -- run loops ---------------------------------------------------------------

def _engine_sha(engine: str) -> str | None:
    path = R.engine_specs()[engine][2]
    return R.sha256_file(path) if path else None


def _common_extra(cell: str, engine: str, phase: str, fio_sha: str | None,
                  session: R.Session, group: str | None = None) -> dict:
    return {
        "cell": cell,
        "u_label": CELL_U.get(cell),
        "cell_spec": R.CELLS[cell],
        "phase": phase,
        "perf_group": group,
        "engine_sha256": _engine_sha(engine),
        "fio_sha256": fio_sha,
        "sluice_revision": session.revision,
    }


def _run_id(cell: str, engine: str, rep: int, phase: str,
            group: str | None) -> str:
    if phase == "clean0":
        return "%s__%s__clean0__rep%d" % (cell, engine, rep)
    return "%s__%s__%s__rep%d" % (cell, engine, group, rep)


def _accepted_run(session: R.Session, run_id: str) -> bool:
    meta = session.runs_dir / run_id / "meta.json"
    try:
        doc = json.loads(meta.read_text())
    except (OSError, ValueError):
        return False
    return doc.get("status") == "ok" and not doc.get("discarded")


def run_cell_phase(session: R.Session, cell: str, engine: str, rep: int,
                   phase: str, data: pathlib.Path, domain: dict,
                   fio_sha: str | None, group: str | None = None,
                   events: tuple | None = None) -> dict | None:
    run_id = _run_id(cell, engine, rep, phase, group)
    if getattr(session, "skip_existing", False) and _accepted_run(session,
                                                                  run_id):
        print("skip %-52s (already accepted)" % run_id, flush=True)
        return None
    job = "\n".join(session.job_lines(engine, data, cell,
                                      ramp_override=0)) + "\n"
    wrap = None
    if group is not None:
        path = session.runs_dir / run_id / "perf-stat.raw.txt"
        wrap = session._sudo([
            "perf", "stat", "-x,", "-o", str(path),
            "-e", ",".join(events)])
    before = sample_env()
    gate(before, domain, run_id + " before")
    extra = _common_extra(cell, engine, phase, fio_sha, session, group)
    rec = session.run_with_retry(
        run_id, job_text=job, wrap=wrap,
        expect_backend=R.engine_specs()[engine][1] or None,
        extra=extra)
    after = sample_env()
    if rec["status"] != "ok":
        write_abort(session.root, "run %s failed: %s"
                    % (rec["run_id"], rec.get("failures")), after)
        raise SessionAbort("run %s failed: %s"
                           % (rec["run_id"], rec.get("failures")))
    attach_evidence(session, rec, cell, engine, phase,
                    before, after, extra)
    # A post-run loadavg excursion is caused by our own just-finished
    # workload and is not background drift.  Pause until the 1-min value
    # is back inside the domain instead of discarding a valid run; every
    # other gate failure aborts immediately.
    problems = check(after, domain)
    if problems:
        if not all(p.startswith("loadavg") for p in problems):
            raise SessionAbort("environment gate failed at %s after: %s"
                               % (run_id, "; ".join(problems)))
        run_dir = session.accepted_run_dir(rec)
        waited = 0
        settled = after
        while waited < 300:
            time.sleep(10)
            waited += 10
            settled = sample_env()
            if not check(settled, domain):
                break
        (run_dir / "env-after-settled.json").write_text(
            json.dumps(settled, indent=2, sort_keys=True) + "\n")
        (run_dir / "env-pause.json").write_text(json.dumps(
            {"run_id": run_id, "problems": problems, "waited_s": waited,
             "settled_problems": check(settled, domain)},
            indent=2, sort_keys=True) + "\n")
        if check(settled, domain):
            raise SessionAbort("loadavg did not settle after %ss at %s: %s"
                               % (waited, run_id, check(settled, domain)))
        print("pause %.0fs after %s (loadavg excursion, data kept)"
              % (waited, rec["run_id"]), flush=True)
    print("ok %-52s wall=%ss" % (rec["run_id"], rec.get("wall_s")),
          flush=True)
    time.sleep(1)  # protocol §2: sleep 1 s between runs
    return rec


def write_abort(root: pathlib.Path, reason: str, env: dict) -> None:
    (root / "ABORT.json").write_text(json.dumps(
        {"ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
         "reason": reason, "env": env}, indent=2, sort_keys=True) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--results-dir",
                    default="results/userspace-cost/session-001-baseline")
    ap.add_argument("--scratch-dir", default="build/scratch")
    ap.add_argument("--cells", default=",".join(CELL_ORDER))
    ap.add_argument("--engines", default=",".join(DEFAULT_ENGINES))
    ap.add_argument("--phase", choices=["clean0", "pmu-root", "all"],
                    default="all")
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--runtime", type=int, default=10)
    ap.add_argument("--file-size", default="1G")
    ap.add_argument("--cpu-set", default="0-9")
    ap.add_argument("--fio", default="fio")
    ap.add_argument("--sudo-password-env", default="SLUICE_SUDO_PW")
    ap.add_argument("--freeze-only", action="store_true",
                    help="write protocol/environment/binary manifest only")
    ap.add_argument("--resume", action="store_true",
                    help="skip already-accepted runs and reuse an existing "
                         "prepared cell file (pause/resume without reruns)")
    args = ap.parse_args()

    cells = [c.strip() for c in args.cells.split(",") if c.strip()]
    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    for cell in cells:
        if cell not in R.CELLS:
            ap.error("unknown cell %s" % cell)
    for engine in engines:
        if engine not in R.engine_specs():
            ap.error("unknown engine %s" % engine)

    root = pathlib.Path(__file__).resolve().parents[2]
    results = root / args.results_dir
    scratch = root / args.scratch_dir
    sudo_pw = os.environ.get(args.sudo_password_env)
    if sudo_pw:
        os.environ.pop(args.sudo_password_env, None)
    if not args.freeze_only and not sudo_pw:
        print("pmu-root requires --sudo-password-env to be set")
        return 2

    session = R.Session(
        results, scratch, cpu_set=args.cpu_set, runtime_s=args.runtime,
        ramp_s=0, file_size=args.file_size, fio=args.fio,
        sudo_pw=sudo_pw)
    session.ensure_layout()
    session.skip_existing = bool(args.resume)
    write_protocol(session, sys.argv[1:], engines, cells)
    with (session.root / "invocations.jsonl").open("a") as fh:
        fh.write(json.dumps(
            {"ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
             "argv": sys.argv[1:]}) + "\n")

    if args.freeze_only:
        session.write_environment_and_capability()
        session.write_binary_manifest()
        print("frozen:", session.root)
        return 0

    start_env = sample_env()
    domain = domain_from(start_env)
    tag = "environment-start-resume.json" if args.resume \
        else "environment-start.json"
    dtag = "environment-domain-resume.json" if args.resume \
        else "environment-domain.json"
    (session.root / tag).write_text(
        json.dumps(start_env, indent=2, sort_keys=True) + "\n")
    (session.root / dtag).write_text(
        json.dumps(domain, indent=2, sort_keys=True) + "\n")
    try:
        gate(start_env, domain, "session start")
    except SessionAbort as exc:
        write_abort(session.root, str(exc), start_env)
        print(str(exc))
        return 3

    session.write_environment_and_capability()
    manifest = session.write_binary_manifest()
    fio_sha = (manifest.get("fio_baseline") or {}).get("sha256")

    try:
        for cell in cells:
            data = session.workdir / (cell + ".bin")
            # On resume the cell file and its page-cache residency are
            # reused; re-prepping would change the inode for the remaining
            # reps and break cell-internal comparability.
            if not (session.skip_existing and data.exists()):
                data = session.prep_file(cell, session.file_size)
            else:
                print("reuse %s" % data, flush=True)
            if args.phase in ("clean0", "all"):
                for engine, rep in interleaved(engines, args.repeats):
                    run_cell_phase(session, cell, engine, rep, "clean0",
                                   data, domain, fio_sha)
            if args.phase in ("pmu-root", "all"):
                for engine, rep in interleaved(engines, args.repeats):
                    for group, events in GROUPS:
                        run_cell_phase(session, cell, engine, rep,
                                       "pmu-root", data, domain, fio_sha,
                                       group=group, events=events)
    except SessionAbort as exc:
        write_abort(session.root, str(exc), sample_env())
        print("ABORT:", exc)
        return 4

    end_env = sample_env()
    (session.root / "environment-end.json").write_text(
        json.dumps(end_env, indent=2, sort_keys=True) + "\n")
    try:
        gate(end_env, domain, "session end")
    except SessionAbort as exc:
        write_abort(session.root, str(exc), end_env)
        print("ABORT:", exc)
        return 4
    session.write_sha_manifest()
    print("session complete:", session.root)
    return 0


if __name__ == "__main__":
    sys.exit(main())

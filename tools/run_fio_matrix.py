#!/usr/bin/env python3
"""E5 performance baseline matrix (Phase 5): A/A + F1-F4 x four engines.

Contract (executed verbatim by the workflow):
    python3 tools/run_fio_matrix.py --scratch-dir build/scratch \
        --results-dir results/fio [--rehearsal]

Determinism and comparability rules:
  - Strictly serial: one fio process at a time, fixed execution order
    (A/A pairs first, then F1..F4; engines in the order psync, io_uring,
    sluice-threadpool, sluice-uring; repeat 1 then repeat 2 per engine).
  - Every job pins randseed=81 and relies on fio's default randrepeat=1,
    so every engine replays the same offset walk for a given case shape.
  - numjobs=1, direct=0 (buffered, page-cache resident working set),
    time_based=1 with fixed runtime + ramp_time; stats after ramp only.
  - Per-case dedicated test file under the scratch directory, pre-created
    by a recorded psync sequential write with fsync_on_close=1 (clean),
    then by a recorded full sequential warm-up read through an async
    engine so every engine starts page-cache resident. Files are deleted
    afterwards.
  - Each run's fio --output-format=json original is kept verbatim as
    results/<run_id>.jsonl; the job file is kept beside it under jobs/.
  - Per-run CPU and context switches come from getrusage(RUSAGE_CHILDREN)
    deltas around the single serial fio child (covers fio's threads and
    the engine's worker threads, which all live in the fio process).
  - Engine-side tripwire diagnostics (same list as check_fio_engine.py)
    and a backend mismatch on the engine load line fail the run: this
    guards against hidden POSIX fallback inside a performance run.

Availability policy: the two uring arms (native io_uring and
sluice-uring) are probed first; if unavailable, an UNAVAILABLE record is
written and the sub-item is skipped. Exit code stays 0 unless the psync
or sluice-threadpool arms (or the harness itself) fail.

Rehearsal mode (--rehearsal) shrinks runtime/ramp/file-size for a 3-5s
per-run self-test; it is not the formal matrix.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import resource
import subprocess
import sys
import time

FIO_VERSION = "fio-3.40"
RANDSEED = 81

ENGINE_PSYNC = "psync"
ENGINE_IOURING = "io_uring"
ENGINE_SLUICE_TP = "sluice-threadpool"
ENGINE_SLUICE_URING = "sluice-uring"
URING_ENGINES = (ENGINE_IOURING, ENGINE_SLUICE_URING)
ENGINE_ORDER = (ENGINE_PSYNC, ENGINE_IOURING, ENGINE_SLUICE_TP,
                ENGINE_SLUICE_URING)

# Case table: (case_id, rw, bs, iodepth, size, extra options); size None
# means the global --file-size. F3 runs last: its write mix stresses the
# slow SATA SSD write path, and any leftover state must not pollute the
# read-only cases. F3 retains the full-span shape; a small dedicated
# span was tried (rehearsal 2026-11-19) and made three of four engines
# COLLAPSE (writeback/extent contention on the hot set), and fio 3.40's
# number_ios cap empirically does not bind under time_based (receipt: a
# 100k-capped 4s job issued 564k ios). Residual F3 repeat spread on this
# substrate is reported per-rep instead of being hidden behind a mean.
CASES = (
    ("F1", "randread", "4k", 1, None, ()),
    ("F2", "randread", "4k", 16, None, ()),
    ("F4", "read", "64k", 4, None, ()),
    ("F3", "randrw", "4k", 16, None, ("rwmixread=70",)),
)

# Same tripwire list as tools/check_fio_engine.py FORBIDDEN.
TRIPWIRES = (
    "slot accounting bug",
    "lost accepted request",
    "previous completion batch was not fully consumed",
    "staged completions were never consumed",
    "still outstanding",
    "with an in-flight request",
    "queue before engine init",
    "was never registered",
    "submit threw",
    "engine state missing",
    "failing closed",
    "request failed: code=",
)
LOAD_RE = re.compile(
    r"fio-sluice: loaded revision=(\S+) sdk_profile=(\S+) fio_source=(\S+)"
    r" backend=(\S+) iodepth=(\d+) workers=(\d+)"
)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Serial A/A + F1-F4 x four-engine fio matrix")
    ap.add_argument("--scratch-dir", default="build/scratch")
    ap.add_argument("--results-dir", default="results/fio")
    ap.add_argument("--fio", default="fio")
    ap.add_argument("--runtime", type=int, default=10,
                    help="measured fio runtime per run (s); capped at 20")
    ap.add_argument("--ramp-time", type=int, default=2,
                    help="fio ramp_time excluded from stats (s)")
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--file-size", default="1G",
                    help="per-case test file size")
    ap.add_argument("--cpu-set", default="0-9",
                    help="taskset CPU list pinning fio and its engine "
                         "threads to one SMT sibling per physical core; "
                         "empty string disables pinning")
    ap.add_argument("--aa-engines", default="psync,sluice-threadpool",
                    help="engines that get an A/A pair (F2 shape)")
    ap.add_argument("--rehearsal", action="store_true",
                    help="small-scale self-test: runtime=3, ramp=1, "
                         "file-size=256M")
    args = ap.parse_args()
    if args.rehearsal:
        args.runtime, args.ramp_time, args.file_size = 3, 1, "256M"
    if args.runtime > 20:
        ap.error(f"runtime {args.runtime}s exceeds the 20s/case cap")
    return args


class Matrix:
    def __init__(self, args: argparse.Namespace, root: pathlib.Path):
        self.args = args
        self.root = root
        self.fio = args.fio
        self.scratch = (root / args.scratch_dir).resolve()
        self.workdir = self.scratch / "fio-matrix"
        self.results = (root / args.results_dir).resolve()
        self.jobs_dir = self.results / "jobs"
        self.engines_dir = root / "build/linux/x86_64/release"
        self.revision = (root / "SLUICE_REVISION").read_text().strip()
        self.runs: list[dict] = []
        self.fatal: list[str] = []
        self.probe_reasons: dict[str, str] = {}
        # Overridden in main() once probe results are known: warm-up reads
        # prefer an async engine (a psync D1 cold read of a large file on
        # compress=zstd btrfs would take minutes).
        self.warm_engine: tuple = ("psync", "")
        self.finished_files: list[pathlib.Path] = []
        self.nocompress_ok: list[str] = []
        self.nocompress_fail: list[str] = []
        self.started = time.monotonic()

    # -- scratch guard (same policy as check_fio_engine.py) ---------------

    def guard(self, path: pathlib.Path) -> None:
        resolved = path.resolve()
        if self.workdir != resolved and self.workdir not in resolved.parents:
            raise RuntimeError(f"refusing non-scratch target {resolved}")
        if resolved.exists() and not resolved.is_file():
            raise RuntimeError(f"refusing non-regular target {resolved}")

    # -- environment snapshot ----------------------------------------------

    def environment_snapshot(self) -> dict:
        def read(path: str):
            try:
                return pathlib.Path(path).read_text().strip()
            except OSError:
                return None

        def read_many(pattern: str) -> list:
            vals = []
            for p in sorted(pathlib.Path("/sys/devices/system/cpu").glob(
                    pattern)):
                v = read(str(p))
                if v is not None:
                    vals.append(v)
            return vals

        gov = read_many("cpu*/cpufreq/scaling_governor")
        cur = read_many("cpu*/cpufreq/scaling_cur_freq")
        mounts = {}
        for m in ("/", "/home", "/tmp"):
            out = subprocess.run(["findmnt", "-n", "-o", "FSTYPE,SOURCE", m],
                                 capture_output=True, text=True)
            mounts[m] = out.stdout.strip() or None
        temps = {}
        for zone in sorted(pathlib.Path("/sys/class/thermal").glob(
                "thermal_zone*")):
            t = read(str(zone / "temp"))
            if t is not None:
                temps[zone.name] = t
        sudo = subprocess.run(["sudo", "-n", "true"], capture_output=True)
        perf = subprocess.run(
            ["perf", "stat", "-e", "instructions", "--", "/bin/true"],
            capture_output=True, text=True)
        def blockdev(name: str):
            base = "/sys/block/%s" % name
            out = {}
            for key, path in (("rotational", "queue/rotational"),
                              ("zoned", "queue/zoned"),
                              ("model", "device/model")):
                v = read("/".join((base, path)))
                if v is not None:
                    out[key] = v
            return out

        return {
            "uname": subprocess.run(["uname", "-r"], capture_output=True,
                                    text=True).stdout.strip(),
            "block_devices": {d: blockdev(d) for d in ("sda", "nvme0n1")
                              if pathlib.Path("/sys/block/" + d).is_dir()},
            "cpu_model": next((ln.split(":", 1)[1].strip()
                               for ln in pathlib.Path("/proc/cpuinfo")
                               .read_text().splitlines()
                               if ln.startswith("model name")), None),
            "online_cpus": len(os.sched_getaffinity(0)),
            "smt_siblings": {
                "cpu0": read("/sys/devices/system/cpu/cpu0/topology/"
                             "thread_siblings_list"),
                "cpu9": read("/sys/devices/system/cpu/cpu9/topology/"
                             "thread_siblings_list"),
            },
            "governor": {"values": sorted(set(gov)),
                         "all_same": len(set(gov)) == 1} if gov else None,
            "cpufreq_driver": read("/sys/devices/system/cpu/cpu0/cpufreq/"
                                   "scaling_driver"),
            "intel_pstate_status": read("/sys/devices/system/cpu/"
                                        "intel_pstate/status"),
            "no_turbo": read("/sys/devices/system/cpu/intel_pstate/no_turbo"),
            "freq_bounds_khz": {
                "min": read("/sys/devices/system/cpu/cpu0/cpufreq/"
                            "scaling_min_freq"),
                "max": read("/sys/devices/system/cpu/cpu0/cpufreq/"
                            "scaling_max_freq"),
            },
            "idle_cur_freq_sample_khz": cur,
            "mounts": mounts,
            "scratch_fs": subprocess.run(
                ["stat", "-f", "-c", "%T", str(self.workdir)],
                capture_output=True, text=True).stdout.strip(),
            "temperatures_milli_c": temps,
            "perf_event_paranoid": read("/proc/sys/kernel/"
                                        "perf_event_paranoid"),
            "perf_user_level_ok": perf.returncode == 0,
            "sudo_passwordless": sudo.returncode == 0,
            "loadavg": pathlib.Path("/proc/loadavg").read_text().split()[:3],
            "cpu_set": self.args.cpu_set or None,
            "fio_version": subprocess.run([self.fio, "--version"],
                                          capture_output=True,
                                          text=True).stdout.strip(),
        }

    # -- fio plumbing -------------------------------------------------------

    def job_lines(self, *, engine_spec: tuple, data: pathlib.Path,
                  rw: str, bs: str, iodepth: int, size: str,
                  extra: tuple = ()) -> list:
        ioengine, backend = engine_spec
        lines = [
            "[job]",
            "ioengine=" + ioengine,
            "filename=" + str(data),
        ]
        if backend:
            lines.append("sluice_backend=" + backend)
        lines += [
            "rw=" + rw,
            "bs=" + bs,
            "size=" + size,
            "iodepth=%d" % iodepth,
            "numjobs=1",
            "direct=0",
            "thread=1",
            "group_reporting=1",
            "randseed=%d" % RANDSEED,
            # fio's invalidate=1 default drops the file's page cache at
            # every job start, which would silently turn the warm-cache
            # baseline into a cold btrfs/zstd storage baseline.
            "invalidate=0",
            # Deterministic write content, applied uniformly to every
            # job (reads ignore it): keeps file state reproducible and,
            # combined with the per-file NODATACOW flag, keeps the
            # btrfs write path free of compression/COW stalls.
            "buffer_pattern=0x5a",
        ]
        lines += list(extra)
        return lines

    def run_fio(self, run_id: str, job_text: str,
                expect_backend: str = None,
                timeout_s: float = None) -> dict:
        """One serial fio run; saves the JSON original + the job file."""
        jf = self.jobs_dir / (run_id + ".fio")
        jf.write_text(job_text)
        for target in re.findall(r"^filename=(.*)$", job_text, re.MULTILINE):
            self.guard(pathlib.Path(target))
        argv = []
        if self.args.cpu_set:
            argv += ["taskset", "-c", self.args.cpu_set]
        argv += [self.fio, "--eta=never", "--output-format=json", str(jf)]
        if timeout_s is None:
            timeout_s = self.args.runtime + self.args.ramp_time + 90
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        t0 = time.monotonic()
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout_s, cwd=str(self.jobs_dir))
        wall = time.monotonic() - t0
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        ru = {
            "cpu_user_s": round(after.ru_utime - before.ru_utime, 6),
            "cpu_sys_s": round(after.ru_stime - before.ru_stime, 6),
            "ctx_voluntary": after.ru_nvcsw - before.ru_nvcsw,
            "ctx_involuntary": after.ru_nivcsw - before.ru_nivcsw,
        }
        (self.results / (run_id + ".jsonl")).write_text(proc.stdout)
        record = {
            "run_id": run_id,
            "fio_exit": proc.returncode,
            "wall_s": round(wall, 3),
            "rusage": ru,
            "stderr_tail": proc.stderr.splitlines()[-8:],
        }
        load = LOAD_RE.search(proc.stderr)
        record["engine_load"] = list(load.groups()) if load else None
        diag = [ln.strip() for ln in proc.stderr.splitlines()
                if "fio-sluice:" in ln and not LOAD_RE.match(ln.strip())]
        record["engine_diag"] = diag
        failures = []
        for line in diag:
            failures.append("unexpected engine diagnostic: " + line)
        for line in (ln.strip() for ln in proc.stderr.splitlines()):
            if any(f in line for f in TRIPWIRES):
                failures.append("tripwire diagnostic: " + line)
        if proc.returncode != 0:
            failures.append("fio exit %d" % proc.returncode)
        if expect_backend is not None:
            if record["engine_load"] is None:
                failures.append("engine load/version trace missing from "
                                "stderr")
            else:
                rev, _prof, _src, backend, _depth, _w = record["engine_load"]
                if backend != expect_backend:
                    failures.append("engine backend %s != requested %s"
                                    % (backend, expect_backend))
                if rev != self.revision:
                    failures.append("engine revision %s != pinned %s"
                                    % (rev, self.revision))
        record["metrics"] = self.extract_metrics(proc.stdout) \
            if proc.returncode == 0 else None
        if not failures and record["metrics"] is None:
            failures.append("fio JSON unparseable or empty")
        record["failures"] = failures
        record["status"] = "fail" if failures else "ok"
        self.runs.append(record)
        return record

    @staticmethod
    def extract_metrics(raw: str):
        # fio prepends human notes (e.g. the sync-engine depth cap) before
        # the JSON document on stdout; parse from the first '{'.
        start = raw.find("{")
        if start < 0:
            return None
        try:
            doc = json.loads(raw[start:])
            job = doc["jobs"][0]
        except (ValueError, KeyError, IndexError, TypeError):
            return None
        out = {"job_name": job.get("jobname"),
               "usr_cpu": job.get("usr_cpu"),
               "sys_cpu": job.get("sys_cpu"),
               "iodepth_level": job.get("iodepth_level"),
               "mean_depth": None,
               "sync_depth_capped": "queue depth will be capped" in raw,
               "directions": {}}
        hist = job.get("iodepth_level") or {}
        weighted, total = 0.0, 0.0
        for bucket, pct in hist.items():
            try:
                lo = 64 if bucket == ">=64" else int(bucket)
                weighted += lo * float(pct)
                total += float(pct)
            except (TypeError, ValueError):
                continue
        if total > 0:
            out["mean_depth"] = round(weighted / total, 3)
        for d in ("read", "write"):
            section = job.get(d) or {}
            if not section.get("iops"):
                continue
            # fio 3.40 keeps percentiles on completion latency (clat_*);
            # total-latency dicts carry no percentile object.
            clat_ns = section.get("clat_ns")
            clat = clat_ns or section.get("clat_us") or \
                section.get("lat_ns") or section.get("lat_us") or {}
            scale = 1.0 if clat_ns or section.get("lat_ns") else 1000.0
            pct = clat.get("percentile") or {}

            def p(key):
                v = pct.get(key)
                return round(v * scale / 1000.0, 3) if v is not None else None

            out["directions"][d] = {
                "iops": round(section["iops"], 3),
                "bw_bytes_per_s": round(section.get("bw_bytes") or 0, 1),
                "p50_us": p("50.000000"),
                "p95_us": p("95.000000"),
                "p99_us": p("99.000000"),
                "io_bytes": section.get("io_bytes"),
            }
        return out

    # -- probes -------------------------------------------------------------

    def probe(self, name: str, engine_spec: tuple,
              expect_backend: str = None):
        data = self.workdir / ("probe-" + name + ".bin")
        self.guard(data)
        with open(data, "wb") as fh:
            fh.truncate(64 << 20)
        job = "\n".join(self.job_lines(
            engine_spec=engine_spec, data=data, rw="read", bs="4k",
            iodepth=1, size="16m",
            extra=("time_based=1", "runtime=2"))) + "\n"
        rec = self.run_fio("probe-" + name, job,
                           expect_backend=expect_backend, timeout_s=60)
        data.unlink(missing_ok=True)
        if rec["status"] == "ok":
            return True, ""
        reason = "; ".join(rec["failures"])[:300]
        rec["status"] = "unavailable"
        rec["failures"] = []
        rec["unavailable_reason"] = reason
        return False, reason

    # -- preparation ----------------------------------------------------------

    def prep_file(self, case_id: str, size: str) -> pathlib.Path:
        """Dedicated per-case file: recorded psync sequential buffered write,
        fsync_on_close so measured runs start clean, then a full recorded
        warm-up read so every engine starts with the file page-cache
        resident (btrfs compress=zstd cold reads would otherwise dominate
        and order runs by cache warmth)."""
        data = self.workdir / (case_id + ".bin")
        self.guard(data)
        if data.exists():
            data.unlink()
        data.touch()
        # btrfs NODATACOW on our own file (no privileges needed): avoids
        # both compress=zstd:1 extent re-compression and COW allocation
        # churn during the F3 write mix (rehearsal: F3 collapsed to disk
        # speed on compressed/COW extents, ran at cache speed on +C).
        chattr = subprocess.run(["chattr", "+C", str(data)],
                                capture_output=True, text=True)
        if chattr.returncode == 0:
            self.nocompress_ok.append(case_id)
        else:
            self.nocompress_fail.append(
                "%s: %s" % (case_id, chattr.stderr.strip()[:120]))
        job = "\n".join(self.job_lines(
            engine_spec=("psync", ""), data=data, rw="write", bs="1M",
            iodepth=1, size=size,
            extra=("fsync_on_close=1",))) + "\n"
        rec = self.run_fio(case_id + "_prep", job, timeout_s=600)
        if rec["status"] != "ok":
            self.fatal.append("%s prep failed: %s" % (case_id,
                                                      rec["failures"]))
            return data
        # Warm-up: one full sequential read through an async engine so the
        # whole file lands in the page cache before any measured run.
        warm_spec = self.warm_engine
        job = "\n".join(self.job_lines(
            engine_spec=warm_spec, data=data, rw="read", bs="1M",
            iodepth=16, size=size)) + "\n"
        rec = self.run_fio(case_id + "_warm", job, timeout_s=600,
                           expect_backend=(warm_spec[1] or None))
        if rec["status"] != "ok":
            self.fatal.append("%s warmup failed: %s" % (case_id,
                                                        rec["failures"]))
        return data

    # -- A/A ------------------------------------------------------------------

    def aa_pairs(self, engine_map: dict) -> dict:
        aa = {}
        shape = next(c for c in CASES if c[0] == "F2")
        _, rw, bs, depth, size, extra = shape
        size = size or self.args.file_size
        for engine in [e.strip() for e in self.args.aa_engines.split(",")
                       if e.strip()]:
            if engine not in engine_map:
                self.fatal.append("A/A engine %s unknown" % engine)
                continue
            data = self.prep_file("aa-" + engine, size)
            reps = []
            for rep in (1, 2):
                job = "\n".join(self.job_lines(
                    engine_spec=engine_map[engine], data=data, rw=rw, bs=bs,
                    iodepth=depth, size=size,
                    extra=extra + ("time_based=1",
                                   "runtime=%d" % self.args.runtime,
                                   "ramp_time=%d" % self.args.ramp_time)
                )) + "\n"
                run_id = "aa-%s_rep%d" % (engine, rep)
                rec = self.run_fio(
                    run_id, job,
                    expect_backend=(engine_map[engine][1] or None))
                rec["case"], rec["engine"], rec["repeat"] = "AA", engine, rep
                reps.append(rec)
            # Deletion is deferred to the end of the whole matrix: unlinking
            # a file on this btrfs (discard=async) triggers TRIM work that
            # stalls the next prep run (rehearsal saw 1s -> 17s prep walls).
            self.finished_files.append(data)
            aa[engine] = self.aa_diff(engine, reps)
        return aa

    @staticmethod
    def aa_diff(engine: str, reps: list) -> dict:
        def get(rec, path):
            node = rec
            for key in path:
                node = node.get(key) if isinstance(node, dict) else None
                if node is None:
                    return None
            return node

        fields = {
            "iops": ("metrics", "directions", "read", "iops"),
            "p50_us": ("metrics", "directions", "read", "p50_us"),
            "p95_us": ("metrics", "directions", "read", "p95_us"),
            "p99_us": ("metrics", "directions", "read", "p99_us"),
            "mean_depth": ("metrics", "mean_depth"),
            "cpu_total_s": ("rusage", "cpu_user_s"),
        }
        diff = {"engine": engine,
                "run_ids": [r["run_id"] for r in reps],
                "status": "ok" if all(r["status"] == "ok" for r in reps)
                else "fail",
                "fields": {}}
        if diff["status"] != "ok":
            diff["error"] = "an A/A leg failed; no diff computed"
            return diff
        for name, path in fields.items():
            a, b = get(reps[0], path), get(reps[1], path)
            a = a + reps[0]["rusage"]["cpu_sys_s"] if name == "cpu_total_s" \
                else a
            b = b + reps[1]["rusage"]["cpu_sys_s"] if name == "cpu_total_s" \
                else b
            if a is None or b is None:
                diff["fields"][name] = {"rep1": a, "rep2": b,
                                        "delta_pct": None}
                continue
            mean = (a + b) / 2
            delta = abs(a - b) / mean * 100 if mean else None
            diff["fields"][name] = {"rep1": a, "rep2": b,
                                    "delta_pct": round(delta, 3)}
        return diff

    # -- matrix -----------------------------------------------------------------

    def execute(self, engine_map: dict, availability: dict) -> None:
        for case_id, rw, bs, depth, size, extra in CASES:
            size = size or self.args.file_size
            data = self.prep_file(case_id, size)
            timed = extra + ("time_based=1",
                             "runtime=%d" % self.args.runtime,
                             "ramp_time=%d" % self.args.ramp_time)
            for engine in ENGINE_ORDER:
                if engine in URING_ENGINES and not availability.get(engine):
                    self.runs.append({
                        "run_id": "%s_%s_skip" % (case_id, engine),
                        "case": case_id, "engine": engine,
                        "status": "unavailable",
                        "unavailable_reason": self.probe_reasons.get(
                            engine, "probe failed"),
                    })
                    continue
                for rep in range(1, self.args.repeats + 1):
                    job = "\n".join(self.job_lines(
                        engine_spec=engine_map[engine], data=data, rw=rw,
                        bs=bs, iodepth=depth, size=size,
                        extra=timed)) + "\n"
                    rec = self.run_fio(
                        "%s_%s_rep%d" % (case_id, engine, rep), job,
                        expect_backend=(engine_map[engine][1] or None))
                    rec["case"], rec["engine"], rec["repeat"] = (
                        case_id, engine, rep)
                    if rw.endswith("write") or "rw" in rw:
                        rec["settled"] = self.settle(data)
                        dev = self.scratch_disk()
                        if dev:
                            rec["device_wait"] = self.wait_disk_idle(dev)
            self.finished_files.append(data)

    def settle(self, data: pathlib.Path) -> bool:
        """fsync the file after a write-bearing measured run so the next
        engine/repeat starts from a clean (not dirty-throttled) state;
        keeps run order from biasing write-mix cases."""
        try:
            fd = os.open(data, os.O_RDWR)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            return True
        except OSError as exc:
            self.fatal.append("settle %s failed: %s" % (data, exc))
            return False

    @staticmethod
    def disk_io_ms(device: str) -> int:
        for line in pathlib.Path("/proc/diskstats").read_text().splitlines():
            fields = line.split()
            if len(fields) > 12 and fields[2] == device:
                return int(fields[12])
        return -1

    def scratch_disk(self) -> str | None:
        """Whole disk backing the scratch directory (e.g. sda3[/root] ->
        sda), for the inter-run quiesce; None when unresolvable."""
        if getattr(self, "_scratch_disk", "unset") != "unset":
            return self._scratch_disk
        self._scratch_disk = None
        out = subprocess.run(["findmnt", "-n", "-o", "SOURCE",
                              str(self.workdir)],
                             capture_output=True, text=True)
        source = out.stdout.strip()
        part = source.split("[", 1)[0].rsplit("/", 1)[-1]
        candidates = [part]
        for trim in range(len(part), 0, -1):
            cand = part[:trim].rstrip("p") or part[:trim]
            if pathlib.Path("/sys/block/" + cand).is_dir():
                self._scratch_disk = cand
                break
        return self._scratch_disk

    def wait_disk_idle(self, device: str, quiet_s: float = 4.0,
                       timeout_s: float = 180.0) -> dict:
        """Deterministic quiesce between write-mix runs: the budget SATA
        SSD behind build/scratch collapses to ~10MB/s writes once its SLC
        cache exhausts (rehearsal: a 4.3k-IOPS 14.6s F3 run right after
        ~2GB of accumulated case writes). Poll io_ms until the device has
        been busy-free for quiet_s, capped at timeout_s."""
        start = time.monotonic()
        deadline = start + timeout_s
        last_ms = self.disk_io_ms(device)
        last_t = start
        quiet_for = 0.0
        while time.monotonic() < deadline:
            time.sleep(0.25)
            now = time.monotonic()
            ms = self.disk_io_ms(device)
            if ms == last_ms:
                quiet_for = now - last_t
                if quiet_for >= quiet_s:
                    return {"waited_s": round(now - start, 1), "quiet": True}
            else:
                last_ms, last_t = ms, now
                quiet_for = 0.0
        return {"waited_s": round(timeout_s, 1), "quiet": False}

    def engine_specs(self) -> dict:
        ext_noring = (self.engines_dir / "fio-sluice-noring.so").resolve()
        ext_ring = (self.engines_dir / "fio-sluice-ring.so").resolve()
        return {
            ENGINE_PSYNC: ("psync", ""),
            ENGINE_IOURING: ("io_uring", ""),
            ENGINE_SLUICE_TP: ("external:%s" % ext_noring, "threadpool"),
            ENGINE_SLUICE_URING: ("external:%s" % ext_ring, "uring"),
        }

    # -- reporting -----------------------------------------------------------

    def finish(self, env: dict, aa: dict) -> int:
        manifest = {
            "schema": 1,
            "mode": "rehearsal" if self.args.rehearsal else "formal",
            "generated_unix": int(time.time()),
            "sluice_revision": self.revision,
            "argv": sys.argv[1:],
            "config": {
                "runtime_s": self.args.runtime,
                "ramp_time_s": self.args.ramp_time,
                "repeats": self.args.repeats,
                "file_size": self.args.file_size,
                "randseed": RANDSEED,
                "numjobs": 1,
                "direct": 0,
                "invalidate": 0,
                "buffer_pattern": "0x5a",
                "settle_fsync_after_write_runs": True,
                "cpu_set": self.args.cpu_set or None,
                "cases": [c[0] for c in CASES],
                "engines": list(ENGINE_ORDER),
            },
            "environment": env,
            "btrfs_nodtacow": {"ok": self.nocompress_ok,
                               "failed": self.nocompress_fail},
            "aa": aa,
            "runs": self.runs,
            "fatal": self.fatal,
            "wall_s": round(time.monotonic() - self.started, 1),
        }
        (self.results / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n")

        print("\n=== run summary ===")
        for r in self.runs:
            m = r.get("metrics") or {}
            dirs = m.get("directions") or {}
            parts = []
            for d, v in sorted(dirs.items()):
                parts.append("%s:%.0f" % (d, v["iops"]))
            iops = "+".join(parts) if parts else "-"
            print("%-38s %-12s iops[%s] wall=%ss"
                  % (r["run_id"], r.get("status", "?"), iops,
                     r.get("wall_s", "?")))
        for engine, diff in aa.items():
            if "error" in diff:
                print("A/A %s: %s" % (engine, diff["error"]))
                continue
            f = diff["fields"]
            i, p = f["iops"], f["p99_us"]
            print("A/A %s: iops %.0f/%.0f (delta %s%%), p99 %s/%s us "
                  "(delta %s%%)"
                  % (engine, i["rep1"], i["rep2"], i["delta_pct"],
                     p["rep1"], p["rep2"], p["delta_pct"]))
        print("wall=%ss -> %s/manifest.json"
              % (manifest["wall_s"], self.results))

        hard_engines = (ENGINE_PSYNC, ENGINE_SLUICE_TP)
        hard_fail = [r["run_id"] for r in self.runs
                     if r.get("engine") in hard_engines
                     and r.get("status") == "fail"]
        hard_fail += ["aa-%s" % e for e, d in aa.items()
                      if d.get("status") != "ok" and e in hard_engines]
        if self.fatal or hard_fail:
            print("FATAL:", "; ".join(self.fatal))
            print("HARD FAILURES (psync/threadpool):", "; ".join(hard_fail))
            return 1
        skipped = sum(1 for r in self.runs
                      if r.get("status") == "unavailable")
        if skipped:
            print("UNAVAILABLE records: %d (uring arms skipped, "
                  "exit 0 per contract)" % skipped)
        return 0


def main() -> int:
    args = parse_args()
    root = pathlib.Path(__file__).resolve().parents[1]
    m = Matrix(args, root)
    m.workdir.mkdir(parents=True, exist_ok=True)
    m.results.mkdir(parents=True, exist_ok=True)
    m.jobs_dir.mkdir(parents=True, exist_ok=True)

    version = subprocess.run([args.fio, "--version"], capture_output=True,
                             text=True)
    if version.returncode != 0 or version.stdout.strip() != FIO_VERSION:
        print("FATAL: host fio is %r, matrix pinned to %s"
              % (version.stdout.strip(), FIO_VERSION))
        return 1
    specs = m.engine_specs()
    for engine in (ENGINE_SLUICE_TP, ENGINE_SLUICE_URING):
        path = pathlib.Path(specs[engine][0].split(":", 1)[1])
        if not path.is_file():
            print("FATAL: engine %s missing: %s" % (engine, path))
            return 1

    env = m.environment_snapshot()
    print(json.dumps(env, indent=2, sort_keys=True))

    availability = {}
    for engine in (ENGINE_IOURING, ENGINE_SLUICE_URING):
        ok, reason = m.probe(engine, specs[engine],
                             specs[engine][1] or None)
        availability[engine] = ok
        m.probe_reasons[engine] = reason
        print("probe %s: %s" % (engine, "available" if ok
                                else "UNAVAILABLE - " + reason))
    if availability.get(ENGINE_IOURING):
        m.warm_engine = specs[ENGINE_IOURING]
    elif availability.get(ENGINE_SLUICE_URING):
        m.warm_engine = specs[ENGINE_SLUICE_URING]
    else:
        m.warm_engine = specs[ENGINE_SLUICE_TP]

    aa = m.aa_pairs(specs)
    m.execute(specs, availability)

    # All test files removed only now, after the last measured run.
    for path in m.finished_files:
        path.unlink(missing_ok=True)

    leftovers = subprocess.run(["pgrep", "-x", "fio"],
                               capture_output=True, text=True)
    stray = [ln for ln in leftovers.stdout.splitlines() if ln
             and int(ln.split()[0]) != os.getpid()]
    if stray:
        m.fatal.append("leftover fio processes: %s" % stray)
    for leftover in sorted(m.workdir.iterdir()):
        m.fatal.append("scratch leftover: %s" % leftover)
        leftover.unlink()
    try:
        m.workdir.rmdir()
    except OSError:
        pass

    return m.finish(env, aa)


if __name__ == "__main__":
    sys.exit(main())

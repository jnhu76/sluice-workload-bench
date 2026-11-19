"""Serial session orchestration for the cost-attribution campaign.

One Session owns one results/attribution/<session-id>/ tree. Modes never mix
inside a run: clean/pmu/record/trace each produce their own run directories
with verbatim artifacts. Every run records engine load line, tripwire scan,
rusage and argv; a failing run is retried exactly once with the original
kept (discarded: true) -- nothing is silently dropped.

fio invocation idioms (prep/guard/rusage/tripwires/load-line validation)
are reused from tools/run_fio_matrix.py by import; that file is the merged
Phase-1 harness and the single source of those rules.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import resource
import shutil
import subprocess
import sys
import time

TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))
from run_fio_matrix import LOAD_RE, TRIPWIRES  # noqa: E402

from . import capability as cap  # noqa: E402

RANDSEED = 81
FIO_VERSION = "fio-3.40"

# Frozen cells (COST-ATTRIBUTION-METHOD §2). C2 is the Option-C cross-check
# shape: identical to A2 but ramp_time=0, no time_based, fixed number_ios.
CELLS = {
    "A1": {"rw": "randread", "bs": "4k", "iodepth": 1, "kind": "time_based"},
    "A2": {"rw": "randread", "bs": "4k", "iodepth": 16, "kind": "time_based"},
    "A3": {"rw": "read", "bs": "64k", "iodepth": 4, "kind": "time_based"},
    "A4": {"rw": "randread", "bs": "4k", "iodepth": 4, "kind": "time_based"},
    "C2": {"rw": "randread", "bs": "4k", "iodepth": 16, "kind": "number_ios",
           "number_ios": 5_000_000},
}
DEFAULT_CELLS = ("A1", "A2", "A3", "A4")
DEFAULT_ENGINES = ("io_uring", "sluice-uring", "sluice-threadpool")

ENGINES_DIR = TOOLS_DIR.parent / "build" / "linux" / "x86_64" / "release"


def engine_specs(diag: bool = False) -> dict:
    """Engine map. diag=True swaps the external engines to their symbol
    twins (byte-identical .text, kept symtab + -g) for perf record."""
    noring = ENGINES_DIR / ("fio-sluice-noring%s.so" % ("-sym" if diag else ""))
    ring = ENGINES_DIR / ("fio-sluice-ring%s.so" % ("-sym" if diag else ""))
    return {
        "psync": ("psync", "", None),
        "io_uring": ("io_uring", "", None),
        "sluice-threadpool": ("external:%s" % noring, "threadpool", noring),
        "sluice-uring": ("external:%s" % ring, "uring", ring),
    }


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def text_section_sha256(path: pathlib.Path) -> str | None:
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".text") as tmp:
        proc = subprocess.run(
            ["objcopy", "-O", "binary", "--only-section=.text",
             str(path), tmp.name], capture_output=True)
        if proc.returncode != 0:
            return None
        return sha256_file(pathlib.Path(tmp.name))


class EnvironmentDrift(RuntimeError):
    pass


class Session:
    def __init__(self, results_dir: pathlib.Path, scratch_dir: pathlib.Path,
                 cpu_set: str = "0-9", runtime_s: int = 10,
                 ramp_s: int = 2, file_size: str = "1G", fio: str = "fio",
                 fio_diag: str | None = None):
        self.root = results_dir.resolve()
        self.runs_dir = self.root / "runs"
        self.analysis_dir = self.root / "analysis"
        self.scratch = scratch_dir.resolve()
        self.workdir = self.scratch / "attribution"
        self.cpu_set = cpu_set
        self.runtime_s = runtime_s
        self.ramp_s = ramp_s
        self.file_size = file_size
        self.fio = fio
        self.fio_diag = fio_diag or str(
            TOOLS_DIR.parent / "build" / "fio" / "upstream" / "fio")
        self.revision = (TOOLS_DIR.parent / "SLUICE_REVISION").read_text().strip()
        self.manifest_runs: list[dict] = []
        self.prepared_files: dict[str, pathlib.Path] = {}

    # -- session bootstrap ------------------------------------------------

    def ensure_layout(self) -> None:
        for d in (self.root, self.runs_dir, self.analysis_dir, self.workdir):
            d.mkdir(parents=True, exist_ok=True)

    def write_protocol(self, argv: list) -> None:
        src = TOOLS_DIR.parent / "docs" / "fio-integration" / \
            "COST-ATTRIBUTION-METHOD.md"
        win = TOOLS_DIR.parent / "docs" / "fio-integration" / \
            "MEASUREMENT-WINDOW-CONTRACT.md"
        parts = ["# Session protocol (frozen pre-registration)\n",
                 "- generated: %s" % time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                 "- argv: %s" % " ".join(argv),
                 "- sluice_revision: %s" % self.revision,
                 "- fio: %s" % self.fio,
                 "- runtime/ramp/size: %ss/%ss/%s"
                 % (self.runtime_s, self.ramp_s, self.file_size),
                 "- cpu_set: %s" % (self.cpu_set or "none"),
                 "\n## Frozen method\n", src.read_text(),
                 "\n## Frozen window contract\n", win.read_text()]
        (self.root / "PROTOCOL.md").write_text("\n".join(parts))

    def write_environment_and_capability(self) -> dict:
        env = cap.collect_environment()
        (self.root / "environment.json").write_text(
            json.dumps(env, indent=2, sort_keys=True) + "\n")
        matrix = cap.probe_all()
        (self.root / "capability.json").write_text(
            json.dumps(matrix, indent=2, sort_keys=True) + "\n")
        return matrix

    def write_binary_manifest(self) -> dict:
        rows = {}
        for name, spec in engine_specs().items():
            path = spec[2]
            if path is None:
                continue
            sym_twin = pathlib.Path(str(path).replace(
                "fio-sluice-", "fio-sluice-").replace(".so", "-sym.so")) \
                if "-sym" not in str(path) else path
            row = {"path": str(path), "sha256": sha256_file(path)}
            twin = pathlib.Path(str(path)[:-3] + "-sym.so")
            if twin.exists():
                row["sym_twin"] = {"path": str(twin),
                                   "sha256": sha256_file(twin)}
                t_rel = text_section_sha256(path)
                t_sym = text_section_sha256(twin)
                row["text_section_match"] = (t_rel == t_sym
                                             and t_rel is not None)
                row["text_sha256"] = t_rel
            rows[name] = row
        diag_fio = pathlib.Path(self.fio_diag)
        manifest = {
            "schema": 1,
            "sluice_revision": self.revision,
            "fio_baseline": {"path": shutil.which(self.fio) or self.fio,
                             "version": subprocess.run(
                                 [self.fio, "--version"],
                                 capture_output=True, text=True).stdout
                             .strip(),
                             "stripped": True},
            "fio_diag": ({"path": str(diag_fio),
                          "sha256": sha256_file(diag_fio),
                          "text_sha256": text_section_sha256(diag_fio)}
                         if diag_fio.exists() else None),
            "engines": rows,
            "note": "record mode uses fio_diag + sym twins; .text section "
                    "hashes prove code identity with the release baseline",
        }
        (self.root / "binary-manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        return manifest

    # -- environment gates --------------------------------------------------

    def gate_environment(self) -> None:
        load = float(pathlib.Path("/proc/loadavg").read_text().split()[0])
        if load > 1.0:
            # Pre-registered drift rule: wait out short spikes, refuse long.
            for _ in range(12):
                time.sleep(5)
                load = float(pathlib.Path("/proc/loadavg")
                             .read_text().split()[0])
                if load <= 1.0:
                    break
            if load > 1.0:
                raise EnvironmentDrift("1min loadavg %.2f > 1.0" % load)
        for pat in ("fio", "perf", "strace", "bpftrace"):
            out = subprocess.run(["pgrep", "-x", pat],
                                 capture_output=True, text=True)
            if out.stdout.strip():
                raise EnvironmentDrift(
                    "competing %s processes: %s" % (pat, out.stdout.strip()))

    # -- fio plumbing -------------------------------------------------------

    def guard(self, path: pathlib.Path) -> None:
        resolved = path.resolve()
        if self.workdir != resolved and self.workdir not in resolved.parents:
            raise RuntimeError("refusing non-scratch target %s" % resolved)
        if resolved.exists() and not resolved.is_file():
            raise RuntimeError("refusing non-regular target %s" % resolved)

    def job_lines(self, engine: str, data: pathlib.Path, cell_id: str,
                  diag: bool = False, trace_short: bool = False,
                  ramp_override: int | None = None) -> list:
        """Job text. ramp_override forces ramp_time for instrumented runs:
        the PMU window spans the whole process, and fio's io_bytes counter
        only covers the post-ramp window (verified live in
        session-000-smoke: io_bytes/bs == iops*runtime exactly), so
        pmu/record/trace MUST run ramp_time=0 or the denominator would
        silently drop ramp-period ios (MEASUREMENT-WINDOW-CONTRACT §2)."""
        ioengine, backend, _path = engine_specs(diag)[engine]
        cell = CELLS[cell_id]
        lines = ["[job]", "ioengine=" + ioengine,
                 "filename=" + str(data)]
        if backend:
            lines.append("sluice_backend=" + backend)
        lines += ["rw=" + cell["rw"], "bs=" + cell["bs"],
                  "size=%s" % (self.file_size if not trace_short else "256m"),
                  "iodepth=%d" % cell["iodepth"], "numjobs=1", "direct=0",
                  "thread=1", "group_reporting=1",
                  "randseed=%d" % RANDSEED, "invalidate=0",
                  "buffer_pattern=0x5a"]
        if cell["kind"] == "time_based":
            runtime = 3 if trace_short else self.runtime_s
            ramp = 0 if trace_short else self.ramp_s
            if ramp_override is not None:
                ramp = ramp_override
            lines += ["time_based=1", "runtime=%d" % runtime,
                      "ramp_time=%d" % ramp]
        else:
            lines += ["number_ios=%d" % cell["number_ios"],
                      "ramp_time=0"]
        return lines

    def prep_file(self, label: str, size: str) -> pathlib.Path:
        """Same discipline as run_fio_matrix.prep_file: dedicated file,
        NODATACOW, recorded psync write + fsync_on_close, recorded async
        warm-up read (page-cache-resident start for every engine)."""
        data = self.workdir / (label + ".bin")
        self.guard(data)
        if data.exists():
            data.unlink()
        data.touch()
        subprocess.run(["chattr", "+C", str(data)], capture_output=True)
        def lines(engine, rw, bs, extra=()):
            ioengine, backend, _ = engine_specs()[engine]
            out = ["[job]", "ioengine=" + ioengine, "filename=" + str(data)]
            if backend:
                out.append("sluice_backend=" + backend)
            return out + ["rw=" + rw, "bs=" + bs, "size=" + size,
                          "iodepth=%d" % (16 if rw == "read" else 1),
                          "numjobs=1", "direct=0", "thread=1",
                          "group_reporting=1", "randseed=%d" % RANDSEED,
                          "invalidate=0", "buffer_pattern=0x5a"] + list(extra)
        self.run_fio("prep-%s-write" % label,
                     "\n".join(lines("psync", "write", "1m",
                                     ("fsync_on_close=1",))) + "\n",
                     expect_backend=None, timeout_s=900)
        self.run_fio("prep-%s-warm" % label,
                     "\n".join(lines("io_uring", "read", "1m")) + "\n",
                     expect_backend=None, timeout_s=900)
        self.prepared_files[label] = data
        return data

    def _record_run(self, run_id: str, run_dir: pathlib.Path, argv: list,
                    proc: subprocess.CompletedProcess, wall: float,
                    rusage_before, rusage_after, expect_backend,
                    extra: dict | None = None) -> dict:
        after, before = rusage_after, rusage_before
        record = {
            "run_id": run_id,
            "argv": argv,
            "fio_exit": proc.returncode,
            "wall_s": round(wall, 3),
            "rusage": {
                "cpu_user_s": round(after.ru_utime - before.ru_utime, 6),
                "cpu_sys_s": round(after.ru_stime - before.ru_stime, 6),
                "ctx_voluntary": after.ru_nvcsw - before.ru_nvcsw,
                "ctx_involuntary": after.ru_nivcsw - before.ru_nivcsw,
            },
            "stderr_tail": proc.stderr.splitlines()[-8:],
            "started_unix": int(time.time()),
        }
        load = LOAD_RE.search(proc.stderr)
        record["engine_load"] = list(load.groups()) if load else None
        diag = [ln.strip() for ln in proc.stderr.splitlines()
                if "fio-sluice:" in ln and not LOAD_RE.match(ln.strip())]
        record["engine_diag"] = diag
        failures = ["unexpected engine diagnostic: " + ln for ln in diag]
        for line in (ln.strip() for ln in proc.stderr.splitlines()):
            if any(f in line for f in TRIPWIRES):
                failures.append("tripwire diagnostic: " + line)
        if proc.returncode != 0:
            failures.append("exit %d" % proc.returncode)
        if expect_backend is not None:
            if record["engine_load"] is None:
                failures.append("engine load line missing")
            else:
                rev, _prof, _src, backend, _d, _w = record["engine_load"]
                if backend != expect_backend:
                    failures.append("backend %s != requested %s"
                                    % (backend, expect_backend))
                if rev != self.revision:
                    failures.append("engine revision %s != pinned %s"
                                    % (rev, self.revision))
        record["failures"] = failures
        record["status"] = "fail" if failures else "ok"
        if extra:
            record.update(extra)
        (run_dir / "meta.json").write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n")
        self.manifest_runs.append(
            {"run_id": run_id, "status": record["status"],
             "run_dir": str(run_dir.relative_to(self.root))})
        (self.root / "runs-manifest.json").write_text(
            json.dumps(self.manifest_runs, indent=2) + "\n")
        return record

    def _save_fio_output(self, run_dir: pathlib.Path, stdout: str) -> bool:
        (run_dir / "fio-result.json").write_text(stdout)
        start = stdout.find("{")
        if start < 0:
            return False
        try:
            doc = json.loads(stdout[start:])
            job = doc["jobs"][0]
            return bool(job.get("read", {}).get("io_bytes"))
        except (ValueError, KeyError, IndexError, TypeError):
            return False
        return True

    def run_fio(self, run_id: str, job_text: str, expect_backend=None,
                timeout_s=None, wrap: list | None = None, diag: bool = False,
                stdout_to_file: str | None = None, extra: dict | None = None,
                trace_short: bool = False) -> dict:
        """One serial measured run. wrap = argv prefix (perf/strace) placed
        between taskset and fio; its own outputs go to the run dir."""
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "fio-job.fio").write_text(job_text)
        for target in (pathlib.Path(ln.split("=", 1)[1])
                       for ln in job_text.splitlines()
                       if ln.startswith("filename=")):
            self.guard(target)
        self.gate_environment()
        argv: list = []
        if self.cpu_set:
            argv += ["taskset", "-c", self.cpu_set]
        fio_bin = self.fio_diag if diag else self.fio
        if wrap:
            argv += wrap
        argv += [fio_bin, "--eta=never", "--output-format=json"]
        if stdout_to_file:
            argv += ["--output=" + stdout_to_file]
        argv += [str(run_dir / "fio-job.fio")]
        if timeout_s is None:
            timeout_s = self.runtime_s + self.ramp_s + 120
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        t0 = time.monotonic()
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout_s, cwd=str(run_dir))
        wall = time.monotonic() - t0
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        stdout = proc.stdout
        if stdout_to_file:
            out_path = run_dir / stdout_to_file
            stdout = out_path.read_text() if out_path.exists() else ""
        parsed_ok = self._save_fio_output(run_dir, stdout)
        rec = self._record_run(run_id, run_dir, argv, proc, wall,
                               before, after, expect_backend, extra)
        if rec["status"] == "ok" and not parsed_ok:
            rec["failures"].append("fio JSON missing/empty")
            rec["status"] = "fail"
            (run_dir / "meta.json").write_text(
                json.dumps(rec, indent=2, sort_keys=True) + "\n")
        return rec

    def run_with_retry(self, attempt_id: str, **kwargs) -> dict:
        """Pre-registered discard rule: a failed run is kept with
        discarded=true and retried exactly once."""
        rec = self.run_fio(attempt_id, **kwargs)
        if rec["status"] != "ok":
            for artifact in self.runs_dir.glob(attempt_id + "/meta.json"):
                meta = json.loads(artifact.read_text())
                meta["discarded"] = True
                meta["discard_reason"] = rec["failures"]
                artifact.write_text(json.dumps(meta, indent=2,
                                               sort_keys=True) + "\n")
                self.manifest_runs[-1]["status"] = "discarded"
            retry_id = attempt_id + "r"
            rec2 = self.run_fio(retry_id, **kwargs)
            if rec2["status"] != "ok":
                rec2["arm_blocked"] = True
            return rec2
        return rec

    # -- modes --------------------------------------------------------------

    def mode_clean(self, cells, engines, repeats: int = 3) -> None:
        for cell in cells:
            data = self.prep_file(cell, self.file_size)
            for engine in engines:
                for rep in range(1, repeats + 1):
                    job = "\n".join(self.job_lines(engine, data, cell)) + "\n"
                    self.run_with_retry(
                        "%s__%s__clean__rep%d" % (cell, engine, rep),
                        job_text=job,
                        expect_backend=engine_specs()[engine][1] or None)

    def mode_pmu(self, cells, engines, repeats: int = 2,
                 with_aux: bool = True) -> None:
        from .capability import AUX1_EVENTS, AUX2_EVENTS, MAIN_EVENTS
        for cell in cells:
            data = self.prep_file(cell, self.file_size)
            for engine in engines:
                for rep in range(1, repeats + 1):
                    job = "\n".join(self.job_lines(
                        engine, data, cell, ramp_override=0)) + "\n"
                    run_id = "%s__%s__pmu__rep%d" % (cell, engine, rep)
                    wrap = ["perf", "stat", "-x,",
                            "-o", str(self.runs_dir / run_id /
                                      "perf-stat.raw.txt"),
                            "-e", ",".join(MAIN_EVENTS)]
                    self.run_with_retry(run_id, job_text=job, wrap=wrap,
                                        expect_backend=engine_specs()
                                        [engine][1] or None)
                if with_aux:
                    job = "\n".join(self.job_lines(
                        engine, data, cell, ramp_override=0)) + "\n"
                    for name, events in (("aux1", AUX1_EVENTS),
                                         ("aux2", AUX2_EVENTS)):
                        run_id = "%s__%s__pmu-%s__rep1" % (cell, engine, name)
                        wrap = ["perf", "stat", "-x,",
                                "-o", str(self.runs_dir / run_id /
                                          "perf-stat.raw.txt"),
                                "-e", ",".join(events)]
                        self.run_with_retry(run_id, job_text=job, wrap=wrap,
                                            expect_backend=engine_specs()
                                            [engine][1] or None)

    def mode_record(self, cells, engines, freq: int = 997) -> None:
        for cell in cells:
            data = self.prep_file(cell, self.file_size)
            for engine in engines:
                job = "\n".join(self.job_lines(engine, data, cell,
                                               diag=True,
                                               ramp_override=0)) + "\n"
                run_id = "%s__%s__record__rep1" % (cell, engine)
                run_dir = self.runs_dir / run_id
                run_dir.mkdir(parents=True, exist_ok=True)
                wrap = ["perf", "record", "-o", str(run_dir / "perf.data"),
                        "-e", "cycles:u", "-F", str(freq),
                        "--call-graph", "dwarf,16384", "--"]
                rec = self.run_with_retry(
                    run_id, job_text=job, wrap=wrap, diag=True,
                    expect_backend=engine_specs(diag=True)[engine][1] or None,
                    extra={"diag_binary": True})
                if rec["status"] != "ok":
                    continue
                data_file = run_dir / "perf.data"
                header = subprocess.run(
                    ["perf", "report", "-i", str(data_file), "--header",
                     "--stdio"], capture_output=True, text=True)
                (run_dir / "perf-header.txt").write_text(header.stdout)
                report = subprocess.run(
                    ["perf", "report", "-i", str(data_file), "--stdio",
                     "--no-children", "--percent-limit", "0.2"],
                    capture_output=True, text=True)
                (run_dir / "perf-report.txt").write_text(report.stdout)
                callgraph = subprocess.run(
                    ["perf", "report", "-i", str(data_file), "--stdio",
                     "--percent-limit", "1", "-g", "none"],
                    capture_output=True, text=True)
                (run_dir / "perf-report-flat.txt").write_text(
                    callgraph.stdout)
                (run_dir / "perf.data.sha256").write_text(
                    sha256_file(data_file) + "\n")

    def mode_trace(self, cells, engines) -> None:
        """strace -c syscall counting on short runs + bare controls +
        io_uring_enter argument-shape capture. All outputs carry the
        STRACE_DIAGNOSTIC label; overhead is measured, never assumed."""
        for cell in cells:
            data = self.prep_file(cell + "-trace", "256m")
            for engine in engines:
                job = "\n".join(self.job_lines(engine, data, cell,
                                               trace_short=True)) + "\n"
                # bare control first
                run_id = "%s__%s__trace-bare__rep1" % (cell, engine)
                self.run_with_retry(
                    run_id, job_text=job,
                    expect_backend=engine_specs()[engine][1] or None)
                # straced counting run
                run_id = "%s__%s__trace-straced__rep1" % (cell, engine)
                run_dir = self.runs_dir / run_id
                run_dir.mkdir(parents=True, exist_ok=True)
                wrap = ["strace", "-c", "-f", "-w",
                        "-o", str(run_dir / "strace-summary.txt")]
                rec = self.run_with_retry(
                    run_id, job_text=job, wrap=wrap,
                    expect_backend=engine_specs()[engine][1] or None)
                if rec["status"] == "ok":
                    (run_dir / "event-loss.json").write_text(json.dumps(
                        {"mode": "strace-count", "loss_concept":
                         "strace -c is exhaustive for traced syscalls; "
                         "loss N/A", "bpftrace_lost_events": None},
                        indent=2) + "\n")
                # submission-shape capture (only for enter-bearing engines)
                if engine in ("io_uring", "sluice-uring"):
                    shape_cell = dict(CELLS[cell])
                    shape_cell.update(kind="number_ios", number_ios=200_000)
                    lines = self.job_lines(engine, data, cell,
                                           trace_short=True)
                    lines = [ln for ln in lines
                             if not ln.startswith(("time_based", "runtime=",
                                                   "ramp_time="))]
                    lines += ["number_ios=200000", "size=256m"]
                    run_id = "%s__%s__trace-shape__rep1" % (cell, engine)
                    run_dir = self.runs_dir / run_id
                    run_dir.mkdir(parents=True, exist_ok=True)
                    wrap = ["strace", "-f", "-e", "trace=io_uring_enter",
                            "-s", "0", "-o",
                            str(run_dir / "enter-shape.txt")]
                    self.run_with_retry(
                        run_id, job_text="\n".join(lines) + "\n", wrap=wrap,
                        expect_backend=engine_specs()[engine][1] or None,
                        timeout_s=600)

    def mode_crosscheck(self, engines) -> None:
        """Option C: fixed number_ios, ramp 0 (window contract cross-check).
        A2 shape only, per pre-registration."""
        data = self.prep_file("C2", self.file_size)
        for engine in engines:
            job = "\n".join(self.job_lines(engine, data, "C2")) + "\n"
            self.run_with_retry(
                "C2__%s__clean__rep1" % engine, job_text=job,
                expect_backend=engine_specs()[engine][1] or None,
                timeout_s=600)
            run_id = "C2__%s__pmu__rep1" % engine
            wrap = ["perf", "stat", "-x,",
                    "-o", str(self.runs_dir / run_id /
                              "perf-stat.raw.txt"),
                    "-e", ",".join(cap.MAIN_EVENTS)]
            self.run_with_retry(run_id, job_text=job, wrap=wrap,
                                expect_backend=engine_specs()[engine][1]
                                or None, timeout_s=600)

    # -- archive ----------------------------------------------------------

    def write_sha_manifest(self) -> None:
        lines = []
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and "sha256-manifest" not in path.name:
                rel = path.relative_to(self.root)
                if path.name == "perf.data":  # large: digest only
                    lines.append("%s  %s" % (sha256_file(path), rel))
                    continue
                lines.append("%s  %s" % (sha256_file(path), rel))
        (self.root / "sha256-manifest.txt").write_text("\n".join(lines)
                                                       + "\n")

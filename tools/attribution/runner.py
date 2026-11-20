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
    # Baseline U4 (deep-queue endpoint): same 4k randread shape as A1/A2/A4
    # at iodepth 64. Added for the userspace-cost baseline session only;
    # DEFAULT_CELLS deliberately stays A1-A4 so existing grids are
    # unchanged.
    "A5": {"rw": "randread", "bs": "4k", "iodepth": 64, "kind": "time_based"},
    # Option C cross-check: working set stays 1G (same as A2) while the
    # total transferred is 20G == 5.24M ios, giving a long, startup-
    # amortized window (fio semantics: size = working set, io_size = total;
    # number_ios is NOT usable here -- size bounds total bytes when
    # time_based is off, which capped the first C2 attempt at 262144 ios
    # and contaminated it with ~60% startup cost).
    "C2": {"rw": "randread", "bs": "4k", "iodepth": 16, "kind": "io_size",
           "io_size": "20G"},
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


def is_stripped(path: pathlib.Path) -> bool | None:
    """Ask `file` instead of hardcoding a boolean into the manifest."""
    proc = subprocess.run(["file", "-b", str(path)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    text = proc.stdout.lower()
    if "not stripped" in text:
        return False
    if "stripped" in text:
        return True
    return None


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
                 fio_diag: str | None = None, sudo_pw: str | None = None):
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
        # Owner-granted root session support: the password lives only in
        # this process's memory and is piped to `sudo -S` stdin; it is
        # never written to argv, meta.json, the evidence tree, or git.
        self.sudo_pw = sudo_pw
        self.fio_diag = fio_diag or str(
            TOOLS_DIR.parent / "build" / "fio" / "upstream" / "fio")
        self.revision = (TOOLS_DIR.parent / "SLUICE_REVISION").read_text().strip()
        # Modes run as separate CLI invocations against the same session:
        # reload the manifest so it accumulates across invocations instead
        # of being silently truncated per mode.
        _manifest = self.root / "runs-manifest.json"
        self.manifest_runs: list = json.loads(_manifest.read_text()) \
            if _manifest.exists() else []
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
        # Root modes measure through `sudo -S`; the capability probe must
        # run under the same elevation or it would record UNAVAILABLE for
        # capabilities the session actually measured.
        matrix = cap.probe_all(sudo_pw=self.sudo_pw)
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
        fio_baseline_path = pathlib.Path(shutil.which(self.fio) or self.fio)
        manifest = {
            "schema": 1,
            "sluice_revision": self.revision,
            "fio_baseline": {
                "path": str(fio_baseline_path),
                "version": subprocess.run(
                    [self.fio, "--version"],
                    capture_output=True, text=True).stdout.strip(),
                "sha256": (sha256_file(fio_baseline_path)
                           if fio_baseline_path.is_file() else None),
                "stripped": (is_stripped(fio_baseline_path)
                             if fio_baseline_path.is_file() else None)},
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

    def gate_environment(self, prep_run: bool = False,
                         allow_bpf: bool = False) -> None:
        """Foreign-process gate always; loadavg gate only before measured
        runs (session-001 receipt: our own 1G prep write/warm-read pushes
        1min loadavg to ~1.5 for a minute -- self-induced churn, not drift;
        refusing on it aborted the chain mid-session). allow_bpf=True for
        trace-bpf runs: the bpftrace process IS the observer under test."""
        for pat in ("fio", "perf", "strace", "bpftrace"):
            if pat == "bpftrace" and allow_bpf:
                continue
            out = subprocess.run(["pgrep", "-x", pat],
                                 capture_output=True, text=True)
            if out.stdout.strip():
                raise EnvironmentDrift(
                    "competing %s processes: %s" % (pat, out.stdout.strip()))
        if prep_run:
            return
        load = float(pathlib.Path("/proc/loadavg").read_text().split()[0])
        if load > 2.5:
            for _ in range(12):
                time.sleep(5)
                load = float(pathlib.Path("/proc/loadavg")
                             .read_text().split()[0])
                if load <= 2.5:
                    break
            if load > 2.5:
                raise EnvironmentDrift("1min loadavg %.2f > 2.5" % load)

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
        elif cell["kind"] == "io_size":
            lines += ["io_size=%s" % cell["io_size"], "ramp_time=0"]
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
        write_rec = self.run_fio(
            "prep-%s-write" % label,
            "\n".join(lines("psync", "write", "1m",
                            ("fsync_on_close=1",))) + "\n",
            expect_backend=None, timeout_s=900)
        if write_rec["status"] != "ok":
            raise EnvironmentDrift(
                "prep write failed for %s: %s"
                % (label, write_rec.get("failures")))
        warm_rec = self.run_fio(
            "prep-%s-warm" % label,
            "\n".join(lines("io_uring", "read", "1m")) + "\n",
            expect_backend=None, timeout_s=900)
        if warm_rec["status"] != "ok":
            raise EnvironmentDrift(
                "prep warm-read failed for %s: %s"
                % (label, warm_rec.get("failures")))
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
        # Full stderr is kept for S4 (stderr RESEARCH_COUNTERS line vs
        # SLUICE_FIO_RESEARCH_COUNTERS_FILE) and for tripwire auditing.
        # stderr_tail stays for backwards-compatible manifests.
        (run_dir / "stderr.txt").write_text(proc.stderr)
        (run_dir / "stdout.txt").write_text(proc.stdout)
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
            # Write-only prep runs legitimately have no read.io_bytes.
            for direction in ("read", "write"):
                if (job.get(direction) or {}).get("io_bytes"):
                    return True
            return False
        except (ValueError, KeyError, IndexError, TypeError):
            return False

    def run_fio(self, run_id: str, job_text: str, expect_backend=None,
                timeout_s=None, wrap: list | None = None, diag: bool = False,
                stdout_to_file: str | None = None, extra: dict | None = None,
                trace_short: bool = False, own_bpf: bool = False) -> dict:
        """One serial measured run. wrap = argv prefix (perf/strace) placed
        between taskset and fio; its own outputs go to the run dir."""
        run_dir = self.runs_dir / run_id
        # Keep failed receipts: a re-invocation of the same run_id (e.g. the
        # record-mode callgraph repair) must not overwrite them silently.
        prior_meta = run_dir / "meta.json"
        if run_dir.exists() and prior_meta.exists():
            try:
                if json.loads(prior_meta.read_text()).get("status") != "ok":
                    keep = run_dir.with_name(run_dir.name + "-failed")
                    suffix = 1
                    while keep.exists():
                        keep = run_dir.with_name(
                            "%s-failed%d" % (run_dir.name, suffix))
                        suffix += 1
                    run_dir.rename(keep)
            except (ValueError, OSError):
                pass
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "fio-job.fio").write_text(job_text)
        for target in (pathlib.Path(ln.split("=", 1)[1])
                       for ln in job_text.splitlines()
                       if ln.startswith("filename=")):
            self.guard(target)
        self.gate_environment(prep_run=run_id.startswith("prep-"),
                              allow_bpf=own_bpf)
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
        needs_sudo = bool(wrap) and "sudo" in wrap
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s,
            cwd=str(run_dir),
            input=(self.sudo_pw + "\n") if (needs_sudo and self.sudo_pw)
            else None)
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
            # Keep runs-manifest consistent with the on-disk meta.
            for entry in reversed(self.manifest_runs):
                if entry.get("run_id") == run_id:
                    entry["status"] = "fail"
                    break
            (self.root / "runs-manifest.json").write_text(
                json.dumps(self.manifest_runs, indent=2) + "\n")
        return rec

    def accepted_run_dir(self, rec: dict) -> pathlib.Path:
        """The directory that holds the ACCEPTED receipt. On retry this is
        `<attempt_id>r`, not the caller's first-attempt path; post-run
        artifacts (perf reports, bpftrace receipts, strace annotations)
        must be written there or the accepted row has no instrumentation."""
        return self.runs_dir / rec["run_id"]

    def _bpf_source_dir(self, attempt_dir: pathlib.Path) -> pathlib.Path:
        """The attempt dir may have been renamed to `<id>-failed*` when
        run_fio re-used a previously failed run_id. The live bpftrace
        files moved with it; locate the renamed directory instead of
        copying from a path that no longer exists."""
        if (attempt_dir / "bpftrace.stdout.txt").exists():
            return attempt_dir
        candidates = [p for p in self.runs_dir.glob(
            attempt_dir.name + "-failed*")
            if (p / "bpftrace.stdout.txt").exists()]
        if candidates:
            return max(candidates, key=lambda p: p.stat().st_mtime)
        return attempt_dir

    def _mark_missing_artifact(self, rec: dict, artifact: str) -> None:
        run_dir = self.accepted_run_dir(rec)
        meta_path = run_dir / "meta.json"
        if not meta_path.exists():
            return
        meta = json.loads(meta_path.read_text())
        meta.setdefault("failures", []).append(
            "%s missing after a successful run" % artifact)
        meta["status"] = "fail"
        meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True)
                             + "\n")
        for entry in reversed(self.manifest_runs):
            if entry.get("run_id") == rec["run_id"]:
                entry["status"] = "fail"
                break
        (self.root / "runs-manifest.json").write_text(
            json.dumps(self.manifest_runs, indent=2) + "\n")

    @staticmethod
    def _retarget_wrap(kwargs: dict, attempt_id: str,
                       retry_id: str) -> dict:
        """Retry must not write its instrumentation artifacts into the
        discarded first-attempt directory. The caller built `wrap` from
        attempt_id, so rewrite only the /runs/<id>/ path component."""
        out = dict(kwargs)
        wrap = out.get("wrap")
        if wrap:
            old = "/runs/%s/" % attempt_id
            new = "/runs/%s/" % retry_id
            out["wrap"] = [arg.replace(old, new) for arg in wrap]
        return out

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
            rec2 = self.run_fio(retry_id,
                                **self._retarget_wrap(kwargs, attempt_id,
                                                      retry_id))
            if rec2["status"] != "ok":
                rec2["arm_blocked"] = True
            return rec2
        return rec

    # -- modes --------------------------------------------------------------

    def mode_clean(self, cells, engines, repeats: int = 3,
                   ramp: int | None = None) -> None:
        """ramp=None keeps the Phase-1 baseline (self.ramp_s, mode label
        "clean"); ramp=0 produces "clean0" grids whose rusage window
        matches the io_bytes denominator (the only clean rows allowed to
        carry per-op rusage numbers, per the window contract)."""
        mode = "clean0" if ramp == 0 else "clean"
        for cell in cells:
            data = self.prep_file(cell, self.file_size)
            for engine in engines:
                for rep in range(1, repeats + 1):
                    job = "\n".join(self.job_lines(
                        engine, data, cell, ramp_override=ramp)) + "\n"
                    self.run_with_retry(
                        "%s__%s__%s__rep%d" % (cell, engine, mode, rep),
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
                # Flat sampling only: --call-graph (dwarf|fp) cannot open
                # any event at perf_event_paranoid=2 on this stack (perf
                # exits 0 on that failure -- receipt in capability.json),
                # so call-chain attribution is UNAVAILABLE non-root and the
                # sampled ledger stays function-level.
                wrap = ["perf", "record", "-o", str(run_dir / "perf.data"),
                        "-e", "cycles:u", "-F", str(freq), "--"]
                rec = self.run_with_retry(
                    run_id, job_text=job, wrap=wrap, diag=True,
                    expect_backend=engine_specs(diag=True)[engine][1] or None,
                    extra={"diag_binary": True})
                if rec["status"] != "ok":
                    continue
                # The accepted run may be the retry directory; reports must
                # be attached to the accepted receipt, never to the
                # discarded first attempt (audit R1-1).
                run_dir = self.accepted_run_dir(rec)
                data_file = run_dir / "perf.data"
                if not data_file.exists():
                    self._mark_missing_artifact(rec, "perf.data")
                    continue
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

    # -- root-session modes (Owner-granted sudo; no persistent config) ----

    def _sudo(self, argv: list) -> list:
        if not self.sudo_pw:
            raise RuntimeError("root mode requested without a sudo password")
        return ["sudo", "-S", "-p", ""] + argv

    def mode_pmu_root(self, cells, engines, repeats: int = 2) -> None:
        """Root PMU grids: user/kernel split plus scheduling/cache events.
        g1/g2 keep their core events at 100% time-running; g3 is an
        auxiliary group with six hardware events and its counters may be
        multiplexed (SCALED), so they never enter exact per-op tables."""
        from .capability import MAIN_EVENTS  # noqa: F401 (non-root groups)
        groups = (
            ("pmu-root-g1", ("instructions:u", "instructions:k",
                             "task-clock")),
            ("pmu-root-g2", ("cycles:u", "cycles:k")),
            ("pmu-root-g3", ("branches", "branch-misses",
                             "context-switches", "cpu-migrations",
                             "L1-dcache-loads", "LLC-loads")),
        )
        for cell in cells:
            data = self.prep_file(cell, self.file_size)
            for engine in engines:
                for rep in range(1, repeats + 1):
                    job = "\n".join(self.job_lines(
                        engine, data, cell, ramp_override=0)) + "\n"
                    for mode, events in groups:
                        run_id = "%s__%s__%s__rep%d" % (cell, engine, mode,
                                                       rep)
                        wrap = self._sudo(
                            ["perf", "stat", "-x,",
                             "-o", str(self.runs_dir / run_id /
                                       "perf-stat.raw.txt"),
                             "-e", ",".join(events)])
                        self.run_with_retry(
                            run_id, job_text=job, wrap=wrap,
                            expect_backend=engine_specs()[engine][1] or None)

    def mode_record_root(self, cells, engines, kernel_cycles: bool = False) -> None:
        """Root perf record with callchains: user dwarf for every engine,
        optional kernel frame-pointer chains on the same run."""
        for cell in cells:
            data = self.prep_file(cell, self.file_size)
            for engine in engines:
                job = "\n".join(self.job_lines(engine, data, cell,
                                               diag=True,
                                               ramp_override=0)) + "\n"
                run_id = "%s__%s__record-root__rep1" % (cell, engine)
                run_dir = self.runs_dir / run_id
                run_dir.mkdir(parents=True, exist_ok=True)
                wrap = self._sudo(
                    ["perf", "record", "-o", str(run_dir / "perf.data"),
                     "-e", "cycles:u", "-F", "997",
                     "--call-graph", "dwarf", "--"])
                rec = self.run_with_retry(
                    run_id, job_text=job, wrap=wrap, diag=True,
                    expect_backend=engine_specs(diag=True)[engine][1] or None,
                    extra={"diag_binary": True})
                if rec["status"] == "ok":
                    run_dir = self.accepted_run_dir(rec)
                    if (run_dir / "perf.data").exists():
                        self._perf_reports(run_dir, "perf-report",
                                           callgraph=True)
                    else:
                        self._mark_missing_artifact(rec, "perf.data")
                if kernel_cycles and engine in ("io_uring", "sluice-uring"):
                    kid = "%s__%s__record-root-k__rep1" % (cell, engine)
                    kdir = self.runs_dir / kid
                    kdir.mkdir(parents=True, exist_ok=True)
                    kwrap = self._sudo(
                        ["perf", "record", "-o", str(kdir / "perf.data"),
                         "-e", "cycles:k", "-F", "997",
                         "--call-graph", "fp", "--"])
                    krec = self.run_with_retry(
                        kid, job_text=job, wrap=kwrap, diag=True,
                        expect_backend=engine_specs(diag=True)[engine][1]
                        or None, extra={"diag_binary": True})
                    if krec["status"] == "ok":
                        kdir = self.accepted_run_dir(krec)
                        if (kdir / "perf.data").exists():
                            self._perf_reports(kdir, "perf-report",
                                               callgraph=True)
                        else:
                            self._mark_missing_artifact(krec, "perf.data")

    def _run_root_readable(self, argv: list) -> subprocess.CompletedProcess:
        """perf.data written by a root perf may be 0600 root: read it back
        through sudo when this is a root session."""
        if self.sudo_pw:
            proc = subprocess.run(self._sudo(argv), capture_output=True,
                                  text=True, input=self.sudo_pw + "\n")
            return proc
        return subprocess.run(argv, capture_output=True, text=True)

    def _perf_reports(self, run_dir: pathlib.Path, prefix: str,
                      callgraph: bool) -> None:
        data_file = run_dir / "perf.data"
        header = self._run_root_readable(
            ["perf", "report", "-i", str(data_file), "--header", "--stdio"])
        (run_dir / ("%s-header.txt" % prefix)).write_text(header.stdout)
        # callchain view (children sorted) for attribution hierarchy
        report = self._run_root_readable(
            ["perf", "report", "-i", str(data_file), "--stdio",
             "--percent-limit", "0.5"])
        (run_dir / ("%s.txt" % prefix)).write_text(report.stdout)
        flat = self._run_root_readable(
            ["perf", "report", "-i", str(data_file), "--stdio",
             "--no-children", "-g", "none", "--percent-limit", "0.2"])
        (run_dir / ("%s-flat.txt" % prefix)).write_text(flat.stdout)
        (run_dir / "perf.data.sha256").write_text(
            self._sha256_any(data_file) + "\n")
        if self.sudo_pw:
            # evidence must be readable by the (non-root) auditor
            targets = [data_file]
            old = run_dir / "perf.data.old"
            if old.exists():
                targets.append(old)
            subprocess.run(self._sudo(["chmod", "644"]
                                      + [str(t) for t in targets]),
                           capture_output=True, text=True,
                           input=self.sudo_pw + "\n")

    def _sha256_any(self, path: pathlib.Path) -> str:
        try:
            return sha256_file(path)
        except PermissionError:
            if not self.sudo_pw:
                return "UNREADABLE"
            proc = subprocess.run(
                self._sudo(["sha256sum", str(path)]), capture_output=True,
                text=True, input=self.sudo_pw + "\n")
            if proc.returncode != 0 or not proc.stdout.split():
                return "UNREADABLE"
            return proc.stdout.split()[0]

    BPF_SCRIPT_TEMPLATE = """// Counts only: tracepoint names are resolved by the running kernel;
// no tracepoint struct layout is hardcoded here.
tracepoint:syscalls:sys_enter_io_uring_enter { @enter[comm] = count(); }
tracepoint:io_uring:io_uring_submit_req { @submit[comm] = count(); }
tracepoint:io_uring:io_uring_queue_async_work { @punt[comm] = count(); }
tracepoint:io_uring:io_uring_complete { @cqe[comm] = count(); }
tracepoint:syscalls:sys_enter_futex { @futex[comm] = count(); }
// on-CPU dwell: arm at switch-IN, account at switch-OUT (two probes;
// a single prev_comm probe would measure wall time between switches)
// key by pid: sched_switch runs in the outgoing task's context, so the
// incoming task's identity comes from args->next_pid, not tid.
// prev_pid/next_pid are TIDs: with fio thread=1 the job is a thread of
// the fio process (TID != TGID), and @dwell is summed per comm, i.e.
// over every comm=fio thread, not over the submitting TID. Comm keys
// carry no pid/tgid, so these counters are host-isolated aggregates.
tracepoint:sched:sched_switch /strcontains(args->next_comm, "fio") || strcontains(args->next_comm, "iou-wrk")/ {
    @start[args->next_pid] = nsecs;
}
tracepoint:sched:sched_switch /strcontains(args->prev_comm, "fio") || strcontains(args->prev_comm, "iou-wrk")/ {
    if (@start[args->prev_pid] != 0) {
        @dwell[args->prev_comm] += nsecs - @start[args->prev_pid];
        delete(@start[args->prev_pid]);
    } else {
        @missing_start[args->prev_comm] = count();
    }
}
interval:s:%(window)d { exit(); }
"""

    def bpf_script(self, window_s: int) -> str:
        return self.BPF_SCRIPT_TEMPLATE % {"window": window_s}

    @staticmethod
    def _wait_for_attach(err_path, proc, timeout: float = 10.0) -> str:
        """Wait for bpftrace's `Attached N probes` receipt. A fixed sleep
        cannot prove attach; a slow attach would silently truncate the
        first ios and leave the denominator intact."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                text = pathlib.Path(err_path).read_text(errors="replace")
            except OSError:
                text = ""
            for line in text.splitlines():
                if line.strip().startswith("Attached "):
                    return line.strip()
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        return ""

    def mode_trace_bpf(self, cells, engines) -> None:
        """Root eBPF counting + scheduling dwell, alongside one measured
        fio run per (cell, engine). Counters are comm-keyed host
        aggregates (HOST_ISOLATED_AGGREGATE); bpftrace's lost-event
        receipt is saved verbatim, but it only covers perf-ring drops."""
        window_s = int(self.runtime_s + self.ramp_s + 4)
        for cell in cells:
            data = self.prep_file(cell, self.file_size)
            for engine in engines:
                job = "\n".join(self.job_lines(
                    engine, data, cell, ramp_override=0)) + "\n"
                run_id = "%s__%s__trace-bpf__rep1" % (cell, engine)
                run_dir = self.runs_dir / run_id
                run_dir.mkdir(parents=True, exist_ok=True)
                script = run_dir / "bpf-counts.bt"
                script.write_text(self.bpf_script(window_s))
                out = open(run_dir / "bpftrace.stdout.txt", "w")
                err = open(run_dir / "bpftrace.stderr.txt", "w")
                proc = subprocess.Popen(
                    self._sudo(["timeout", "-s", "INT", str(window_s),
                                "bpftrace", str(script)]),
                    stdin=subprocess.PIPE, stdout=out, stderr=err,
                    text=True)
                if self.sudo_pw:
                    try:
                        proc.stdin.write(self.sudo_pw + "\n")
                        proc.stdin.flush()
                    except BrokenPipeError:
                        proc.terminate()
                        proc.wait(timeout=10)
                        out.close()
                        err.close()
                        raise EnvironmentDrift(
                            "bpftrace exited before accepting the sudo "
                            "password")
                proc.stdin.close()
                attach_t0 = time.monotonic()
                attach_line = self._wait_for_attach(
                    run_dir / "bpftrace.stderr.txt", proc, timeout=10.0)
                attach_s = round(time.monotonic() - attach_t0, 3)
                if not attach_line:
                    proc.terminate()
                    proc.wait(timeout=10)
                    out.close()
                    err.close()
                    raise EnvironmentDrift(
                        "bpftrace did not confirm probe attach; refusing "
                        "to run fio outside a confirmed trace window")
                rec = self.run_with_retry(
                    run_id, job_text=job, own_bpf=True,
                    expect_backend=engine_specs()[engine][1] or None,
                    extra={"bpf_script": script.name,
                           "bpf_attach": attach_line,
                           "bpf_attach_s": attach_s,
                           "bpf_window_s": window_s})
                try:
                    proc.wait(timeout=window_s + 30)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    proc.wait(timeout=10)
                out.close()
                err.close()
                # The bpftrace window belongs to the first attempt. If the
                # accepted run is a retry, its fio execution happened outside
                # (or after) that window; never copy the first attempt's
                # counters into the retry directory (audit R1-1, corrected).
                accepted = self.accepted_run_dir(rec)
                retry_outside_window = accepted != run_dir
                if retry_outside_window:
                    accepted.mkdir(parents=True, exist_ok=True)
                    source = self._bpf_source_dir(run_dir)
                    src = source / "bpf-counts.bt"
                    if src.exists():
                        shutil.copy2(src, accepted / src.name)
                    (accepted / "bpf-scope.json").write_text(
                        json.dumps({
                            "trace_attempt": run_dir.name,
                            "accepted_attempt": accepted.name,
                            "counts_attributable": False,
                            "reason": "accepted fio run is a retry outside "
                                      "the traced attempt",
                        }, indent=2, sort_keys=True) + "\n")
                # Completion receipt: attach line, window, attach latency and
                # exit code. The report refuses EBPF_COUNTED when the window
                # does not cover attach + fio wall time.
                meta_path = accepted / "meta.json"
                if meta_path.exists():
                    meta = json.loads(meta_path.read_text())
                    meta["bpf_attach"] = attach_line
                    meta["bpf_window_s"] = window_s
                    meta["bpf_attach_s"] = attach_s
                    meta["bpf_retry_outside_window"] = retry_outside_window
                    meta["bpftrace_returncode"] = proc.returncode
                    meta_path.write_text(
                        json.dumps(meta, indent=2, sort_keys=True) + "\n")

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
                    accepted = self.accepted_run_dir(rec)
                    (accepted / "event-loss.json").write_text(json.dumps(
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

    def mode_crosscheck(self, engines, clean_reps: int = 3,
                        pmu_reps: int = 2) -> None:
        """Option C: fixed io_size, ramp 0 (window contract cross-check).
        A2 shape only, per pre-registration. Repeats match the main-grid
        rule (clean x3, pmu x2) so the C2 side carries a variance, not a
        single-run point."""
        data = self.prep_file("C2", self.file_size)
        for engine in engines:
            job = "\n".join(self.job_lines(engine, data, "C2")) + "\n"
            for rep in range(1, clean_reps + 1):
                self.run_with_retry(
                    "C2__%s__clean__rep%d" % (engine, rep), job_text=job,
                    expect_backend=engine_specs()[engine][1] or None,
                    timeout_s=600)
            for rep in range(1, pmu_reps + 1):
                run_id = "C2__%s__pmu__rep%d" % (engine, rep)
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
                # root-owned perf artifacts may be 0600: hash via sudo as
                # needed so the manifest is complete in root sessions.
                # A single unreadable file must not abort the manifest.
                try:
                    digest = self._sha256_any(path)
                except (RuntimeError, OSError, IndexError) as exc:
                    digest = "UNREADABLE(%s)" % type(exc).__name__
                lines.append("%s  %s" % (digest, rel))
        (self.root / "sha256-manifest.txt").write_text("\n".join(lines)
                                                       + "\n")

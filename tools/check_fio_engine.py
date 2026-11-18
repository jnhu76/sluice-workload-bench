#!/usr/bin/env python3
"""Deterministic correctness matrix for the fio-sluice external ioengine (Phase 5).

Contract (executed verbatim by the workflow):
    python3 tools/check_fio_engine.py --scratch-dir build/scratch \
        --output results/fio/correctness.json [--require-uring]

Exit codes: 0 = every case passed; 1 = any case failed (or the harness could
not run at all); 77 = --require-uring was given and io_uring is unavailable
(printed as UNAVAILABLE, never falling back to threadpool for the uring arm).

All I/O targets are temporary regular files created inside --scratch-dir and
removed afterwards; a filename guard refuses any target outside the scratch
directory, so raw devices can never be reached. Cases run strictly serially.

Oracles are two-layered:
  1. fio's own verify (crc32, or verify=pattern) catches per-offset data
     corruption on read-back, including a dedicated audit read pass after
     every mixed / mixed-size run (frozen legal outcome set: the final file
     is any interleaving of valid fio-written blocks, and the audit accepts
     exactly that set -- no undefined race is used as an expectation).
  2. Independent POSIX checks by this script: file size exactness, sha256
     immutability for read-only passes, and a full byte-compare against the
     expected per-block pattern for the verify=pattern write case.
     Shapes whose accounting cannot be derived arithmetically (write+verify
     short-read count, 70/30 split, bsrange io counts) are calibrated with
     fio's native sync engine on the identical job shape; the engine must
     match the native reference exactly.

Determinism notes (probed against fio 3.40, 2026-11-19; receipts in
docs/fio-integration/CORRECTNESS-MATRIX.md):
  - fio never issues a read crossing EOF on a regular file (it floors the io
    range to whole blocks inside the laid-out file), so the EOF case is a
    boundary case: a non-block-aligned file must come back bit-identical
    with exactly floor(size/bs) reads issued, none short.
  - fio 3.40 has no timeout_threshold option; cleanup-with-inflight is
    driven by a short time_based runtime plus SIGTERM delivered as soon as
    /proc/<pid>/io shows real I/O progress (a deterministic condition, not
    a random sleep).
  - thinktime is in microseconds; sparse arrivals use thinktime=2000 (2ms).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import time

FIO_VERSION = "fio-3.40"
RANDSEED = 81  # decimal; pins every random draw (offsets, split, payloads)
LOAD_RE = re.compile(
    r"fio-sluice: loaded revision=(\S+) sdk_profile=(\S+) fio_source=(\S+)"
    r" backend=(\S+) iodepth=(\d+) workers=(\d+)"
)
RWTS_RE = re.compile(
    r"issued rwts: total=(\d+),(\d+),(\d+),(\d+)"
    r" short=(\d+),(\d+),(\d+),(\d+) dropped=(\d+),(\d+),(\d+),(\d+)")
ERR_RE = re.compile(r"err=\s*(\d+)")
VERIFY_FAIL_RE = re.compile(r"verify failed|csum mismatch|verify: bad")
FILENAME_RE = re.compile(r"^filename=(.*)$", re.MULTILINE)

# Engine tripwires for fd-borrow, exactly-once staging, buffer publication
# and request settlement. Any occurrence in any run's stderr fails the case.
FORBIDDEN = (
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
    "request failed: code=",  # request-level failure outside injected cases
)


class CaseResult:
    def __init__(self, case_id: str, backend: str, engine: str, group: str):
        self.case_id = case_id
        self.backend = backend
        self.engine = engine
        self.group = group
        self.status = "pass"
        self.failures: list[str] = []
        self.evidence: dict = {}

    def fail(self, why: str) -> None:
        self.status = "fail"
        self.failures.append(why)


class Harness:
    """Runs fio jobs against scratch regular files and records evidence."""

    def __init__(self, fio: str, scratch: pathlib.Path, revision: str):
        self.fio = fio
        self.scratch = scratch.resolve()
        self.revision = revision
        self.case_root = self.scratch / "cases"
        self.data_root = self.scratch / "data"
        self.case_root.mkdir(parents=True, exist_ok=True)
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.results: list[CaseResult] = []

    # -- scratch helpers ---------------------------------------------------

    def data_path(self, name: str) -> pathlib.Path:
        return (self.data_root / name).resolve()

    def data_file(self, name: str, size: int | None = None,
                  content: bytes | None = None) -> pathlib.Path:
        path = self.data_path(name)
        self.guard(path)
        if path.exists():
            path.unlink()
        with open(path, "wb") as fh:
            if content is not None:
                fh.write(content)
            elif size is not None:
                fh.truncate(size)
        return path

    def guard(self, path: pathlib.Path) -> None:
        resolved = path.resolve()
        if self.scratch != resolved and self.scratch not in resolved.parents:
            raise RuntimeError(f"refusing non-scratch target {resolved}")
        if resolved.exists() and not resolved.is_file():
            raise RuntimeError(f"refusing non-regular target {resolved}")

    @staticmethod
    def sha256(path: pathlib.Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    # -- job construction ---------------------------------------------------

    def job_text(self, *, engine: pathlib.Path | str, backend: str,
                 data: pathlib.Path, rw: str, iodepth: int, bs: str = "4k",
                 size: str = "256k", verify: str | None = "crc32c",
                 do_verify: int | None = None,
                 extra: tuple[str, ...] = ()) -> str:
        lines = [
            "[case]",
            f"ioengine=external:{engine}",
            f"filename={data}",
            f"sluice_backend={backend}",
            f"rw={rw}",
            f"bs={bs}",
            f"size={size}",
            f"iodepth={iodepth}",
        ]
        if verify is not None:
            lines.append(f"verify={verify}")
        if do_verify is not None:
            lines.append(f"do_verify={do_verify}")
        lines += ["direct=1", "thread=1", "group_reporting=1",
                  f"randseed={RANDSEED}"]
        lines += list(extra)
        return "\n".join(lines) + "\n"

    # -- job execution ------------------------------------------------------

    def run(self, case: CaseResult, job: str, *, timeout_s: float = 90.0,
            allowed_diag: tuple[str, ...] = (),
            expect_load_line: bool = True) -> dict:
        """Run one job, fill case.evidence, and enforce the global
        engine-side invariants (tripwires + diagnostic allowlist)."""
        for target in FILENAME_RE.findall(job):
            self.guard(pathlib.Path(target))
        cdir = self.case_root / case.case_id
        if cdir.exists():
            shutil.rmtree(cdir)
        cdir.mkdir(parents=True)
        jf = cdir / "job.fio"
        jf.write_text(job)
        started = time.monotonic()
        proc = subprocess.Popen(
            [self.fio, "--eta=never", str(jf)], cwd=cdir,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True)
        timed_out = False
        try:
            out, err = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGKILL)
            out, err = proc.communicate()
            case.fail(f"watchdog exceeded {timeout_s}s (finite-timeout"
                      f" control); process killed")
        wall_ms = int((time.monotonic() - started) * 1000)
        out, err = out or "", err or ""

        rwts = RWTS_RE.search(out)
        # Early failures (file setup) put the err= line on stderr only.
        errm = ERR_RE.search(out) or ERR_RE.search(err)
        diag = [ln.strip() for ln in err.splitlines() if "fio-sluice:" in ln]
        load = LOAD_RE.search(err)
        ev = {
            "exit_code": proc.returncode,
            "timed_out": timed_out,
            "wall_ms": wall_ms,
            "rwts": rwts.groups() if rwts else None,
            "err": int(errm.group(1)) if errm else None,
            "engine_diag": diag,
            "engine_load": load.groups() if load else None,
            "verify_fail_lines":
                [ln.strip() for ln in (out.splitlines() + err.splitlines())
                 if VERIFY_FAIL_RE.search(ln)][:4],
            "sync_section_present":
                "fsync/fdatasync/sync_file_range:" in out,
            "stdout_tail": out.splitlines()[-12:],
            "stderr_tail": err.splitlines()[-12:],
        }
        case.evidence = ev

        for line in diag:
            if any(f in line for f in FORBIDDEN):
                case.fail(f"engine tripwire diagnostic: {line}")
            elif LOAD_RE.match(line):
                continue
            elif not any(a in line for a in allowed_diag):
                case.fail(f"unexpected engine diagnostic: {line}")
        if expect_load_line and ev["engine_load"] is None:
            case.fail("engine load/version trace missing from stderr")
        if not expect_load_line and ev["engine_load"] is not None:
            case.fail("engine load trace present although init should fail")
        if ev["engine_load"] is not None:
            if ev["engine_load"][0] != self.revision:
                case.fail(f"engine revision {ev['engine_load'][0]}"
                          f" != pinned {self.revision}")
        if ev["verify_fail_lines"]:
            case.fail("fio verify reported corruption: "
                      + "; ".join(ev["verify_fail_lines"][:2]))
        shutil.rmtree(cdir, ignore_errors=True)
        return ev

    def expect(self, case: CaseResult, ev: dict, *, exit_code: int = 0,
               total: tuple[int, ...] | None = None,
               short: tuple[int, ...] | None = None,
               backend: str | None = None,
               iodepth: int | None = None) -> None:
        if ev["timed_out"]:
            return
        if ev["exit_code"] != exit_code:
            case.fail(f"fio exit {ev['exit_code']} != expected {exit_code}")
        if total is not None:
            if ev["rwts"] is None:
                case.fail("no 'issued rwts' line in fio output")
            else:
                got = tuple(int(x) for x in ev["rwts"][:4])
                if got != tuple(total):
                    case.fail(f"issued total {got} != expected {tuple(total)}")
        if short is not None:
            if ev["rwts"] is None:
                case.fail("no 'issued rwts' line in fio output")
            else:
                got = tuple(int(x) for x in ev["rwts"][4:8])
                if got != tuple(short):
                    case.fail(f"issued short {got} != expected {tuple(short)}")
        if ev["rwts"] is not None:
            dropped = tuple(int(x) for x in ev["rwts"][8:12])
            if any(dropped):
                case.fail(f"dropped ios reported: {dropped}")
        if exit_code == 0 and ev["err"] not in (0, None):
            case.fail(f"err={ev['err']} on a clean run")
        if backend is not None and ev["engine_load"] is not None:
            if ev["engine_load"][3] != backend:
                case.fail(f"engine reports backend {ev['engine_load'][3]}"
                          f" != {backend}")
        if iodepth is not None and ev["engine_load"] is not None:
            if int(ev["engine_load"][4]) != iodepth:
                case.fail(f"engine reports iodepth {ev['engine_load'][4]}"
                          f" != {iodepth}")

    # -- native calibration (parity oracle) ---------------------------------

    def calibrate(self, harness_case_id: str, job_template: str,
                  data: pathlib.Path) -> dict:
        """Run the identical job shape under fio's native sync engine and
        return the reference accounting. Parity, not performance."""
        lines = [ln for ln in job_template.splitlines()
                 if not ln.startswith(("ioengine=", "sluice_backend="))]
        lines = [ln.replace("[case]", f"[{harness_case_id}]") for ln in lines]
        header = next(i for i, ln in enumerate(lines) if ln.startswith("["))
        lines.insert(header + 1, "ioengine=sync")
        case = CaseResult("native-calibration", "native", "sync",
                          "calibration")
        ev = self.run(case, "\n".join(lines) + "\n", expect_load_line=False)
        try:
            if case.status != "pass" or ev["rwts"] is None or ev["exit_code"]:
                raise RuntimeError(
                    f"native calibration {harness_case_id} failed: "
                    f"{case.failures}; ev_exit={ev['exit_code']}")
            return {"total": tuple(int(x) for x in ev["rwts"][:4]),
                    "short": tuple(int(x) for x in ev["rwts"][4:8])}
        finally:
            data.unlink(missing_ok=True)


class Matrix:
    """Builds and executes the case matrix."""

    def __init__(self, harness: Harness, engines: dict[str, pathlib.Path],
                 uring_available: bool):
        self.h = harness
        self.engines = engines
        self.uring_available = uring_available

    def backends(self) -> list[tuple[str, pathlib.Path, str]]:
        """(backend, engine path, engine label) triples to exercise."""
        out = [("threadpool", self.engines["noring"], "noring")]
        ring = self.engines.get("ring")
        if ring is not None:
            out.append(("threadpool", ring, "ring"))
            if self.uring_available:
                out.append(("uring", ring, "ring"))
        return out

    def new_case(self, name: str, backend: str, engine_label: str,
                 group: str) -> CaseResult:
        case = CaseResult(name, backend, engine_label, group)
        self.h.results.append(case)
        return case

    # -- case families ------------------------------------------------------

    def read_family(self) -> None:
        """read 4KiB D1/4/16 and 64KiB D1/4: fio verify + POSIX immutability.
        Each block size gets its own prefill: fio's verify headers encode the
        writing block size, so 64k reads must audit a 64k-written file."""
        for backend, engine, label in self.backends():
            for bs, depths, nblocks in (("4k", (1, 4, 16), 64),
                                        ("64k", (1, 4), 4)):
                data = self.h.data_file(f"read-{bs}-{label}-{backend}.bin")
                pre = self.new_case(f"prefill-read-{bs}-{label}-{backend}",
                                    backend, label, "read")
                ev = self.h.run(pre, self.h.job_text(
                    engine=engine, backend=backend, data=data, rw="write",
                    iodepth=4, bs=bs, verify="crc32c"))
                self.h.expect(pre, ev, total=(nblocks, nblocks, 0, 0),
                              backend=backend)
                pre.evidence["note"] = ("write+verify prefill feeds the"
                                        " read cases")
                digest = self.h.sha256(data)
                for d in depths:
                    case = self.new_case(f"read-{bs}-d{d}-{label}-{backend}",
                                         backend, label, "read")
                    ev = self.h.run(case, self.h.job_text(
                        engine=engine, backend=backend, data=data, rw="read",
                        iodepth=d, bs=bs, verify="crc32c"))
                    self.h.expect(case, ev, total=(nblocks, 0, 0, 0),
                                  short=(0, 0, 0, 0), backend=backend,
                                  iodepth=d)
                    if self.h.sha256(data) != digest:
                        case.fail("read pass mutated the data file")
                data.unlink(missing_ok=True)

    def write_family(self) -> None:
        """write 4KiB D1/4/16 with do_verify; short counts from native parity."""
        for backend, engine, label in self.backends():
            data = self.h.data_file(f"write-{label}-{backend}.bin",
                                    256 * 1024)
            for d in (1, 4, 16):
                calib_data = self.h.data_file("calib-write.bin", 256 * 1024)
                calib = self.h.calibrate(
                    f"write-verify-d{d}",
                    self.h.job_text(engine=engine, backend=backend,
                                    data=calib_data, rw="write", iodepth=d,
                                    verify="crc32c", do_verify=1), calib_data)
                job = self.h.job_text(engine=engine, backend=backend,
                                      data=data, rw="write", iodepth=d,
                                      verify="crc32c", do_verify=1)
                case = self.new_case(f"write-4k-d{d}-{label}-{backend}",
                                     backend, label, "write")
                ev = self.h.run(case, job)
                self.h.expect(case, ev, total=calib["total"],
                              short=calib["short"], backend=backend,
                              iodepth=d)
                # Layer 2: exactly `size` bytes, nothing more.
                if data.stat().st_size != 256 * 1024:
                    case.fail(f"file size {data.stat().st_size} != 262144")
            data.unlink(missing_ok=True)

    def pattern_family(self) -> None:
        """verify=pattern write, verified byte-for-byte by this script."""
        pattern = bytes.fromhex("a5dead")
        block, blocks = 4096, 16
        expected_block = (pattern * (block // len(pattern) + 1))[:block]
        for backend, engine, label in self.backends():
            data = self.h.data_file(f"pattern-{label}-{backend}.bin")
            calib_data = self.h.data_file("calib-pattern.bin")
            calib = self.h.calibrate(
                "write-pattern",
                self.h.job_text(engine=engine, backend=backend,
                                data=calib_data, rw="write", iodepth=4,
                                size="64k", verify="pattern",
                                do_verify=1,
                                extra=("verify_pattern=0xa5dead",)),
                calib_data)
            job = self.h.job_text(engine=engine, backend=backend, data=data,
                                  rw="write", iodepth=4, size="64k",
                                  verify="pattern", do_verify=1,
                                  extra=("verify_pattern=0xa5dead",))
            case = self.new_case(f"write-pattern-d4-{label}-{backend}",
                                 backend, label, "write-pattern")
            ev = self.h.run(case, job)
            self.h.expect(case, ev, total=calib["total"],
                          short=calib["short"], backend=backend)
            raw = data.read_bytes()
            if len(raw) != blocks * block:
                case.fail(f"pattern file size {len(raw)} != {blocks * block}")
            elif raw != expected_block * blocks:
                first = next(i for i, (a, b) in enumerate(
                    zip(raw, expected_block * blocks)) if a != b)
                case.fail(f"pattern byte mismatch at offset {first}")
            data.unlink(missing_ok=True)

    def mixed_family(self) -> None:
        """70/30 randrw D4/16 with verify, then a full audit read pass."""
        for backend, engine, label in self.backends():
            data = self.h.data_file(f"mixed-{label}-{backend}.bin")
            pre = self.new_case(f"mixed-prefill-{label}-{backend}",
                                backend, label, "mixed")
            ev = self.h.run(pre, self.h.job_text(
                engine=engine, backend=backend, data=data, rw="write",
                iodepth=4, verify="crc32c"))
            self.h.expect(pre, ev, total=(64, 64, 0, 0), backend=backend)

            for d in (4, 16):
                calib_data = self.h.data_file("calib-mixed.bin", 256 * 1024)
                calib = self.h.calibrate(
                    f"randrw-70-d{d}",
                    self.h.job_text(engine=engine, backend=backend,
                                    data=calib_data, rw="randrw", iodepth=d,
                                    extra=("rwmixread=70",),
                                    verify="crc32c"), calib_data)
                job = self.h.job_text(
                    engine=engine, backend=backend, data=data, rw="randrw",
                    iodepth=d, extra=("rwmixread=70",), verify="crc32c")
                case = self.new_case(f"mixed-7030-d{d}-{label}-{backend}",
                                     backend, label, "mixed")
                ev = self.h.run(case, job)
                # Frozen legal set: any interleaving of valid fio-written
                # blocks; the accounting must match the native reference.
                self.h.expect(case, ev, total=calib["total"],
                              short=calib["short"], backend=backend,
                              iodepth=d)
                if data.stat().st_size != 256 * 1024:
                    case.fail("mixed run changed the data file size")
                aud = self.new_case(f"mixed-audit-d{d}-{label}-{backend}",
                                    backend, label, "mixed")
                aev = self.h.run(aud, self.h.job_text(
                    engine=engine, backend=backend, data=data, rw="read",
                    iodepth=d, verify="crc32c", do_verify=0))
                self.h.expect(aud, aev, total=(64, 0, 0, 0), short=(0, 0, 0, 0),
                              backend=backend)
                aud.evidence["note"] = ("audit read+verify accepts exactly the"
                                        " frozen legal outcome set")
            data.unlink(missing_ok=True)

    def mixed_size_family(self) -> None:
        """bsrange 4k-64k write phase, native-parity accounting. The data
        oracle is fio's in-job do_verify walk: it replays exactly the ios
        the job itself wrote, so every varied-size header and payload is
        checked. A cross-job read audit is impossible here (probed: fio's
        randread does not regenerate the randwrite offset/size walk even
        under a fixed randseed), so no separate audit job is run."""
        for backend, engine, label in self.backends():
            data = self.h.data_file(f"msize-{label}-{backend}.bin")
            for d in (4, 16):
                calib_data = self.h.data_file("calib-msize.bin", 512 * 1024)
                calib = self.h.calibrate(
                    f"randwrite-bsrange-d{d}",
                    self.h.job_text(engine=engine, backend=backend,
                                    data=calib_data, rw="randwrite",
                                    iodepth=d, bs="4k-64k", size="512k",
                                    verify="crc32c", do_verify=1), calib_data)
                case = self.new_case(f"mixed-size-d{d}-{label}-{backend}",
                                     backend, label, "mixed-size")
                ev = self.h.run(case, self.h.job_text(
                    engine=engine, backend=backend, data=data, rw="randwrite",
                    iodepth=d, bs="4k-64k", size="512k", verify="crc32c",
                    do_verify=1))
                self.h.expect(case, ev, total=calib["total"],
                              short=calib["short"], backend=backend,
                              iodepth=d)
                if data.stat().st_size != 512 * 1024:
                    case.fail("mixed-size run changed the data file size")
            data.unlink(missing_ok=True)

    def sparse_family(self) -> None:
        """thinktime=2ms gaps (sparse arrivals) at D1/4 on a prefilled file."""
        for backend, engine, label in self.backends():
            data = self.h.data_file(f"sparse-{label}-{backend}.bin")
            pre = self.new_case(f"sparse-prefill-{label}-{backend}",
                                backend, label, "sparse")
            ev = self.h.run(pre, self.h.job_text(
                engine=engine, backend=backend, data=data, rw="write",
                iodepth=4, size="128k", verify="crc32c"))
            self.h.expect(pre, ev, total=(32, 32, 0, 0), backend=backend)
            digest = self.h.sha256(data)
            for d in (1, 4):
                case = self.new_case(f"sparse-d{d}-{label}-{backend}",
                                     backend, label, "sparse")
                ev = self.h.run(case, self.h.job_text(
                    engine=engine, backend=backend, data=data, rw="randread",
                    iodepth=d, size="128k", verify="crc32c",
                    extra=("thinktime=2000", "thinktime_blocks=1")))
                self.h.expect(case, ev, total=(32, 0, 0, 0), backend=backend,
                              iodepth=d)
                # At D1 the 32 think gaps are serial: 32 x 2ms = 64ms is a
                # deterministic lower bound (recorded, not asserted, at D4
                # where the pipeline legitimately hides part of the gap).
                if d == 1 and ev["wall_ms"] < 50:
                    case.fail(f"sparse arrivals collapsed at D1 "
                              f"(wall {ev['wall_ms']}ms < 50ms)")
                if self.h.sha256(data) != digest:
                    case.fail("sparse read pass mutated the data file")
            data.unlink(missing_ok=True)

    def eof_family(self) -> None:
        """Non-block-aligned file (73 blocks + 2048B): floor(size/bs) reads,
        nothing short, file bit-identical. fio never issues the partial
        trailing block on regular files (probed), so this pins the EOF
        boundary semantics: no over-read, no mutation."""
        size = 73 * 4096 + 2048  # 301056
        content = bytes((i * 7 + 11) & 0xFF for i in range(size))
        for backend, engine, label in self.backends():
            data = self.h.data_file(f"eof-{label}-{backend}.bin",
                                    content=content)
            digest = self.h.sha256(data)
            case = self.new_case(f"eof-boundary-d4-{label}-{backend}",
                                 backend, label, "eof")
            ev = self.h.run(case, self.h.job_text(
                engine=engine, backend=backend, data=data, rw="read",
                iodepth=4, size=str(size), verify=None))
            self.h.expect(case, ev, total=(73, 0, 0, 0), short=(0, 0, 0, 0),
                          backend=backend)
            if data.stat().st_size != size:
                case.fail("EOF boundary run changed the data file size")
            if self.h.sha256(data) != digest:
                case.fail("EOF boundary run mutated the data file content")
            data.unlink(missing_ok=True)

    def sync_family(self) -> None:
        """fdatasync=8 through the engine: the fsync latency section in fio's
        output proves real syncs were issued and accounted as syncs, never
        faked as data-io successes."""
        for backend, engine, label in self.backends():
            data = self.h.data_file(f"sync-{label}-{backend}.bin", 64 * 1024)
            case = self.new_case(f"fdatasync-d2-{label}-{backend}",
                                 backend, label, "sync")
            ev = self.h.run(case, self.h.job_text(
                engine=engine, backend=backend, data=data, rw="write",
                iodepth=2, size="64k", verify="crc32c", do_verify=1,
                extra=("fdatasync=8",)))
            self.h.expect(case, ev, total=(16, 16, 0, 0), backend=backend)
            if not ev["sync_section_present"]:
                case.fail("fsync/fdatasync section missing: syncs not issued"
                          " or not accounted as syncs")
            data.unlink(missing_ok=True)

    def error_family(self) -> None:
        eng_n = self.engines["noring"]
        eng_r = self.engines.get("ring")

        # Per-request failure delivered, never lost: the engine rejects TRIM
        # with EINVAL through the completion path and fio accounts the io.
        data = self.h.data_file("err-trim.bin", 64 * 1024)
        case = self.new_case("trim-rejected", "threadpool", "noring", "error")
        ev = self.h.run(case, self.h.job_text(
            engine=eng_n, backend="threadpool", data=data, rw="trim",
            iodepth=1, size="64k", verify=None),
            allowed_diag=("ddir 2 is not supported",))
        self.h.expect(case, ev, exit_code=1)
        if ev["err"] != 22:
            case.fail(f"TRIM completion err={ev['err']} != EINVAL(22)")
        if ev["rwts"] and int(ev["rwts"][2]) != 1:
            case.fail("TRIM io not accounted in the issued counters")
        data.unlink(missing_ok=True)

        # Open failures (fail closed at file setup, through the engine's
        # open_file -> generic_open_file path).
        missing = self.h.data_path("err-missing.bin")
        if missing.exists():
            missing.unlink()
        for backend, engine, label, cid in (
                ("threadpool", eng_n, "noring", "open-missing-file"),
                *(() if not self.uring_available else
                  (("uring", eng_r, "ring", "open-missing-file-uring"),)),
        ):
            case = self.new_case(cid, backend, label, "error")
            ev = self.h.run(case, self.h.job_text(
                engine=engine, backend=backend, data=missing, rw="read",
                iodepth=1, size="64k", verify=None,
                extra=("allow_file_create=0",)),
                expect_load_line=False)  # fio fails file setup before init
            self.h.expect(case, ev, exit_code=1)
            blob = json.dumps(ev)
            if "disallowed" not in blob and "No such file" not in blob:
                case.fail("missing-file run lacks an open-failure message")

        rodir = self.h.case_root / "rodir"
        rodir.mkdir(parents=True, exist_ok=True)
        os.chmod(rodir, 0o500)
        try:
            case = self.new_case("open-readonly-dir", "threadpool",
                                 "noring", "error")
            ev = self.h.run(case, self.h.job_text(
                engine=eng_n, backend="threadpool", data=rodir / "x.bin",
                rw="write", iodepth=1, size="64k", verify=None),
                expect_load_line=False)
            self.h.expect(case, ev, exit_code=1)
            if ev["err"] != 13:
                case.fail(f"read-only dir open err={ev['err']} != EACCES(13)")
        finally:
            os.chmod(rodir, 0o700)
            shutil.rmtree(rodir, ignore_errors=True)

        # Engine-side invalid parameters: rejected at init/parse, no silent
        # fallback, no POSIX fallback.
        data = self.h.data_file("err-params.bin", 64 * 1024)
        expectations = [
            ("invalid-backend", eng_n, "nosuchbackend", (), "invalid sluice_backend"),
            ("noring-rejects-uring", eng_n, "uring", (), "io_uring unavailable"),
            ("verify-async-rejected", eng_n, "threadpool",
             ("verify_async=2",), "verify_async"),
            ("workers-zero-rejected", eng_n, "threadpool",
             ("sluice_workers=0",), "out of range"),
            ("iodepth-zero-rejected", eng_n, "threadpool", (),
             "failed parsing iodepth"),
            ("offload-rejected", eng_n, "threadpool",
             ("io_submit_mode=offload",), "offloaded submit"),
        ]
        if eng_r is not None:
            expectations.append(
                ("uring-workers-rejected", eng_r, "uring",
                 ("sluice_workers=2",),
                 "sluice_workers applies to the threadpool backend only"))
        for cid, engine, backend, extra, marker in expectations:
            case = self.new_case(cid, backend,
                                 "noring" if engine == eng_n else "ring",
                                 "error")
            ev = self.h.run(case, self.h.job_text(
                engine=engine, backend=backend, data=data, rw="read",
                iodepth=0 if cid == "iodepth-zero-rejected" else 1,
                size="64k", verify=None, extra=extra),
                allowed_diag=(marker,), expect_load_line=False)
            self.h.expect(case, ev, exit_code=1)
            blob = "\n".join(ev["engine_diag"]) + json.dumps(ev)
            if marker not in blob:
                case.fail(f"rejection marker '{marker}' not found in output")
        data.unlink(missing_ok=True)

        # iodepth == capacity == 1024: no silent overflow, nothing dropped,
        # nothing stuck (the engine derives capacity from iodepth, so this is
        # the overflow arm of the negative controls).
        for backend, engine, label in self.backends():
            data = self.h.data_file(f"depth1024-{label}-{backend}.bin",
                                    4 * 1024 * 1024)
            pre = self.new_case(f"depth1024-prefill-{label}-{backend}",
                                backend, label, "error")
            ev = self.h.run(pre, self.h.job_text(
                engine=engine, backend=backend, data=data, rw="write",
                iodepth=32, size="4096k", verify="crc32c"), timeout_s=180.0)
            self.h.expect(pre, ev, total=(1024, 1024, 0, 0), backend=backend)
            case = self.new_case(f"depth1024-d1024-{label}-{backend}",
                                 backend, label, "error")
            ev = self.h.run(case, self.h.job_text(
                engine=engine, backend=backend, data=data, rw="read",
                iodepth=1024, size="4096k", verify="crc32c"), timeout_s=180.0)
            self.h.expect(case, ev, total=(1024, 0, 0, 0), short=(0, 0, 0, 0),
                          backend=backend, iodepth=1024)
            data.unlink(missing_ok=True)

    def cleanup_family(self) -> None:
        """Short time_based run + SIGTERM once /proc/<pid>/io shows real
        progress (deterministic trigger; fio 3.40 has no timeout_threshold).
        The process must exit promptly with every accepted request settled."""
        for backend, engine, label in self.backends():
            data = self.h.data_file(f"inflight-{label}-{backend}.bin")
            case = self.new_case(f"cleanup-inflight-{label}-{backend}",
                                 backend, label, "cleanup")
            cdir = self.h.case_root / case.case_id
            if cdir.exists():
                shutil.rmtree(cdir)
            cdir.mkdir(parents=True)
            (cdir / "job.fio").write_text(self.h.job_text(
                engine=engine, backend=backend, data=data, rw="randrw",
                iodepth=16, size="4096k", verify=None,
                extra=("rwmixread=50", "time_based=1", "runtime=30")))
            proc = subprocess.Popen(
                [self.h.fio, "--eta=never", str(cdir / "job.fio")],
                cwd=cdir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, start_new_session=True)
            deadline = time.monotonic() + 30.0
            progress = 0
            while time.monotonic() < deadline and proc.poll() is None:
                try:
                    with open(f"/proc/{proc.pid}/io") as fh:
                        ios = sum(int(ln.split()[1]) for ln in fh
                                  if ln.startswith(("read_bytes",
                                                    "write_bytes")))
                except (FileNotFoundError, ProcessLookupError):
                    break
                if ios >= 128 * 1024:
                    progress = ios
                    break
                time.sleep(0.02)
            sig_at = time.monotonic()
            proc.terminate()
            try:
                out, err = proc.communicate(timeout=30.0)
                exit_code, ignored = proc.returncode, False
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                out, err = proc.communicate()
                exit_code, ignored = proc.returncode, True
            drain_ms = int((time.monotonic() - sig_at) * 1000)
            out, err = out or "", err or ""
            diag = [ln.strip() for ln in err.splitlines()
                    if "fio-sluice:" in ln]
            case.evidence = {
                "exit_code": exit_code,
                "ignored_sigterm": ignored,
                "progress_bytes_at_signal": progress,
                "drain_wall_ms": drain_ms,
                "engine_diag": diag,
                "signal_line": next((ln for ln in out.splitlines()
                                     if "signal" in ln), None),
            }
            if ignored:
                case.fail("process ignored SIGTERM for 30s (hang at cleanup)")
            if exit_code not in (0, 128, 143):
                case.fail(f"unexpected exit code {exit_code} after SIGTERM")
            if not case.evidence["signal_line"]:
                case.fail("fio did not log the termination signal")
            for line in diag:
                if any(f in line for f in FORBIDDEN):
                    case.fail(f"engine tripwire at cleanup: {line}")
                elif not LOAD_RE.match(line):
                    case.fail(f"unexpected engine diagnostic at cleanup:"
                              f" {line}")
            if progress < 128 * 1024:
                case.fail(f"job never reached 128KiB of real I/O ({progress}B)"
                          f" before the signal")
            try:
                data.unlink()
            except OSError as exc:
                case.fail(f"data file not removable after exit: {exc}")
            shutil.rmtree(cdir, ignore_errors=True)


def probe_uring(harness: Harness, ring: pathlib.Path) -> tuple[bool, str]:
    """Tiny uring job through the ring engine. An init failure carrying our
    diagnostic counts as unavailability, never as a harness error."""
    data = harness.data_file("probe-uring.bin", 256 * 1024)
    case = CaseResult("uring-probe", "uring", "ring", "probe")
    harness.results.append(case)
    ev = harness.run(case, harness.job_text(
        engine=ring, backend="uring", data=data, rw="read", iodepth=1,
        size="64k", verify=None),
        allowed_diag=("io_uring unavailable", "io_uring context rejected"),
        expect_load_line=False)
    data.unlink(missing_ok=True)
    if ev["exit_code"] == 0 and ev["engine_load"] is not None \
            and not ev["timed_out"]:
        case.status = "pass"
        case.evidence["available"] = True
        return True, ""
    case.status = "pass"  # the probe itself behaved as expected
    case.failures = []  # init failure is the answer, not a probe defect
    case.evidence["available"] = False
    reason = next((l for l in ev["engine_diag"] if LOAD_RE.match(l) is None),
                  f"uring probe fio exit {ev['exit_code']}")
    case.evidence["unavailable_reason"] = reason
    return False, reason


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Deterministic correctness matrix for fio-sluice")
    ap.add_argument("--scratch-dir", default="build/scratch")
    ap.add_argument("--output", required=True)
    ap.add_argument("--require-uring", action="store_true")
    ap.add_argument("--fio", default="fio")
    args = ap.parse_args()

    root = pathlib.Path(__file__).resolve().parents[1]
    scratch = (root / args.scratch_dir).resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    out_path = (root / args.output).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    engines = {
        "noring": root / "build/linux/x86_64/release/fio-sluice-noring.so",
        "ring": root / "build/linux/x86_64/release/fio-sluice-ring.so",
    }
    meta = {"fio_version": None, "require_uring": args.require_uring,
            "engines": {}, "uring": {"available": False, "reason": None},
            "sluice_revision": None}
    fatal: list[str] = []

    harness_results: list[CaseResult] = []

    def emit_and_exit(code: int) -> int:
        rows = harness_results
        summary = {
            "total": len(rows),
            "passed": sum(r.status == "pass" for r in rows),
            "failed": sum(r.status == "fail" for r in rows),
            "skipped": sum(r.status == "skipped" for r in rows),
        }
        doc = {
            "meta": meta,
            "cases": [
                {"id": r.case_id, "group": r.group, "backend": r.backend,
                 "engine": r.engine, "status": r.status,
                 "failures": r.failures, "evidence": r.evidence}
                for r in rows
            ],
            "summary": summary,
            "fatal": fatal,
        }
        out_path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
        for r in rows:
            if r.status == "fail":
                print(f"FAIL: {r.case_id}: {'; '.join(r.failures)}")
        if fatal:
            print("FATAL:", "; ".join(fatal))
        print(f"RESULT: {summary['passed']} passed, {summary['failed']} failed,"
              f" {summary['skipped']} skipped -> {out_path}")
        return code

    # Environment gates: the threadpool engine and fio version are hard
    # requirements; a missing ring engine only disables the uring arm
    # (mirrors the smoke.sh semantics).
    version = subprocess.run([args.fio, "--version"], capture_output=True,
                             text=True)
    meta["fio_version"] = version.stdout.strip()
    if version.returncode != 0 or version.stdout.strip() != FIO_VERSION:
        fatal.append(f"host fio is {version.stdout.strip()!r},"
                     f" engine ABI pinned to {FIO_VERSION}")
        return emit_and_exit(1)
    if not engines["noring"].is_file():
        fatal.append(f"engine noring missing: {engines['noring']}")
        return emit_and_exit(1)
    if not engines["ring"].is_file():
        del engines["ring"]
    meta["engines"] = {k: str(v) for k, v in engines.items()}
    meta["sluice_revision"] = (root / "SLUICE_REVISION").read_text().strip()

    harness = Harness(args.fio, scratch, meta["sluice_revision"])
    harness_results = harness.results
    uring_ok, uring_reason = (
        (False, f"ring engine not built: "
                f"{root / 'build/linux/x86_64/release/fio-sluice-ring.so'}")
        if "ring" not in engines else probe_uring(harness, engines["ring"]))
    meta["uring"] = {"available": uring_ok,
                     "reason": None if uring_ok else uring_reason}
    if not uring_ok and args.require_uring:
        print(f"UNAVAILABLE: {uring_reason}")
        print("(--require-uring: threadpool fallback is not attempted)")
        return emit_and_exit(77)
    if not uring_ok:
        print(f"io_uring unavailable; the uring arm is recorded as skipped:"
              f" {uring_reason}")
        skip = CaseResult("uring-arm", "uring", "ring", "matrix-arm")
        skip.status = "skipped"
        skip.evidence = {"reason": uring_reason}
        harness.results.append(skip)

    matrix = Matrix(harness, engines, uring_ok)
    for family in (matrix.read_family, matrix.write_family,
                   matrix.pattern_family, matrix.mixed_family,
                   matrix.mixed_size_family, matrix.sparse_family,
                   matrix.eof_family, matrix.sync_family,
                   matrix.error_family, matrix.cleanup_family):
        try:
            family()
        except (RuntimeError, OSError) as exc:
            fatal.append(f"{family.__name__}: {exc}")

    # Nothing may outlive the matrix: no fio process, no scratch leftovers.
    leftovers = subprocess.run(["pgrep", "-af", f"fio.*{scratch}"],
                               capture_output=True, text=True)
    stray = [ln for ln in leftovers.stdout.splitlines()
             if ln and "check_fio_engine" not in ln
             and int(ln.split()[0]) != os.getpid()]
    if stray:
        fatal.append(f"leftover fio processes: {' '.join(stray)}")
    for entry in (scratch / "cases", scratch / "data"):
        if entry.exists() and any(entry.iterdir()):
            fatal.append(f"scratch leftovers in {entry}: "
                         f"{[p.name for p in entry.iterdir()]}")

    failed = bool(fatal) or any(r.status == "fail" for r in harness.results)
    return emit_and_exit(1 if failed else 0)


if __name__ == "__main__":
    sys.exit(main())

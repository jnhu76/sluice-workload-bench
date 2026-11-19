#!/usr/bin/env python3
"""fio-sluice cost-attribution runner (bench #3).

Thin orchestration over existing tools -- never a new tracing engine
(#622 verdict EXISTING_TOOLS_SUFFICIENT). Modes are separate processes and
separate run directories; CPU-occupying measurements run strictly serially.

  probe       capability admission + environment + binary manifest
  clean       un-instrumented fio runs (fio JSON + rusage)
  pmu         perf stat counting (window = process lifecycle, Option B)
  record      perf record sampling (diag fio + sym engines; SAMPLED only)
  trace       strace -c syscall counting + enter-shape capture (short runs)
  crosscheck  Option C fixed-number_ios runs (window contract)
  analyze     recompute normalized costs from raw artifacts only

Contract details: docs/fio-integration/COST-ATTRIBUTION-METHOD.md and
docs/fio-integration/MEASUREMENT-WINDOW-CONTRACT.md.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from attribution import report as report_mod  # noqa: E402
from attribution import runner as runner_mod  # noqa: E402


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("mode", choices=["probe", "clean", "pmu", "record",
                                     "trace", "crosscheck", "analyze",
                                     "pmu-root", "record-root",
                                     "trace-bpf"])
    ap.add_argument("--results-dir", required=True,
                    help="session directory, e.g. results/attribution/"
                         "session-001")
    ap.add_argument("--scratch-dir", default="build/scratch")
    ap.add_argument("--cases", default=",".join(runner_mod.DEFAULT_CELLS))
    ap.add_argument("--engines",
                    default=",".join(runner_mod.DEFAULT_ENGINES))
    ap.add_argument("--repeats", type=int, default=None)
    ap.add_argument("--no-aux", action="store_true",
                    help="pmu: skip aux event groups")
    ap.add_argument("--clean-ramp", type=int, default=None,
                    help="clean: force ramp_time (0 -> mode 'clean0', the "
                         "only window-matched clean grid for rusage/op)")
    ap.add_argument("--runtime", type=int, default=10)
    ap.add_argument("--ramp-time", type=int, default=2)
    ap.add_argument("--file-size", default="1G")
    ap.add_argument("--cpu-set", default="0-9")
    ap.add_argument("--fio", default="fio")
    ap.add_argument("--fio-diag", default=None)
    ap.add_argument("--sudo-password-env", default="SLUICE_SUDO_PW",
                    help="env var carrying the Owner-granted sudo password "
                         "for root modes; never written to disk")
    args = ap.parse_args()

    def csv(name):
        return [x.strip() for x in name.split(",") if x.strip()]
    args.cases = csv(args.cases)
    args.engines = csv(args.engines)
    for case in args.cases:
        if case not in runner_mod.CELLS:
            ap.error("unknown case %s (known: %s)"
                     % (case, ",".join(runner_mod.CELLS)))
    known = set(runner_mod.engine_specs())
    for engine in args.engines:
        if engine not in known:
            ap.error("unknown engine %s (known: %s)"
                     % (engine, ",".join(sorted(known))))
    return args


def main() -> int:
    args = parse_args()
    root = pathlib.Path(__file__).resolve().parents[1]
    results = root / args.results_dir
    scratch = root / args.scratch_dir
    results.mkdir(parents=True, exist_ok=True)
    scratch.mkdir(parents=True, exist_ok=True)

    if args.mode == "analyze":
        analysis = report_mod.analyze_session(results)
        print("runs=%d discarded=%d sha_mismatch_free=%s verdict=%s"
              % (analysis["runs_total"], analysis["runs_discarded"],
                 analysis["sha_mismatch_free"],
                 analysis["cpu_accounting_verdict"]))
        return 0 if analysis["sha_mismatch_free"] else 1

    sudo_pw = None
    if args.mode in ("pmu-root", "record-root", "trace-bpf"):
        sudo_pw = os.environ.get(args.sudo_password_env)
        if not sudo_pw:
            print("root mode requires --sudo-password-env variable to be set")
            return 2
    session = runner_mod.Session(
        results, scratch, cpu_set=args.cpu_set, runtime_s=args.runtime,
        ramp_s=args.ramp_time, file_size=args.file_size, fio=args.fio,
        fio_diag=args.fio_diag, sudo_pw=sudo_pw)
    session.ensure_layout()

    if args.mode == "probe":
        session.write_protocol(sys.argv[1:])
        matrix = session.write_environment_and_capability()
        session.write_binary_manifest()
        print("cpu_accounting_verdict:", matrix["cpu_accounting_verdict"])
        for row in ("user_instructions", "user_cycles", "kernel_cycles",
                    "ebpf_bpftrace", "io_uring_tracepoints"):
            print("  %-22s %s" % (row, matrix[row]["status"]))
        return 0

    # Exit code reflects THIS invocation only: the manifest is reloaded
    # across modes, and historical failed receipts (kept on disk) must not
    # fail a later, successful mode invocation.
    start = len(session.manifest_runs)
    if args.mode == "clean":
        session.mode_clean(args.cases, args.engines,
                           repeats=args.repeats or 3,
                           ramp=args.clean_ramp)
    elif args.mode == "pmu":
        session.mode_pmu(args.cases, args.engines,
                         repeats=args.repeats or 2,
                         with_aux=not args.no_aux)
    elif args.mode == "record":
        session.mode_record(args.cases, args.engines)
    elif args.mode == "trace":
        session.mode_trace(args.cases, args.engines)
    elif args.mode == "crosscheck":
        session.mode_crosscheck(args.engines)
    elif args.mode == "pmu-root":
        session.mode_pmu_root(args.cases, args.engines,
                              repeats=args.repeats or 2)
    elif args.mode == "record-root":
        session.mode_record_root(args.cases, args.engines,
                                 kernel_cycles=not args.no_aux)
    elif args.mode == "trace-bpf":
        session.mode_trace_bpf(args.cases, args.engines)

    session.write_sha_manifest()
    new_runs = session.manifest_runs[start:]
    for entry in (new_runs or session.manifest_runs)[-8:]:
        print("%-52s %s" % (entry["run_id"], entry["status"]))
    blocked = [e for e in new_runs if e["status"] != "ok"]
    print("runs this invocation: %d (non-ok: %d); session total: %d"
          % (len(new_runs), len(blocked), len(session.manifest_runs)))
    return 1 if any(e["status"] == "fail" for e in new_runs) else 0


if __name__ == "__main__":
    sys.exit(main())

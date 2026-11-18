"""Run the curated workload matrix and retain one JSONL record per invocation."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys


CASES = {
    "seq-read": ("seq-read", (1, 16), ()),
    "random-read-d1": ("random-read", (1,), ()),
    "random-read-concurrent": ("random-read", (4, 16), ()),
    "mixed-4k": ("mixed", (1, 16), ()),
    "mixed-size": ("mixed-size", (1, 16), ("--rate", "8000")),
    "background-write": ("background-write", (1, 16), ()),
}


def variants(cases, backends, requests, scratch, seed):
    for name in cases:
        scenario, depths, extras = CASES[name]
        workload = [
            "--scenario", scenario, "--requests", str(requests), "--seed", str(seed),
            "--scratch-dir", str(scratch), *extras,
        ]
        for backend in backends:
            blocking = backend in ("posix", "direct")
            if blocking and name == "random-read-concurrent":
                continue
            for depth in (1,) if blocking else depths:
                execution = ["--backend", backend, "--depth", str(depth)]
                if backend == "threadpool":
                    execution += ["--workers", str(min(depth, 4))]
                yield name, workload + execution
                if not blocking and depth == 16 and name in ("mixed-size", "background-write"):
                    yield name, workload + execution + ["--max-inflight-bytes", "131072"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scratch-dir", type=Path, default=Path("."))
    parser.add_argument("--case", choices=CASES, action="append")
    parser.add_argument("--backend", choices=("posix", "direct", "threadpool", "uring"), action="append")
    parser.add_argument("--requests", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    if args.repeat < 1 or args.requests < 2:
        parser.error("--repeat must be positive and --requests must be at least 2")

    binary = args.binary.resolve()
    binary_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
    workload_cases = list(dict.fromkeys(args.case or CASES))
    backends = list(dict.fromkeys(args.backend or ("posix", "direct", "threadpool", "uring")))
    roster = list(variants(workload_cases, backends, args.requests, args.scratch_dir.resolve(), args.seed))

    if not roster:
        parser.error("selected backends and cases have no compatible runs")

    traces = {}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        for trial in range(1, args.repeat + 1):
            for name, command in roster if trial % 2 else reversed(roster):
                run = subprocess.run([binary, *command], capture_output=True, text=True)
                if run.returncode not in (0, 77):
                    sys.exit(f"{name}: {' '.join(command)}\n{run.stderr}")
                row = json.loads(run.stdout)
                expected_status = "ok" if run.returncode == 0 else "unavailable"
                if row["status"] != expected_status:
                    sys.exit(f"{name}: runner exit and status disagree")
                if expected_status == "ok":
                    trace = row["workload"]["trace_id"]
                    if traces.setdefault(name, trace) != trace:
                        sys.exit(f"{name}: logical trace differs across replay arms")
                row.update(case=name, trial=trial, arguments=command, runner_sha256=binary_hash)
                output.write(json.dumps(row, separators=(",", ":")) + "\n")
                output.flush()


if __name__ == "__main__":
    main()

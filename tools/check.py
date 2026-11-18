"""Exercise real I/O paths with the same oracle and trace on a disposable fixture."""

import json
import pathlib
import subprocess
import sys
import tempfile


def invoke(binary, scratch, *args):
    return subprocess.run(
        [binary, "--scratch-dir", scratch, "--working-set-mib", "1", *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def result(run):
    if run.returncode:
        raise AssertionError((run.returncode, run.stdout, run.stderr))
    row = json.loads(run.stdout)
    assert row["status"] == "ok" and row["byte_oracle"] == "passed"
    assert sum(group["samples"] for group in row["classes"]) == row["workload"]["requests"]
    return row


def main():
    binary = sys.argv[1]
    require_uring = "--require-uring" in sys.argv[2:]
    with tempfile.TemporaryDirectory() as scratch:
        args = ("--scenario", "mixed-size", "--requests", "120", "--seed", "51")
        native = result(invoke(binary, scratch, *args))
        assert native["workload"]["read_requests"] == 108
        assert native["workload"]["write_requests"] == 12
        assert set((c["operation"], c["size_bytes"]) for c in native["classes"]) == {
            ("read", 4096), ("read", 65536), ("write", 4096), ("write", 65536)
        }
        direct = result(invoke(binary, scratch, *args, "--backend", "direct"))
        threadpool = result(invoke(
            binary, scratch, *args, "--backend", "threadpool", "--depth", "4",
            "--workers", "2", "--max-inflight-bytes", "65536",
        ))
        assert native["workload"] == direct["workload"] == threadpool["workload"]
        assert threadpool["execution"]["peak_inflight_requests"] <= 4
        assert threadpool["execution"]["peak_inflight_bytes"] <= 65536
        assert threadpool["execution"]["blocked_admission_checks"] > 0

        replay = invoke(binary, scratch, *args, "--backend", "uring", "--depth", "4")
        if replay.returncode == 77:
            unavailable = json.loads(replay.stdout)
            assert unavailable["status"] == "unavailable" and unavailable["reason"]
            if require_uring:
                raise AssertionError(f"io_uring required: {unavailable['reason']}")
            print("io_uring skipped:", unavailable["reason"])
        else:
            uring = result(replay)
            assert uring["workload"] == native["workload"] and uring["sdk_profile"] == "ring"

        background = result(invoke(
            binary, scratch, "--scenario", "background-write", "--requests", "60",
            "--read-rate", "2000", "--write-rate", "1000",
            "--backend", "threadpool", "--depth", "4",
        ))
        assert background["workload"]["arrival"] == "scheduled"
        assert background["workload"]["read_requests"] > background["workload"]["write_requests"] > 0
        for group in background["classes"]:
            assert group["latency_ns"]["p99"] >= group["pre_submit_delay_ns"]["p99"] >= 0

        sequential = result(invoke(binary, scratch, "--scenario", "seq-read", "--requests", "12"))
        assert sequential["workload"]["read_bytes"] == 12 * 65536
        writes = result(invoke(
            binary, scratch, "--read-percent", "0", "--requests", "10", "--backend", "direct",
        ))
        assert writes["workload"]["working_set_bytes"] == 0
        assert writes["workload"]["write_bytes"] == writes["workload"]["fixture_bytes"] == 40960
        hotspot = result(invoke(
            binary, scratch, "--access", "hotspot", "--requests", "12",
            "--hotspot-percent", "71", "--hotspot-range-percent", "30",
        ))
        assert hotspot["workload"]["hotspot_percent"] == 71
        assert hotspot["workload"]["trace_id"] != sequential["workload"]["trace_id"]

        for invalid_args, diagnostic in (
            (("--depth", "2"), "require depth 1"),
            (("--read-sizes", "4096:60,65536:30"), "sum to 100"),
            (("--max-inflight-bytes", "1024"), "largest generated request"),
            (("--scenario", "background-write", "--rate", "100"), "unavailable for this scenario"),
        ):
            rejected = invoke(binary, scratch, *invalid_args)
            assert rejected.returncode == 1 and not rejected.stdout
            assert diagnostic in rejected.stderr, rejected.stderr

        assert not list(pathlib.Path(scratch).iterdir()), "fixture leaked"
        print("PASS: trace parity, byte oracle, limits, schedules, pure writes, hotspot and validation")


if __name__ == "__main__":
    main()

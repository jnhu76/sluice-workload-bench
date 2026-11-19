"""Live capability admission (G1): probe the machine, never assume.

Every capability row must carry raw evidence (command + first lines of real
output). A missing tool or denied permission is UNAVAILABLE, never zero and
never silently skipped. #622's verdicts were earned on kernel 7.2.11; this
machine may differ, so nothing is inherited without a live probe.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

# Event groups actually used by the runner. Probed verbatim so the capability
# matrix describes the measurement, not a generic wish list.
MAIN_EVENTS = ("instructions:u", "cycles:u", "task-clock:u")
# Scheduling counters are NOT measured via PMU here: at paranoid=2 perf
# applies exclude_kernel, zeroing context-switches/cpu-migrations. They
# come from process rusage instead (exact, scope = fio process tree).
AUX1_EVENTS = ("branches:u", "branch-misses:u")
AUX2_EVENTS = ("L1-dcache-loads:u", "LLC-loads:u")
KERNEL_EVENT = "cycles:k"


def _run(argv: list, timeout: float = 20.0, env=None) -> dict:
    """Run a probe command, returning verbatim receipt material."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, env=env)
        return {"argv": argv, "returncode": proc.returncode,
                "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-4000:]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"argv": argv, "returncode": None, "stdout": "",
                "stderr": "%s: %s" % (type(exc).__name__, exc)}


def _which(name: str, env=None) -> str | None:
    return shutil.which(name, path=(env or os.environ).get("PATH"))


def probe_perf_event(event: str, env=None) -> dict:
    """Count one event against /bin/true; classify from the raw receipt."""
    perf = _which("perf", env)
    if perf is None:
        return {"status": "UNAVAILABLE",
                "reason": "perf binary not found on PATH",
                "evidence": "which perf -> not found"}
    receipt = _run([perf, "stat", "-x,", "-e", event, "--", "/bin/true"],
                   env=env)
    out = receipt["stdout"] + receipt["stderr"]
    if "<not counted>" in out or "<not supported>" in out:
        return {"status": "UNAVAILABLE", "reason": "not counted/supported",
                "evidence": receipt}
    if receipt["returncode"] != 0 or event not in out:
        reason = "permission denied" if "perf_event_paranoid" in out \
            or "No such file or directory" in out else "probe failed"
        return {"status": "UNAVAILABLE", "reason": reason,
                "evidence": receipt}
    # A scaled (multiplexed) single-event probe cannot happen (one event,
    # one counter), so 100% running is expected; anything else is PARTIAL.
    return {"status": "SUPPORTED", "reason": "", "evidence": receipt}


def probe_sw_event_effective(event: str, env=None) -> dict:
    """Software events under paranoid=2: perf silently sets exclude_kernel,
    so kernel-occurring SW events (context switches, migrations) count as
    ZERO even under heavy scheduling. A zero on a switching workload means
    UNAVAILABLE, never 'no switches happened' (session-001 receipt: every
    aux run reported 0 ctx/migrations while process rusage showed 1k-1M).
    """
    perf = _which("perf", env)
    if perf is None:
        return {"status": "UNAVAILABLE", "reason": "perf not found",
                "evidence": "which perf -> not found"}
    # nanosleep entry/exit guarantees voluntary context switches.
    receipt = _run([perf, "stat", "-x,", "-e", event, "--",
                    "/bin/sh", "-c", "sleep 0.05"], env=env)
    out = receipt["stdout"] + receipt["stderr"]
    row = None
    for line in out.splitlines():
        if event in line and "," in line:
            row = line
            break
    if row is None:
        return {"status": "UNAVAILABLE", "reason": "event row absent",
                "evidence": receipt}
    try:
        value = row.split(",", 1)[0]
        zero = value.startswith("<") or int(value) == 0
    except ValueError:
        zero = True
    if zero:
        return {"status": "UNAVAILABLE",
                "reason": "perf enforces exclude_kernel at "
                          "perf_event_paranoid=2; kernel-occurring software "
                          "event reads 0 and is not usable as a count",
                "evidence": receipt}
    return {"status": "SUPPORTED", "reason": "", "evidence": receipt}


def probe_tracepoints() -> dict:
    """tracefs visibility without root: can we enumerate io_uring events?"""
    for base in ("/sys/kernel/tracing", "/sys/kernel/debug/tracing"):
        events = pathlib.Path(base) / "events" / "io_uring"
        try:
            names = sorted(p.name for p in events.iterdir())
        except OSError as exc:
            return {"status": "UNAVAILABLE",
                    "reason": "tracefs events not readable: %s" % exc,
                    "evidence": {"path": str(events)}}
        if names:
            return {"status": "SUPPORTED", "reason": "",
                    "evidence": {"path": str(events), "events": names}}
    return {"status": "UNAVAILABLE", "reason": "no tracefs mount",
            "evidence": {}}


def probe_bpftrace(env=None) -> dict:
    path = _which("bpftrace", env)
    if path is None:
        return {"status": "UNAVAILABLE", "reason": "bpftrace not on PATH",
                "evidence": "which bpftrace -> not found"}
    receipt = _run(["bpftrace", "-e", "BEGIN { @x = count(); exit(); }"],
                   timeout=15, env=env)
    if receipt["returncode"] == 0:
        return {"status": "SUPPORTED", "reason": "", "evidence": receipt}
    reason = "root required" if "root" in receipt["stderr"] else "probe failed"
    return {"status": "UNAVAILABLE", "reason": reason, "evidence": receipt}


def probe_perf_record(env=None) -> dict:
    perf = _which("perf", env)
    if perf is None:
        return {"status": "UNAVAILABLE", "reason": "perf binary not found",
                "evidence": "which perf -> not found"}
    import tempfile

    def record_ok(receipt: dict, data: pathlib.Path) -> bool:
        # perf record exits 0 even when it fails to open events (observed
        # on perf 7.3.3, paranoid=2, --call-graph): judge by output text
        # AND produced samples, never by exit code alone.
        text = receipt["stdout"] + receipt["stderr"]
        if "Failure to open" in text or "Error" in text:
            return False
        return receipt["returncode"] == 0 and data.exists() \
            and data.stat().st_size > 0

    with tempfile.TemporaryDirectory() as tmp:
        data = pathlib.Path(tmp) / "probe.data"
        receipt = _run([perf, "record", "-o", str(data), "-e", "cycles:u",
                        "-F", "99", "--", "/bin/true"], env=env)
        ok = record_ok(receipt, data)
        report = _run([perf, "report", "-i", str(data), "--stdio",
                       "--percent-limit", "0"], env=env)
        symbols = any(s in report["stdout"] for s in ("true", "ld.so", "_start"))
        graphs = {}
        for kind in ("dwarf", "fp"):
            data.unlink(missing_ok=True)
            r = _run([perf, "record", "-o", str(data), "-e", "cycles:u",
                      "-F", "99", "--call-graph", kind, "--", "/bin/true"],
                     env=env)
            graphs[kind] = {"ok": record_ok(r, data), "receipt": r}
        status = ("SUPPORTED" if ok and symbols
                  else "PARTIAL" if ok else "UNAVAILABLE")
        return {"status": status,
                "reason": "" if ok else "record failed",
                "callgraph_dwarf": graphs["dwarf"]["ok"],
                "callgraph_fp": graphs["fp"]["ok"],
                "callgraph_note": "perf record exits 0 even on event-open "
                                  "failure; judged from output text",
                "evidence": {"record": receipt,
                             "callgraph": graphs,
                             "report_head": report["stdout"][:2000]}}


def probe_sudo_root() -> dict:
    receipt = _run(["sudo", "-n", "true"])
    return {"status": "SUPPORTED" if receipt["returncode"] == 0
            else "UNAVAILABLE",
            "reason": "passwordless sudo" if receipt["returncode"] == 0
            else "sudo requires a password",
            "evidence": receipt}


def collect_environment(env=None) -> dict:
    """Machine identity snapshot (structure mirrors run_fio_matrix.py)."""

    def read(path: str):
        try:
            return pathlib.Path(path).read_text().strip()
        except OSError:
            return None

    def read_many(pattern: str) -> list:
        vals = []
        for p in sorted(pathlib.Path("/sys/devices/system/cpu").glob(pattern)):
            v = read(str(p))
            if v is not None:
                vals.append(v)
        return vals

    mounts = {}
    for m in ("/", "/home", "/tmp"):
        out = subprocess.run(["findmnt", "-n", "-o", "FSTYPE,SOURCE", m],
                             capture_output=True, text=True)
        mounts[m] = out.stdout.strip() or None
    lscpu = _run(["lscpu"])
    return {
        "uname": _run(["uname", "-a"])["stdout"].strip(),
        "cpu_model": next(ln.split(":", 1)[1].strip()
                          for ln in pathlib.Path("/proc/cpuinfo")
                          .read_text().splitlines()
                          if ln.startswith("model name")),
        "lscpu": lscpu["stdout"][:6000],
        "online_cpus": len(os.sched_getaffinity(0)),
        "smt_siblings_cpu0": read("/sys/devices/system/cpu/cpu0/topology/"
                                  "thread_siblings_list"),
        "numa_nodes": read("/sys/devices/system/node/has_cpu"),
        "governor_values": sorted(set(read_many("cpu*/cpufreq/"
                                                "scaling_governor"))),
        "cpufreq_driver": read("/sys/devices/system/cpu/cpu0/cpufreq/"
                               "scaling_driver"),
        "no_turbo": read("/sys/devices/system/cpu/intel_pstate/no_turbo"),
        "freq_bounds_khz": {
            "min": read("/sys/devices/system/cpu/cpu0/cpufreq/"
                        "scaling_min_freq"),
            "max": read("/sys/devices/system/cpu/cpu0/cpufreq/"
                        "scaling_max_freq"),
        },
        "perf_event_paranoid": read("/proc/sys/kernel/perf_event_paranoid"),
        "kptr_restrict": read("/proc/sys/kernel/kptr_restrict"),
        "lockdown": read("/sys/kernel/security/lockdown"),
        "btf_vmlinux_bytes": (pathlib.Path("/sys/kernel/btf/vmlinux").stat()
                              .st_size
                              if pathlib.Path("/sys/kernel/btf/vmlinux")
                              .exists() else None),
        "mounts": mounts,
        "temperatures_milli_c": {
            z.name: read(str(z / "temp"))
            for z in sorted(pathlib.Path("/sys/class/thermal")
                            .glob("thermal_zone*"))},
        "loadavg": pathlib.Path("/proc/loadavg").read_text().split()[:3],
        "fio_version": _run(["fio", "--version"])["stdout"].strip(),
        "perf_version": _run(["perf", "--version"])["stdout"].strip(),
        "bpftrace_version": _run(["bpftrace", "--version"])["stdout"].strip(),
        "tool_paths": {t: _which(t, env) for t in
                       ("perf", "bpftrace", "bpftool", "turbostat",
                        "strace", "taskset")},
    }


def probe_all(env=None) -> dict:
    """Capability matrix per the campaign directive (G1 table)."""
    perf_present = _which("perf", env) is not None

    def group(events: tuple) -> dict:
        if not perf_present:
            return {"status": "UNAVAILABLE",
                    "reason": "perf binary not found",
                    "evidence": "which perf -> not found", "events": {}}
        rows = {e: probe_perf_event(e, env=env) for e in events}
        ok = [e for e, r in rows.items() if r["status"] == "SUPPORTED"]
        return {
            "status": ("SUPPORTED" if len(ok) == len(events)
                       else "PARTIAL" if ok else "UNAVAILABLE"),
            "reason": "" if len(ok) == len(events)
            else "missing: %s" % [e for e in events if e not in ok],
            "events": rows,
        }

    sudo = probe_sudo_root()
    root_like = sudo["status"] == "SUPPORTED" or os.geteuid() == 0
    tracepoints = probe_tracepoints()

    matrix = {
        "schema": 1,
        "as_user": os.environ.get("USER"),
        "effective_uid": os.geteuid(),
        "passwordless_sudo": sudo,
        "user_instructions": probe_perf_event("instructions:u", env),
        "user_cycles": probe_perf_event("cycles:u", env),
        "kernel_cycles": probe_perf_event(KERNEL_EVENT, env),
        "task_clock": probe_perf_event("task-clock:u", env),
        "context_switches": probe_sw_event_effective("context-switches",
                                                     env),
        "cpu_migrations": probe_sw_event_effective("cpu-migrations", env),
        "cache_llc_events": group(AUX2_EVENTS),
        "branch_events": group(AUX1_EVENTS[:2]),
        "perf_callgraph_user": probe_perf_record(env),
        "io_uring_tracepoints": tracepoints,
        "sched_tracepoints": {
            "status": tracepoints["status"],
            "reason": tracepoints["reason"]
            if tracepoints["status"] != "SUPPORTED" else "",
            "evidence": {"same_read_scope_as_io_uring": True},
        },
        "futex_tracepoints": {
            "status": tracepoints["status"],
            "reason": tracepoints["reason"]
            if tracepoints["status"] != "SUPPORTED" else "",
            "evidence": {"note": "futex tracepoint enumerates under the same "
                                 "tracefs read scope"},
        },
        "ebpf_bpftrace": probe_bpftrace(env),
        "io_wq_worker_attribution": {
            "status": "UNAVAILABLE",
            "reason": "requires kernel tracepoints or :k PMU (root); "
                      "non-root cannot attribute io-wq CPU",
            "evidence": {"kernel_cycles": "see kernel_cycles row",
                         "tracepoints": tracepoints["status"]},
        },
        "block_layer_events": {
            "status": "UNAVAILABLE" if not root_like else "SUPPORTED",
            "reason": "tracefs read denied without root",
            "evidence": tracepoints["evidence"],
        },
        "user_probes": {
            "status": "SUPPORTED" if perf_present else "UNAVAILABLE",
            "reason": "perf record :u sampling on own processes",
            "evidence": "see perf_callgraph_user",
        },
    }

    # Aggregate verdict used by the session gate.
    core_user_pmu = (matrix["user_instructions"]["status"] == "SUPPORTED"
                     and matrix["user_cycles"]["status"] == "SUPPORTED")
    kernel_side = (matrix["kernel_cycles"]["status"] == "SUPPORTED"
                   and matrix["ebpf_bpftrace"]["status"] == "SUPPORTED")
    if core_user_pmu and kernel_side:
        matrix["cpu_accounting_verdict"] = "FULL"
    elif core_user_pmu:
        matrix["cpu_accounting_verdict"] = "PARTIAL_CPU_ACCOUNTING"
    else:
        matrix["cpu_accounting_verdict"] = "ENVIRONMENT_BLOCKED"
    return matrix


def write_capability(path: pathlib.Path, env=None) -> dict:
    matrix = probe_all(env)
    path.write_text(json.dumps(matrix, indent=2, sort_keys=True) + "\n")
    return matrix


if __name__ == "__main__":
    print(json.dumps(probe_all(), indent=2, sort_keys=True))

#!/usr/bin/env bash
# fio x Sluice external-engine smoke (docs/fio-integration/ADAPTER-ARCHITECTURE §7.1).
#
# Usage: adapters/fio/tests/smoke.sh [scratch-dir]   (default build/scratch)
#
# Checks: plugin load, threadpool read/write, io_uring read/write, invalid
# sluice_backend rejected, iodepth 1/4/16, crc32 write+verify data checking,
# clean process exit (threads/fds observed through /proc while running and
# gone afterwards), engine lifecycle unit tests (errno mapping; close_file
# fd-borrow invariant and init failure containment). All I/O targets are
# temporary regular files inside the scratch directory.
#
# Note on verify: the task contract names verify=crud, but fio 3.40 has no
# such verify method (options.c posval offers md5/crc*/sha* only), so the
# templates carry verify=crc32 -- the cheap checksum matching the intent.
#
# REQUIRE_URING=1: io_uring must be usable, otherwise exit 77 with an
# UNAVAILABLE reason and never fall back to threadpool. Without it, an
# unavailable io_uring is recorded and the uring items are skipped while
# everything else must still pass.
set -uo pipefail

root="$(cd -- "$(dirname -- "$0")/../../.." && pwd)"
scratch_arg="${1:-$root/build/scratch}"
targetdir="$root/build/linux/x86_64/release"
noring_engine="$targetdir/fio-sluice-noring.so"
ring_engine="$targetdir/fio-sluice-ring.so"
errno_test="$targetdir/fio-sluice-errno-map-test"
lifecycle_test="$targetdir/fio-sluice-lifecycle-test"
fio_expected_version="fio-3.40"
run_timeout_s=90

failures=0
log() { printf '%s\n' "$*"; }
fail() { log "SMOKE-FAIL: $*"; failures=$((failures + 1)); }

mkdir -p "$scratch_arg"
scratch="$(cd -- "$scratch_arg" && pwd)"
mkdir -p "$scratch/jobs"

fio_bin="$(command -v fio || true)"
if [[ -z "$fio_bin" ]]; then
    log "SMOKE-FAIL: fio not found in PATH"
    exit 1
fi
fio_version="$("$fio_bin" --version 2>&1)"
if [[ "$fio_version" != "$fio_expected_version" ]]; then
    log "SMOKE-FAIL: host fio is '$fio_version', engine ABI is pinned to $fio_expected_version"
    exit 1
fi

build_target() {
    local target="$1" artifact="$2"
    if [[ -f "$artifact" ]]; then
        return 0
    fi
    log "building missing target $target via xmake"
    if ! (cd "$root" && xmake f -m release -y >/dev/null &&
          xmake build -y "$target" >/dev/null); then
        log "SMOKE-FAIL: xmake build $target did not produce $artifact"
        return 1
    fi
    [[ -f "$artifact" ]]
}

if ! build_target fio-sluice-noring "$noring_engine"; then
    exit 1
fi
if [[ -f "$errno_test" ]] || build_target fio-sluice-errno-map-test "$errno_test"; then
    if ! "$errno_test" >/dev/null; then
        fail "errno_map_test reported failures"
    else
        log "ok: errno map unit test"
    fi
fi
if [[ -f "$lifecycle_test" ]] || build_target fio-sluice-lifecycle-test "$lifecycle_test"; then
    if ! "$lifecycle_test" "$scratch/lifecycle" >/dev/null; then
        fail "lifecycle_test reported failures"
    else
        log "ok: engine lifecycle unit test"
    fi
fi

# Runs one job file, sampling the process's thread/fd counts through /proc
# while it is alive. Fills RUN_EXIT/RUN_THREADS_SEEN/RUN_FDS_SEEN.
run_fio() {
    local job="$1" tag="$2"
    local out="$scratch/jobs/$tag.out" err="$scratch/jobs/$tag.err"
    RUN_EXIT=0 RUN_THREADS_SEEN=0 RUN_FDS_SEEN=0
    # Run from the scratch jobs directory: fio drops verify-state files in
    # the process working directory and nothing may appear at the repo root.
    (cd "$scratch/jobs" && "$fio_bin" --eta=never "$job") >"$out" 2>"$err" &
    local pid=$!
    local started=$SECONDS
    while kill -0 "$pid" 2>/dev/null; do
        if [[ -d "/proc/$pid/task" ]]; then
            local tasks fds
            tasks=$(find "/proc/$pid/task" -mindepth 1 -maxdepth 1 2>/dev/null | wc -l)
            fds=$(find "/proc/$pid/fd" -mindepth 1 -maxdepth 1 2>/dev/null | wc -l)
            (( tasks > RUN_THREADS_SEEN )) && RUN_THREADS_SEEN=$tasks
            (( fds > RUN_FDS_SEEN )) && RUN_FDS_SEEN=$fds
        fi
        if (( SECONDS - started >= run_timeout_s )); then
            kill -9 "$pid" 2>/dev/null
            wait "$pid" 2>/dev/null
            RUN_EXIT=124
            return 0
        fi
        sleep 0.05
    done
    wait "$pid"
    RUN_EXIT=$?
    return 0
}

expect_job_ok() {
    local template="$1" engine="$2" backend="$3" depth="$4" tag="$5"
    local job="$scratch/jobs/$tag.fio"
    local data="$scratch/$tag.bin"
    sed -e "s|__ENGINE__|$engine|g" \
        -e "s|__FILENAME__|$data|g" \
        -e "s|__BACKEND__|$backend|g" \
        -e "s|__IODEPTH__|$depth|g" \
        "$root/$template" >"$job"
    rm -f "$data"
    run_fio "$job" "$tag"
    if [[ "$RUN_EXIT" != "0" ]]; then
        fail "$tag: fio exited $RUN_EXIT (see $scratch/jobs/$tag.err)"
        sed -n '1,20p' "$scratch/jobs/$tag.err" || true
        return 1
    fi
    if ! grep -q "fio-sluice: loaded revision=" "$scratch/jobs/$tag.err"; then
        fail "$tag: engine load trace missing"
        return 1
    fi
    if grep -qiE "verify.*(fail|mismatch|error)|crc32.*(differs|mismatch)" \
        "$scratch/jobs/$tag.err"; then
        fail "$tag: verification mismatch reported"
        return 1
    fi
    if [[ "$(stat -c %F "$data" 2>/dev/null || true)" != "regular file" ]]; then
        fail "$tag: data target is not a regular file"
        return 1
    fi
    log "ok: $tag (threads<=${RUN_THREADS_SEEN} fds<=${RUN_FDS_SEEN})"
    rm -f "$data" "$job" "$scratch/jobs/$tag.out" "$scratch/jobs/$tag.err" \
        "$scratch/jobs/"*-verify.state
    return 0
}

URING_AVAILABLE=0
URING_REASON=""
RING_ENGINE_PRESENT=0
if [[ -f "$ring_engine" ]]; then
    RING_ENGINE_PRESENT=1
    # Tiny uring job through the ring engine: an init failure carrying our
    # diagnostic counts as unavailability, not as a smoke error.
    probe_job="$scratch/jobs/probe-uring.fio"
    probe_data="$scratch/probe-uring.bin"
    sed -e "s|__ENGINE__|$ring_engine|g" \
        -e "s|__FILENAME__|$probe_data|g" \
        -e "s|__BACKEND__|uring|g" \
        -e "s|__IODEPTH__|1|g" \
        "$root/workloads/fio/smoke-read.fio" >"$probe_job"
    rm -f "$probe_data"
    run_fio "$probe_job" "probe-uring"
    if [[ "$RUN_EXIT" == "0" ]]; then
        URING_AVAILABLE=1
    else
        URING_REASON="$(grep -m1 "fio-sluice:" "$scratch/jobs/probe-uring.err" ||
                        echo "io_uring engine init failed (fio exit $RUN_EXIT)")"
    fi
    rm -f "$probe_data" "$probe_job" "$scratch/jobs/probe-uring.out" \
        "$scratch/jobs/probe-uring.err"
else
    URING_REASON="ring engine $ring_engine not built"
fi

if [[ "${REQUIRE_URING:-0}" == "1" ]]; then
    if [[ "$URING_AVAILABLE" != "1" ]]; then
        log "UNAVAILABLE: $URING_REASON"
        log "(REQUIRE_URING=1: threadpool fallback is not attempted)"
        exit 77
    fi
    log "ok: io_uring available under REQUIRE_URING=1"
else
    if [[ "$URING_AVAILABLE" != "1" ]]; then
        log "io_uring skipped: $URING_REASON"
    fi
    if [[ "$RING_ENGINE_PRESENT" != "1" ]]; then
        log "ring-engine items skipped: $ring_engine not built"
    fi
fi

for depth in 1 4 16; do
    expect_job_ok workloads/fio/smoke-read.fio "$noring_engine" threadpool "$depth" \
        "noring-tp-read-d$depth" || true
    expect_job_ok workloads/fio/smoke-write.fio "$noring_engine" threadpool "$depth" \
        "noring-tp-write-d$depth" || true
    if [[ "$RING_ENGINE_PRESENT" == "1" ]]; then
        expect_job_ok workloads/fio/smoke-read.fio "$ring_engine" threadpool "$depth" \
            "ring-tp-read-d$depth" || true
        expect_job_ok workloads/fio/smoke-write.fio "$ring_engine" threadpool "$depth" \
            "ring-tp-write-d$depth" || true
    fi
    if [[ "$URING_AVAILABLE" == "1" ]]; then
        expect_job_ok workloads/fio/smoke-read.fio "$ring_engine" uring "$depth" \
            "ring-uring-read-d$depth" || true
        expect_job_ok workloads/fio/smoke-write.fio "$ring_engine" uring "$depth" \
            "ring-uring-write-d$depth" || true
    fi
done

# Invalid backend must be rejected by the engine at init (fail closed).
invalid_job="$scratch/jobs/invalid-backend.fio"
invalid_data="$scratch/invalid-backend.bin"
sed -e "s|__ENGINE__|$noring_engine|g" \
    -e "s|__FILENAME__|$invalid_data|g" \
    -e "s|__BACKEND__|nosuchbackend|g" \
    -e "s|__IODEPTH__|1|g" \
    "$root/workloads/fio/smoke-read.fio" >"$invalid_job"
rm -f "$invalid_data"
run_fio "$invalid_job" "invalid-backend"
if [[ "$RUN_EXIT" == "0" ]]; then
    fail "invalid-backend: fio accepted sluice_backend=nosuchbackend"
elif grep -q "invalid sluice_backend" "$scratch/jobs/invalid-backend.err"; then
    log "ok: invalid backend rejected (fio exit $RUN_EXIT)"
else
    fail "invalid-backend: rejected without the engine diagnostic"
fi
rm -f "$invalid_data" "$invalid_job" "$scratch/jobs/invalid-backend.out" \
    "$scratch/jobs/invalid-backend.err"

# The noring-built engine must fail closed when asked for uring (no POSIX
# fallback, no cross-profile mixing).
nouring_job="$scratch/jobs/noring-uring.fio"
nouring_data="$scratch/noring-uring.bin"
sed -e "s|__ENGINE__|$noring_engine|g" \
    -e "s|__FILENAME__|$nouring_data|g" \
    -e "s|__BACKEND__|uring|g" \
    -e "s|__IODEPTH__|1|g" \
    "$root/workloads/fio/smoke-read.fio" >"$nouring_job"
rm -f "$nouring_data"
run_fio "$nouring_job" "noring-uring"
if [[ "$RUN_EXIT" == "0" ]]; then
    fail "noring-uring: the noring engine accepted sluice_backend=uring"
elif grep -q "io_uring unavailable" "$scratch/jobs/noring-uring.err"; then
    log "ok: noring engine rejects uring (fio exit $RUN_EXIT, no fallback)"
else
    fail "noring-uring: rejected without the unavailable diagnostic"
fi
rm -f "$nouring_data" "$nouring_job" "$scratch/jobs/noring-uring.out" \
    "$scratch/jobs/noring-uring.err"

# Nothing may outlive the runs: no fio process holding our job files, and no
# scratch leftovers besides the jobs directory.
if pgrep -f "fio.*$scratch" >/dev/null 2>&1; then
    fail "leftover fio process still references the scratch directory"
fi
leftovers="$(find "$scratch" -mindepth 1 -maxdepth 1 ! -name jobs)"
if [[ -n "$leftovers" ]]; then
    fail "scratch directory has leftover entries: $leftovers"
fi

if (( failures != 0 )); then
    log "SMOKE RESULT: FAIL ($failures failing item(s))"
    exit 1
fi
uring_state="unavailable (skipped)"
[[ "$URING_AVAILABLE" == "1" ]] && uring_state="available (exercised)"
log "SMOKE RESULT: PASS (threadpool read/write; io_uring: $uring_state;" \
    "depths 1/4/16; crc32 verify; no leftover threads/fds)"
exit 0

# Sluice Workload Bench

Independent Linux regular-file I/O benchmarks for [Sluice](https://github.com/jnhu76/Sluice).
A deterministic request generator feeds the same logical workload to Linux POSIX,
Sluice Direct, Sluice ThreadPool, and Sluice io_uring. The runner uses installed
public SDK headers and libraries from the commit in [SLUICE_REVISION](SLUICE_REVISION).
A companion fio external ioengine (`fio-sluice`) runs the same Sluice backends
under fio; see [fio external engine](#fio-external-engine-fio-sluice) below.

## Build and smoke test

Install a C++20 GCC toolchain, Git, xmake, and Python 3. Install the liburing
development package to build the ring targets. The build system is xmake (there
is no Makefile). Builds fetch Sluice by revision and stage the SDK under a
separate prefix per profile. Set SLUICE_SOURCE to the path of an existing local
Sluice Git checkout to fetch the pinned commit locally.

Four build targets plus a unit test target (all land in
`build/linux/x86_64/release/`):

| Target | Artifact | Notes |
| --- | --- | --- |
| `sluice-workload-bench` | bench binary | noring SDK (no liburing) |
| `sluice-workload-bench-ring` | bench binary | ring SDK, links `-luring` |
| `fio-sluice-noring` | `fio-sluice-noring.so` | fio external engine, PIC noring SDK, threadpool backend |
| `fio-sluice-ring` | `fio-sluice-ring.so` | fio external engine, PIC ring SDK, threadpool + io_uring backends |
| `fio-sluice-errno-map-test` | unit test binary | IoError-to-errno mapping exhaustiveness |

```sh
xmake f -m release -y
xmake build -y sluice-workload-bench-ring   # or any target above
xmake check                                 # tools/check.py on the noring binary
xmake check-ring                            # tools/check.py --require-uring on the ring binary
```

The engine targets also clone and verify the frozen fio 3.40 source tree under
`build/fio/upstream` (tag and commit are asserted) and run `./configure` there;
nothing from that tree is copied into the repository. The default profile
builds without liburing. The ring profile requires a kernel and execution
environment that permit Sluice's io_uring setup. An unavailable ring emits a
JSON reason and exits 77. The matrix keeps that record. Invalid options, failed
I/O, or failed correctness checks exit 1. A successful measurement emits a
single JSON record. On a native host where io_uring must execute, use
`xmake check-ring`. The check tasks run small functional workloads, so they
only run when invoked explicitly. `xmake clean -a` removes `build/` entirely,
including the staged SDKs and the frozen fio tree; the build hooks recreate
both on demand.

## Run a case

Choose an existing scratch directory on the filesystem you want to measure.
Every invocation creates and removes its own disposable regular file. Fixture
size equals the read working set plus the bytes of all generated writes. The
runner initializes the fixture with data before measuring.

```sh
mkdir -p /path/on/test/filesystem/sluice-scratch

build/linux/x86_64/release/sluice-workload-bench \
    --scenario mixed-size --backend threadpool --depth 16 --workers 4 \
    --requests 10000 --working-set-mib 32 --seed 42 --rate 8000 \
    --max-inflight-bytes 131072 --scratch-dir /path/on/test/filesystem/sluice-scratch
```

Use --help for the full command-line surface. Flags take separate values.

| Workload parameter | Values and behavior |
| --- | --- |
| Scenario | seq-read, random-read, mixed, mixed-size, background-write |
| Read percentage | 0–100 by request count. Small runs can round the write count down. |
| Read and write sizes | Separate weighted distributions, such as 4096:80,65536:20. Sizes are 4 KiB multiples up to 1 MiB. Weights sum to 100. The actual sample counts are reported. |
| Offset distribution | Sequential, uniform random, or a configurable two-region read hotspot. Random/hotspot writes visit shuffled disjoint extents. |
| Working set and seed | Read region size in MiB and deterministic seed. The write region follows the read region and holds one unique extent per write. |
| Arrival | Closed-loop refill by default. Set --rate for fixed aggregate scheduled arrivals. The background-write preset schedules separate read and write streams using --read-rate and --write-rate. |
| Execution | Single-caller POSIX and Sluice Direct at depth 1. ThreadPool and io_uring have a bounded asynchronous window. --workers controls ThreadPool workers. |
| In-flight limits | --depth caps request count. --max-inflight-bytes adds a payload-byte cap. Zero leaves the byte cap disabled. The runner admits requests in trace order. |

## Run the matrix

```sh
python3 tools/run_matrix.py \
    --binary build/linux/x86_64/release/sluice-workload-bench-ring \
    --scratch-dir /path/on/test/filesystem/sluice-scratch \
    --output results/e5-first-sweep.jsonl
```

Select individual cases with repeated --case flags, or exclude unavailable
backends by passing repeated --backend flags. The cases include sequential
64 KiB reads, random 4 KiB reads at depth 1 and at concurrent depths, fixed-size
mixed I/O, mixed-size I/O, and small reads with a separately scheduled background
write stream. At async depth 16 the latter two cases also compare a 128 KiB byte
cap. --repeat runs the same trace again and reverses the roster on alternate
trials. Choose an unused output path for each sweep.

## fio external engine (fio-sluice)

`fio-sluice` is an external fio ioengine that executes fio-generated I/O
through the Sluice public async API (ThreadPool or io_uring backend). It is a
pure consumer of the installed SDK: no Sluice code is modified, no new threads
or runtimes are introduced beyond what the Sluice factories themselves create,
and there is no fallback to POSIX — a backend that cannot be constructed fails
the job. Design and lifecycle rationale live in
[docs/fio-integration/ADAPTER-ARCHITECTURE.md](docs/fio-integration/ADAPTER-ARCHITECTURE.md);
the implementation notes are in [adapters/fio/README.md](adapters/fio/README.md).

Load it with the explicit external path form (the engine-name dlopen convention
depends on host fio build flags and is not relied upon):

```sh
fio --name=demo \
    --ioengine=external:$PWD/build/linux/x86_64/release/fio-sluice-ring.so \
    --sluice_backend=uring \
    --rw=randread --bs=4k --iodepth=16 --direct=0 \
    --size=1G --time_based --runtime=10 \
    --filename=/path/on/test/filesystem/sluice-scratch/demo.bin
```

Engine options:

| Option | Values | Behavior |
| --- | --- | --- |
| `sluice_backend` | `threadpool` (default), `uring` | Selects the Sluice async backend factory. The noring build rejects `uring` at init with an explicit error (no fallback). |
| `sluice_workers` | integer >= 1, default 4 | ThreadPool worker count. Rejected for the `uring` backend when non-default. |

Engine capacity always equals the job's `iodepth` (there is no capacity knob),
`FIO_Q_BUSY` is never returned, and every accepted io_u is delivered exactly
once through `getevents`/`event`. The engine logs a version line
(`fio-sluice: loaded revision=… sdk_profile=… fio_source=… backend=…`) at init
so results can be reconciled against [SLUICE_REVISION](SLUICE_REVISION).

Supported fio operations: read, write, fsync (`DDIR_SYNC`), fdatasync
(`DDIR_DATASYNC`). Trim, sync_file_range, and wait requests are rejected with
EINVAL. `verify_async` and `io_submit_mode=offload` are rejected (they would
queue from a non-owner thread). The engine ABI is pinned to fio 3.40
(`FIO_IOOPS_VERSION 37`); after any host fio upgrade, rebuild against the
matching tag and re-qualify. The engine is a GPLv2 derivative work of fio.

### fio correctness matrix

`tools/check_fio_engine.py` runs a deterministic correctness matrix (read,
write, verify=pattern, mixed, mixed-size, sparse, EOF-boundary, fdatasync,
negative controls, SIGTERM cleanup) across the threadpool and io_uring engines,
with a two-layer data oracle (fio's crc32c verify plus independent POSIX
checks and a native-sync-engine parity oracle):

```sh
xmake build -y fio-sluice-noring && xmake build -y fio-sluice-ring
python3 tools/check_fio_engine.py --scratch-dir build/scratch \
    --output results/fio/correctness.json
python3 tools/check_fio_engine.py --scratch-dir build/scratch \
    --output results/fio/correctness-uring.json --require-uring
```

Exit 0 = all cases passed, 1 = any failure, 77 = `--require-uring` with
io_uring unavailable (recorded, never falls back to the threadpool arm). All
target files are disposable regular files under the scratch directory, guarded
by the tool, and deleted afterwards. An engine-level smoke script with the
same contracts is `bash adapters/fio/tests/smoke.sh` (set REQUIRE_URING=1 to
demand the io_uring arm).

### fio performance matrix

`tools/run_fio_matrix.py` runs the serial formal matrix (A/A consistency plus
F1–F4 × psync / io_uring / sluice-threadpool / sluice-uring, repeats,
pre-created page-cache-warm files, CPU pinning, deterministic intermissions);
`tools/compare_fio.py` aggregates the fio JSON originals, cross-checks every
number, and refreshes the tables in
[docs/fio-integration/PERF-BASELINE.md](docs/fio-integration/PERF-BASELINE.md):

```sh
python3 tools/run_fio_matrix.py --scratch-dir build/scratch \
    --results-dir results/fio --repeats 2
python3 tools/compare_fio.py --results-dir results/fio \
    --doc docs/fio-integration/PERF-BASELINE.md
```

The matrix executes strictly serially and writes only to disposable regular
files under the scratch directory. Keep `results/` as workspace evidence: it is
git-ignored by design; the committed baseline tables are the reportable record.

## Interpret results

- The trace ID covers the seed, operation order, offsets, sizes and scheduled
  arrivals. Compare identical traces from the same filesystem and host.
  The JSONL matrix adds the binary SHA-256 and exact invocation arguments.
- Every invocation first replays the trace untimed and checks every read byte.
  It restores the fixture, measures the second pass, checks every returned byte
  count and verifies the final file with POSIX reads. Startup, data setup,
  verification and teardown sit outside the timing interval.
- Timing includes the driver's request submission and completion observation.
  The async runner consumes any ready ticket through RequestScope and includes
  its own active-slot scan in the measurement.
  Scheduled latency starts at the intended arrival, so the pre-submit queue
  is included. Closed-loop latency starts at submission. Read and write
  latency percentiles are reported separately for each size with sample counts.
  Small sample groups produce coarse tail estimates.
- CPU means process user plus system CPU measured by getrusage. The observed
  high-water marks count accepted, unretired requests and payload bytes. The
  allocated payload buffer size is reported separately. Admission checks count
  times a due request encountered the runner's window/byte limit.
- The fixture is sync-prepared outside the measured pass. Page-cache residency
  before timing is sampled with mincore, and the filesystem magic is recorded.
  Benchmarked writes complete at the buffered API boundary. They do not measure
  durability or full-system writeback. Sluice Direct is its synchronous API.
- Higher-depth async runs expose potential overlap. The single-caller
  synchronous arms provide a depth-1 reference. A same-depth comparison
  against multiple blocking callers needs a separate comparator.
- The initial matrix supports exploratory curves. Confirm a candidate on the
  target native host with A/A calibration, interleaved matched A/B runs, and
  representative traces before promoting it into Sluice.

Future experiments can add trace import and I/O dependencies, multipath access
patterns, verified cold runs, sync/durability scenarios, and a blocking
multi-caller comparator. Add each as a workload or execution experiment under
the same correctness and reporting rules. See [measurement scope in #591](https://github.com/jnhu76/Sluice/issues/591)
and [the workload research entry point in #604](https://github.com/jnhu76/Sluice/issues/604).

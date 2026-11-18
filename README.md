# Sluice Workload Bench

Independent Linux regular-file I/O benchmarks for [Sluice](https://github.com/jnhu76/Sluice).
A deterministic request generator feeds the same logical workload to Linux POSIX,
Sluice Direct, Sluice ThreadPool, and Sluice io_uring. The runner uses installed
public SDK headers and libraries from the commit in [SLUICE_REVISION](SLUICE_REVISION).

## Build and smoke test

Install a C++20 GCC toolchain, GNU Make, Git, xmake, and Python 3. Install the
liburing development package to build the ring profile. Builds fetch Sluice by
revision and stage the SDK under a separate prefix per profile. Set
SLUICE_SOURCE to the path of an existing local Sluice Git checkout to fetch the
pinned commit locally.

```sh
make check
make PROFILE=ring check
```

The default profile builds without liburing. The ring profile requires a kernel
and execution environment that permit Sluice's io_uring setup. An unavailable
ring emits a JSON reason and exits 77. The matrix keeps that record. Invalid
options, failed I/O, or failed correctness checks exit 1. A successful
measurement emits a single JSON record. On a native host where io_uring must
execute, pass REQUIRE_URING=1 to make PROFILE=ring check.

## Run a case

Choose an existing scratch directory on the filesystem you want to measure.
Every invocation creates and removes its own disposable regular file. Fixture
size equals the read working set plus the bytes of all generated writes. The
runner initializes the fixture with data before measuring.

```sh
mkdir -p /path/on/test/filesystem/sluice-scratch

build/noring/sluice-workload-bench \
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
    --binary build/ring/sluice-workload-bench \
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

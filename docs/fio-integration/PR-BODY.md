# feat(fio): add external Sluice ioengine for workload benchmarking

Phase 1 of the fio integration for [Sluice](https://github.com/jnhu76/Sluice)
issue [#604](https://github.com/jnhu76/Sluice/issues/604): a `fio` external
ioengine (`fio-sluice`) that executes fio-generated I/O through the **Sluice
public async API** (ThreadPool and io_uring backends), plus deterministic
correctness tooling, a serial four-engine performance matrix, and the xmake
build migration that replaces the Makefile.

**Suggested owner-gate verdict: `PHASE1_CONDITIONAL_GO`** — correctness is
green (smoke, 90/90 correctness matrix in both invocation forms, regression),
the performance gate is `BASELINE_VALID`, and the independent adversarial
review returned `conditional` with all limitations explicitly stated below.
This PR is **research tooling only**: nothing here claims admission of a
synthetic fio workload as #604 G0 (see final section).

Full handover report: [`docs/fio-integration/PHASE1-REPORT.md`](../fio-integration/PHASE1-REPORT.md) (sections A–K + owner-gate ten questions).

## Pinned versions

| Component | Pin | Evidence |
| --- | --- | --- |
| fio (host binary) | `fio-3.40` (`fio-3.40-3.fc44.x86_64`, `/usr/sbin/fio`) | `docs/fio-integration/REPOSITORY-REALITY.md` §5 |
| fio (frozen source tree) | tag `fio-3.40`, commit `ff930c4653ae3952d6b09ab3ec89671aeabf2cbe`, `FIO_IOOPS_VERSION 37` | `xmake.lua:35-36` asserts tag+commit at every engine build |
| Sluice SDK | revision `5d94f233967c3aef993eb19ba9e3252c532b0951` | [`SLUICE_REVISION`](../SLUICE_REVISION); engine logs the revision at init and the matrix tool cross-checks the load line |
| Toolchain | GCC 16.2.1 (C++20), xmake v3.1.2, liburing 2.13 (dynamic), kernel 7.3.3-200.fc44 | `docs/fio-integration/REPOSITORY-REALITY.md` §5 |

The engine ABI is pinned to fio 3.40 exactly (the loader rejects any other
`FIO_IOOPS_VERSION` — negative-tested in
[`FIO-ABI-QUALIFICATION.md`](../fio-integration/FIO-ABI-QUALIFICATION.md) §4.2). A host fio upgrade requires rebuilding against the matching tag and
re-qualifying.

## Four-engine support matrix

| Engine | Kind | Backends exercised | Formal-matrix status |
| --- | --- | --- | --- |
| `psync` | fio builtin (native sync) | POSIX pread/pwrite | ok (depth capped to 1 by fio semantics — recorded, not hidden) |
| `io_uring` | fio builtin | native io_uring | ok |
| `sluice-threadpool` | **this PR** (`fio-sluice.so`, `--sluice_backend=threadpool`) | Sluice ThreadPool via public API | ok |
| `sluice-uring` | **this PR** (`fio-sluice.so`, `--sluice_backend=uring`) | Sluice io_uring via public API | ok |

Supported fio operations: read, write, fsync, fdatasync. Trim /
sync_file_range / wait are rejected with EINVAL (fail-closed, negative-tested);
`verify_async` and `io_submit_mode=offload` are rejected at load/init (they
would queue from a non-owner thread). Engine options: `sluice_backend`
(`threadpool` default | `uring`), `sluice_workers` (TP workers, default 4).
Engine capacity is identical to the job's `iodepth`; `FIO_Q_BUSY` is never
returned; every accepted io_u is delivered exactly once through
`getevents`/`event`.

## Build and load

```sh
xmake f -m release -y
xmake build -y fio-sluice-ring        # also: fio-sluice-noring,
                                      # sluice-workload-bench[-ring],
                                      # fio-sluice-errno-map-test
xmake check / xmake check-ring        # explicit small functional checks

fio --name=demo \
    --ioengine=external:$PWD/build/linux/x86_64/release/fio-sluice-ring.so \
    --sluice_backend=uring --sluice_workers=4 \
    --rw=randread --bs=4k --iodepth=16 --direct=0 --size=1G \
    --time_based --runtime=10 --filename=<scratch regular file>
```

The Makefile is gone; `xmake.lua` is the only build system (per the explicit
repo requirement). Engine builds stage **PIC SDKs** under
`build/{noring,ring}-pic/` (a non-PIC static SDK cannot link into a shared
object — discovered and solved in
[`adapters/fio/README.md`](../adapters/fio/README.md) §3) and auto-verify +
configure the frozen fio tree under `build/fio/upstream`. The engine is a
GPLv2 derivative work of fio (headers included, structures linked); source
ships in this open repository.

## Correctness results

- **Deterministic correctness matrix** (`tools/check_fio_engine.py`; 35
  cases per backend-arm across read/write/pattern/mixed/mixed-size/sparse/
  EOF/fdatasync/negative-controls/SIGTERM-cleanup): **90 passed / 0 failed /
  0 skipped, exit 0**, and the same again with `--require-uring`
  (io_uring available on this host). Hidden ring engine + `--require-uring`
  → exit 77, never a threadpool fallback. See
  [`CORRECTNESS-MATRIX.md`](../fio-integration/CORRECTNESS-MATRIX.md).
- Two-layer data oracle: fio's `verify=crc32c` plus independent POSIX checks
  (sha256/size/pattern byte-compare, native-sync-engine parity oracle);
  verified non-vacuous by a deliberate 1-byte corruption probe.
- Engine smoke suite `adapters/fio/tests/smoke.sh`: PASS (19 jobs + 2
  rejections + errno-map unit test), both REQUIRE_URING branches exercised.
- Bench regression (`tools/check.py` via `xmake check` / `check-ring`): PASS,
  including the ring arm's byte oracle.
- Lifecycle discipline is enforced, not assumed: buffer borrows end only at
  publication (`take` in `getevents`), fds stay fio's, cleanup settles via
  `RequestScope::finish()`, health failures settle via owner-driven drain
  shutdown or fail-closed terminate — tripwire diagnostics are asserted
  absent by every matrix case.

## Initial performance matrix (E5 host, serial formal run, 2026-11-19)

A/A OBSERVED_TWO_REPEAT_SPREAD (two same-config repeats, psync and
sluice-threadpool only — no A/A pair exists for native io_uring or
sluice-uring): psync ΔIOPS **1.65%**, sluice-threadpool **8.35%**.
INSUFFICIENT_FOR_GENERAL_NOISE_THRESHOLD: these are two-repeat spreads, not
engine-family noise floors, not a minimum detectable effect, not 95%
intervals, and not general 5%/10% rules. Numbers below are means of 2
repeats (hot page cache,
direct=0, randseed=81, 10 s + 2 s ramp, `taskset 0-9`); full tables, per-rep
values and environment qualification in
[`PERF-BASELINE.md`](../fio-integration/PERF-BASELINE.md) (auto-generated
from the 50 fio JSON originals under `results/fio/`, cross-checked 50/50).

| Case | psync | io_uring | sluice-uring | sluice-threadpool |
| --- | ---: | ---: | ---: | ---: |
| F1 randread 4k D1 | 785.8k | 616.0k | 422.5k (0.69× native) | 39.2k |
| F2 randread 4k D16 | 789.5k (D1-capped) | 618.9k | 368.0k (0.59×) | 211.6k |
| F3 randrw 70/30 4k D16 | 4.6k | 5.9k | 98.1k | 27.9k |
| F4 seqread 64k D4 | 158.1k (D1-capped) | 126.9k | 117.3k (0.92×) | **211.0k (best, 13.8 GB/s)** |

- **F1/F2/F4 are EXPLORATORY_F1_F2_F4 exploratory baselines** (numbers are
  the archived record); rep spreads ≤ 11% and, where engine-to-engine deltas
  are far larger, interpreted per-rep rather than via a universal threshold.
- **F3 does not support cross-engine ranking on this substrate**
  (F3_ENVIRONMENT_CONFOUNDED: SATA SSD SLC-cache exhaustion dominates;
  declared before the formal run, reported per-rep rather than hidden
  behind a mean).
- Attribution (UNVERIFIED_EXPLORATORY_ATTRIBUTION — figures come from an
  earlier measurement session whose perf/strace receipts were never
  archived; no independent raw-counter recheck this round; not a basis for
  product performance verdicts): user-space instruction counts (native
  psync ~2.4k, native io_uring ~2.9k, sluice-uring ~7.9k, sluice-TP ~8.9k
  per IO) suggest ~5–6k extra user instructions/IO, but adapter + Sluice is
  a combined cost — the adapter's net overhead cannot be inferred
  separately; no blocking/double-traversal/copy syscall signature was
  observed (absence of extra syscalls does not rule out user-space
  overhead); ThreadPool's futex handshake is a Sluice-backend property, not
  adapter overhead (this side has archived rusage/ctx corroboration). See
  PERF-BASELINE.md §5.

## Known limitations (all stated in-repo, PHASE1-REPORT §I)

1. Unsupported: trim / sync_file_range / wait (EINVAL), `verify_async`,
   `io_submit_mode=offload`; `cancel` deliberately not implemented (borrowed-
   buffer safety; early-exit degrades to blocking drain).
2. Hard version pins: fio exactly 3.40 (ABI gate 37), liburing 2.13 dynamic,
   io_uring not disabled in the kernel. Cross-version loading untested.
3. Concurrency shape: one progress owner per job thread; multi-job /
   numjobs > 1 supported by construction (per-td contexts) but the formal
   matrix ran numjobs=1 only.
4. `direct=1` (O_DIRECT) untested in this phase; all baselines are buffered
   (direct=0) with page-cache-warm files.
5. F3 (write mix) environment-limited as above; governor not pinned to
   performance (no sudo); kernel-side PMU counts unavailable
   (perf_event_paranoid=2).
6. `results/` originals are git-ignored workspace evidence by design; the
   committed record is the cross-checked tables in PERF-BASELINE.md.

## No Sluice product code was modified

- The Sluice repository at `/home/jnhu/Source/Sluice` (HEAD `5d94f23` =
  `SLUICE_REVISION`) is treated read-only for the entire phase: `git status`
  shows zero tracked-file changes (only three pre-existing untracked evidence
  directories from earlier, unrelated work).
- The engine consumes **only the installed public SDK headers**
  (`sluice/async/{async_io_context,request_scope,threadpool_backend,
  uring_backend}.hpp`, `sluice/error.hpp`) and the static libraries; `grep`
  shows zero references to `detail/` headers or the Sluice source tree
  (`adapters/fio/src/`), and the SDK build aborts if the checkout has
  tracked edits (`xmake.lua:101-105`).
- The engine's undefined symbols are exactly five pinned fio internals
  (`generic_open_file`, `generic_close_file`, `generic_get_file_size`,
  `log_err`, `io_u_mark_submit`) plus C++/libc runtimes — no fio-internal API
  is turned into a Sluice product interface, no Sluice v2 public API changed,
  and no unified runtime/scheduler/polling thread was added (each fio job
  thread is the sole progress owner of its own context).
- No hidden fallback: a backend that cannot be constructed fails the job
  (noring engine + `--sluice_backend=uring` → explicit init error;
  negative-tested).

## Relation to #604 (research scope)

#604's **G0 workload admission** requires a *real consumer or defensible
representative trace* with frozen oracles/dependencies and budgets. This PR's
workloads are parameterized synthetic distributions (fio randread/randrw/
seqread with a fixed seed): no real consumer, no application dependency
structure, no application-level oracle or arrival process. **Therefore this
PR does not claim G0 admission, and its numbers are not G0 evidence.**

What it does provide for #604: a comparable measurement substrate — four
engines executing identical work under one harness, with adapter+Sluice
combined overhead indicated by unarchived-receipt exploratory attribution
(UNVERIFIED_EXPLORATORY_ATTRIBUTION, see above) — as groundwork for G0
admission preparation
and as a methods preview for G1 cost attribution. Proceeding to a real G0
admission (trace capture from a real consumer, frozen oracles) is the
recommended next step. #578 (fio + DuckDB R0 admission), #617 (kernel
boundary), and #603 (Direct Beyond-Parity) remain the owners of their
respective gates; this bench repository's engine does not bypass them.

Suggested review order: PHASE1-REPORT.md §J (ten questions + verdict
rationale) → `adapters/fio/README.md` → engine sources
(`adapters/fio/src/`, ~900 LOC) → tooling under `tools/`.

## Review repair round (r2)

Range: BASE `bea54ac8` → fixes `ed8ca4f` (close invariant) and
`3e995c5` (init containment), tests `3734974`, docs/tool revisions and
the repair ledger in the final commit of this PR. No merge performed —
Owner Merge Gate.

### Code fixes (accepted in r2, receipts archived)

1. **close_file fd lifetime (Plan A, fail closed).** A frozen-fio-3.40
   source audit established that close-with-in-flight is unreachable via
   legal fio sequences: `close_file` fires only from `put_file` when
   `f->references == 0` (filesetup.c:1898, 1912-1913), every io_u holds a
   file reference from `set_io_u_file` (io_u.c:1488) until reaped in
   `put_io_u` (io_u.c:855-856, after IO_U_F_FLIGHT clears at io_u.c:2103),
   and `td_io_close_file` defers the real close until the last io completes
   (ioengines.c:636-641). A live slot at close_file therefore means an
   engine bookkeeping bug; closing anyway could misdirect still-borrowed
   I/O to a reused fd number, and returning an error without closing
   violates `put_file`'s `f->fd == -1` post-condition (filesetup.c:1921).
   `sluice_close_file` now logs and `std::terminate()`s — the same
   fail-closed idiom already used by the slot-accounting path and
   `fail_closed()` (adapters/fio/src/files.cpp).
2. **init exception containment.** `sluice_init` previously had
   `make_unique<EngineState>` before its try block and the slot-vector
   setup after the catch handlers, so an allocation failure there escaped
   the C callback. All throwing initialization now sits in one try block;
   a `unique_ptr` owns the state until success (`state.release()` only on
   the success tail, which is unchanged), and failure logs and returns 1
   with full RAII rollback (scope-before-context teardown per the
   EngineState member order) (adapters/fio/src/engine.cpp).

### Deterministic tests (new `fio-sluice-lifecycle-test`)

The test links the real engine TUs against stub fio-core callbacks (the
five symbols fio_cxx.hpp declares) and compiles the engine with a
test-only `SLUICE_FIO_INIT_SEAM` hook (compiled out of both shipped .so
profiles — verified by `nm`).

- **T1 — close-with-in-flight (contract guard).** The audited fio 3.40
  refcount gate makes the violated state unreachable through real fio, so
  T1 invokes the callback synthetically: normal close, borrow on a
  different file, staged-completion non-blocking, and a forked violation
  child whose pipe EOF (the stub closer never ran) plus SIGABRT are the
  receipt.
- **T2 — init allocation failure.** A 4-variant matrix (steps 0/1/2 ×
  `std::bad_alloc`, plus a non-std exception type at step 1 to exercise
  `catch(...)`) asserts: failed init returns 1, `td->io_ops_data` stays
  null, and an in-process re-init on the same td then succeeds and cleans
  up — proving no leaked progress owner.
- **RED receipt (unfixed engine):** T1's violation child actually closed
  the borrowed fd (stub wrote 'X', child exited 42 → parent FAIL lines);
  T2 died with `terminate called after throwing an instance of
  'std::bad_alloc'` escaping the C callback; exit 134.
- **GREEN receipt (fixed engine):** `close_file invariant violated: fd 3
  still has in-flight request(s); failing closed` → SIGABRT with zero
  bytes on the pipe (closer never ran); all T2 variants contained
  (return 1, no state published, re-init ok); `lifecycle_test: PASS`,
  exit 0. Raw logs: `results/fio/lifecycle/{red,green}-t1t2.log`.

### Findings disposition

| Finding | Disposition |
| --- | --- |
| R-F1 root README make→xmake drift | RESOLVED before this round (commit `a9a9fd8`; verified at `bea54ac8`; not re-modified) |
| close_file in-flight borrow | RESOLVED (Plan A fail-closed + T1) |
| init exception escape | RESOLVED (single-try containment + T2) |
| doc drift ×2 (adapters README, ADAPTER-ARCHITECTURE status) | RESOLVED |
| PMU/instruction attribution receipts never archived | QUALIFIED — downgraded to UNVERIFIED_EXPLORATORY_ATTRIBUTION (numbers kept, caveats added) |
| A/A scope (psync/sluice-TP only, 2 repeats) | QUALIFIED — downgraded to OBSERVED_TWO_REPEAT_SPREAD / INSUFFICIENT_FOR_GENERAL_NOISE_THRESHOLD |

### r2 regression gates (all green)

All gates re-run at HEAD `bea54ac8` with the fixes in the working tree:
six `xmake build` targets = 0; `smoke.sh` = 0; `REQUIRE_URING=1 smoke.sh`
= 0; `check_fio_engine.py --require-uring` = 0 (90/90/0 →
`results/fio/correctness-review-r2.json`); `xmake check` = 0; `xmake
check-ring` = 0. Full ledger with binary hashes:
[`REVIEW-REPAIR-LEDGER.md`](REVIEW-REPAIR-LEDGER.md).

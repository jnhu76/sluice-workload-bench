# FIO external ioengine ABI qualification (Phase 1)

- Status: **QUALIFIED** — a shared object built against the frozen fio-3.40
  sources loads and runs under the system `/usr/sbin/fio` (fio-3.40,
  `FIO_IOOPS_VERSION 37`), verified by execution on 2026-11-19.
- Scope: version/ABI eligibility for a Sluice external fio engine. No product
  code, no Sluice modification, no performance measurement. All commands below
  were executed on this host during this audit; scratch artifacts lived under
  `build/scratch-abi/` (git-ignored) and were deleted afterwards.
- Upstream reference tree: `build/fio/upstream` (git-ignored, on-disk frozen
  copy). All `path:line` citations for fio internals are relative to that tree.

## 1. Environment audit (executed)

```
$ fio --version
fio-3.40
$ command -v fio
/usr/sbin/fio
$ ls -la /usr/sbin/fio
-rwxr-xr-x. 1 root root 1768584 Jan 16  2026 /usr/sbin/fio
$ rpm -qf /usr/sbin/fio
fio-3.40-3.fc44.x86_64
$ ldd /usr/sbin/fio
	linux-vdso.so.1 (0x00007f6efa134000)
	libnuma.so.1 => /lib64/libnuma.so.1 (0x00007f6efa101000)
	libz.so.1 => /lib64/libz.so.1 (0x00007f6efa0d7000)
	libm.so.6 => /lib64/libm.so.6 (0x00007f6ef9ce9000)
	libmvec.so.1 => /lib64/libmvec.so.1 (0x00007f6ef9bef000)
	libc.so.6 => /lib64/libc.so.6 (0x00007f6ef99f6000)
	/lib64/ld-linux-x86-64.so.2 (0x00007f6efa136000)
```

`/usr/include` contains no fio headers (`ls /usr/include/fio` → No such file
or directory). Consequence: the engine cannot build against installed
development headers; it must include the frozen upstream tree's headers
(`build/fio/upstream`), after running `./configure` there to generate
`config-host.h` (see §4.1). The fio binary exports 604 `T` symbols
(`nm -D /usr/sbin/fio | grep -c " T "`), including every fio-internal symbol
an external engine is expected to call (`generic_open_file`,
`generic_close_file`, `fio_gettime`, …) — required because an external engine
resolves fio core symbols from the host process at `dlopen` time.

## 2. Frozen upstream source

```
$ git clone --depth 1 --branch fio-3.40 https://github.com/axboe/fio.git build/fio/upstream
$ git -C build/fio/upstream describe --tags --exact-match   # → fio-3.40
$ git -C build/fio/upstream rev-parse HEAD
ff930c4653ae3952d6b09ab3ec89671aeabf2cbe
$ git -C build/fio/upstream log -1 --format='%H %ci %s'
ff930c4653ae3952d6b09ab3ec89671aeabf2cbe 2025-05-20 12:23:01 -0600 Fio 3.40
```

- Frozen tag: `fio-3.40`; frozen commit:
  `ff930c4653ae3952d6b09ab3ec89671aeabf2cbe` (clean tree, 21 MB).
- `build/` is git-ignored (`.gitignore:1`), so the tree is a local artifact,
  not repo content. No fio internal header is copied into the repository;
  implementers reference the frozen tree in place (recreate with the clone
  command above, then `./configure` inside it).

## 3. ABI contract, as defined by the frozen source

### 3.1 Version gate

`FIO_IOOPS_VERSION` is `37` (`ioengines.h:12`). Loading enforces strict
equality, not compatibility ranges (`ioengines.c:35-41`):

```c
if (ops->version != FIO_IOOPS_VERSION) {
	log_err("bad ioops version %d (want %d)\n", ops->version, FIO_IOOPS_VERSION);
```

Empirically re-verified in both directions in §4.1 (accept at 37) and §4.2
(reject at 36). Every callback of the ops structure is behind this gate, so the
whole `struct ioengine_ops` layout is only guaranteed within one
`FIO_IOOPS_VERSION` value.

### 3.2 `struct ioengine_ops` (ioengines.h:29-75)

Complete field list, in declaration order — an external engine fills a
non-static instance of exactly this shape:

| Field | Type / notes |
| --- | --- |
| `list` | `struct flist_head`; registration linkage, leave zeroed |
| `name` | `const char *`; engine name shown by fio |
| `version` | `int`; must be `FIO_IOOPS_VERSION` (37) |
| `flags` | `int`; `FIO_SYNCIO`, `FIO_DISKLESSIO`, … (ioengines.h:77-127) |
| `dlhandle` | `void *`; fio writes the dlopen handle here (ioengines.c:167) |
| `setup` | `int (*)(struct thread_data *)` |
| `init` | `int (*)(struct thread_data *)`; once per thread |
| `post_init` | `int (*)(struct thread_data *)`; after `init_io_u` (backend.c:1873) |
| `prep` | `int (*)(struct thread_data *, struct io_u *)`; before each submit |
| `queue` | `enum fio_q_status (*)(struct thread_data *, struct io_u *)`; **required** |
| `commit` | `int (*)(struct thread_data *)`; flush queued submissions |
| `getevents` | `int (*)(td, unsigned min, unsigned max, const struct timespec *)`; required for async |
| `event` | `struct io_u *(*)(struct thread_data *, int)`; required for async |
| `errdetails` | `char *(*)(struct thread_data *, struct io_u *)`; error text |
| `cancel` | `int (*)(struct thread_data *, struct io_u *)` |
| `cleanup` | `void (*)(struct thread_data *)`; pairs with `init` |
| `open_file` / `close_file` | `int (*)(td, struct fio_file *)`; fd ownership |
| `invalidate` / `unlink_file` / `get_file_size` / `prepopulate_file` | cache/file helpers |
| `terminate` | `void (*)(struct thread_data *)` |
| `iomem_alloc` / `iomem_free` | buffer allocation overrides |
| `io_u_init` / `io_u_free` | per-io_u engine scratch setup/teardown |
| `get_zoned_model`, `report_zones`, `reset_wp`, `move_zone_wp`, `get_max_open_zones`, `get_max_active_zones`, `finish_zone` | zoned-device hooks |
| `fdp_fetch_ruhs` | FDP/reclaim-unit hints (dataplacement.h) |
| `option_struct_size` | `int`; `sizeof` of the engine's private options struct |
| `options` | `struct fio_option *`; engine option table |

Mandatory-callback rules at load time (`check_engine_ops`, ioengines.c:35-70):
`queue` always; `event` and `getevents` additionally unless `FIO_SYNCIO` is
set. In practice `open_file` is also mandatory: `td_io_open_file` asserts it
(ioengines.c:528).

Return-value enum (`ioengines.h:23-27`): `FIO_Q_COMPLETED = 0`,
`FIO_Q_QUEUED = 1`, `FIO_Q_BUSY = 2`.

### 3.3 Loading an external engine — current conventions

Two conventions exist; the qualified one is the explicit path form:

1. **`ioengine=external:/abs/or/rel/path.so`** (documented in
   `HOWTO.rst:2197-2202`). The parser callback (`options.c:1853-1884`)
   `stat()`s the path and requires a regular file (`S_ISREG`), then stores it
   in `td->o.ioengine_so_path`. `load_ioengine` prefers that path
   (ioengines.c:196) and dlopens it directly with `RTLD_LAZY`
   (ioengines.c:131). This path works regardless of how the host fio was
   configured — it is what §4 exercises.
2. **Engine short name** (`ioengine=foo`): after the builtin registry lookup
   fails, fio tries `dlopen("foo")`, then — only if the host was configured
   with `--dynamic-libengines` (`configure:276,332`, `CONFIG_DYNAMIC_ENGINES`)
   — `<FIO_EXT_ENG_DIR>/fio-foo.so` where `FIO_EXT_ENG_DIR` is
   `/usr/local/lib/fio` on Linux (`os/os-linux.h:73`, ioengines.c:98-116).
   Whether the distro binary enables this was not probed; do not rely on it.

Symbol resolution after dlopen (ioengines.c:144-159), in order:

1. `dlsym(handle, "<engine string>")` — the engine *name/path string* as a
   symbol name (matches engines named after their exported symbol);
2. `dlsym(handle, "ioengine")` — a non-static `struct ioengine_ops ioengine`
   global (what skeleton_external.c exports; `engines/skeleton_external.c:211`);
3. `dlsym(handle, "get_ioengine")` — `void (*)(struct ioengine_ops **)`
   (`get_ioengine_t`, ioengines.h:132), explicitly provided for C++ engines
   that prefer to hand out an allocated ops structure (comment at
   ioengines.c:148-153).

External engines must **not** call `register_ioengine()` /
`unregister_ioengine()` — those are for engines compiled into fio
(`engines/skeleton_external.c:19-23`).

### 3.4 Per-thread lifecycle and call ordering

Verified ordering from `init.c`, `backend.c`:

1. Job parse → `load_ioengine` (init.c:1243): version + callback check, then
   if `.options` and `.option_struct_size` are set, fio `malloc`s `td->eo`,
   zeroes it, fills option defaults, and **overwrites the first pointer-sized
   field with `td`** (`*(struct thread_data **)td->eo = td;` init.c:1276) —
   hence the mandatory leading pad pointer in the engine options struct
   (§3.7, `engines/skeleton_external.c:31-34`).
2. `td_io_init` → `->init` (backend.c:1862). Async engines get the hint
   "Perhaps try reducing io depth?" on failure (ioengines.c:479-482).
3. `init_io_u`: for each of `iodepth` units fio allocates (`fio_memalign`,
   cache-line aligned), zeroes it, and calls `->io_u_init` if present
   (backend.c:1316-1351).
4. `->post_init` (backend.c:1873).
5. File setup/open by the core, then `td_io_open_file` → `->open_file`
   (ioengines.c:528-530). For the teardown direction: `td_io_close_file`
   marks closing and drops the refcount; `->close_file` runs when the last
   reference goes (ioengines.c:631-642).
6. Steady state loop: `->prep` (ioengines.c:271-279) → `->queue`
   (ioengines.c:388) → `->commit` (batched, ioengines.c:503-504) →
   `->getevents` → `->event` per completion (io_u.c:2224-2231).
7. Thread exit: `cleanup_io_u` drains the freelist calling `->io_u_free` per
   io_u (backend.c:1279-1285), then `close_ioengine` calls `->cleanup`
   followed by `dlclose` of the engine handle (ioengines.c:252-262) and frees
   `td->eo` (ioengines.c:238-242). Ordering matters: engine teardown code runs
   before the library is unloaded.

### 3.5 `queue()` return protocol — obligations of the engine

Core behavior around the callback (ioengines.c:327-470):

- Before the call, core asserts the io_u is not already in flight, sets
  `IO_U_F_FLIGHT` (ioengines.c:336-337), asserts the file is open
  (ioengines.c:355), and resets `io_u->error = 0; io_u->resid = 0`
  (ioengines.c:362-363).
- The engine must move `io_u->xfer_buflen` bytes to/from `io_u->xfer_buf`
  honoring `io_u->ddir`; a short transfer sets `io_u->resid`; failure sets
  `io_u->error` (contract text: `engines/skeleton_external.c:84-92`). It must
  not change the offset/order semantics fio chose (`io_u->file`, `io_u->offset`).
- `FIO_Q_COMPLETED`: the I/O finished before returning. Core does the
  completion accounting itself (ioengines.c:433-440). Nothing else may be
  reported later for this io_u.
- `FIO_Q_QUEUED`: accepted for asynchronous completion. Core increments
  `td->io_u_queued` and auto-calls `->commit` once the batch reaches
  `iodepth_batch` (ioengines.c:441-449). The completion **must** be delivered
  later through `getevents`/`event`, exactly once per io_u. (For engines
  without any `->commit`, core immediately marks submit+complete 1,
  ioengines.c:428-431 — such engines must not return `FIO_Q_QUEUED`.)
- `FIO_Q_BUSY`: nothing was accepted. Core rolls back the issue counters and
  clears `IO_U_F_FLIGHT` (ioengines.c:393-398); the engine must provide
  `->commit` so core can drain and retry (skeleton comment at
  `engines/skeleton_external.c:102-106`; core calls `commit` from the
  reap path when `min > 0`, ioengines.c:299-303).

### 3.6 `getevents()` / `event()` protocol and timeout semantics

Signature: `int getevents(td, unsigned int min, unsigned int max, const
struct timespec *t)` (ioengines.h:41). Contract (skeleton comments at
`engines/skeleton_external.c:52-72`, core use in ioengines.c:284-325):

- Returns the number of completions available since the last call, in
  `[0, max]`; a negative value is an error and is routed to `td_verror`
  (ioengines.c:320-321). On `r >= 0` core does `td->io_u_in_flight -= r` and
  marks `r` completions (ioengines.c:313-319).
- Core clamps `max` down to `td->cur_depth` and up to at least `min`
  (ioengines.c:304-307) before calling.
- Timeout argument: `t == NULL` means block until at least `min` completions
  are reaped (no timeout). A non-NULL `t` is an absolute-style timespec
  budget; the polling case passes a zeroed `{0, 0}` timespec and expects an
  immediate return with whatever is ready. This split is visible at the core
  call site: `io_u_queued_complete` selects `tvp = NULL` when `min_evts > 0`
  and `ts = {0,0}` when `min_evts == 0` (io_u.c:2275-2291). For reference,
  libaio copies `*t` and hands it to `io_pgetevents` (engines/libaio.c:228-239).
- After a return of `r > 0`, core calls `event(td, i)` for `i` in `[0, r)`
  (io_u.c:2224-2231). The engine must return each completed `io_u` exactly
  once per getevents batch, and must have set `io_u->error`/`io_u->resid`
  before returning from `getevents`.

### 3.7 Memory and ownership: `thread_data`, `io_u`, `fio_file`

- **`thread_data`** (fio.h:197): opaque-ish per-job context. Engine-scope
  state belongs in `td->io_ops_data` (fio.h:316), allocated in `init`, freed
  in `cleanup`. Engine options arrive pre-parsed in `td->eo` (fio.h:201).
- **`io_u`** (io_u.h:32): allocated, zeroed, and freed exclusively by fio
  core (backend.c:1322-1329, backend.c:1284). The engine never frees an
  io_u; after completion processing core reclaims it via `put_io_u`
  (io_u.c:843-870). Per-io_u engine scratch goes in `io_u->engine_data`
  (io_u.h:100), initialized in `->io_u_init`, released in `->io_u_free`.
  Fields the engine reads/fills: `file`, `ddir`, `offset`, `xfer_buf`,
  `xfer_buflen`, `resid`, `error`, `flags`.
- **`fio_file`** (file.h:79): the fd lifecycle belongs to the engine's
  `open_file`/`close_file` callbacks; `close_file` must `close(f->fd)` and
  set `f->fd = -1` (filesetup.c:654-674). The default implementation
  `generic_open_file`/`generic_close_file` (filesetup.c:717, filesetup.c:654)
  handles `O_DIRECT`, `O_CREAT` for write jobs, file hashing — skeleton
  simply forwards to them (`engines/skeleton_external.c:143-154`).
  `get_file_size` is optional; core falls through when absent
  (ioengines.c:659-665).

### 3.8 Engine options registration

- Option table: `struct fio_option` array (`parse.h:47-86`) terminated by
  `{ .name = NULL }`; types from `FIO_OPT_STR` … `FIO_OPT_STR_SET`
  (parse.h:10-28). Offsets (`off1`…) index into the engine's private options
  struct; `option_struct_size` gives its size. Example:
  `engines/skeleton_external.c:31-49`.
- fio core allocates and owns `td->eo` (init.c:1247-1277), zeroes it, fills
  defaults, and stores `td` in the **first pointer field** (init.c:1276) —
  the private struct must begin with a pad pointer so `off1` of a real option
  is never 0 (`engines/skeleton_external.c:27-34`).
- Options are engine-scoped on the command line (`--dummy=1` parsed by fio
  core into `td->eo` — verified live in §4.3). Recommended metadata:
  `.category = FIO_OPT_C_ENGINE` (`engines/skeleton_external.c:43`).
- `td->eo` is freed by fio after `cleanup` (ioengines.c:238-242); the engine
  must not free it, and any string options are duplicated by core
  (`options_mem_dupe`, init.c:1271).

### 3.9 Build requirements and licensing

- Shared object: `-shared -fPIC`; upstream's reference build line is in the
  skeleton header comment (`engines/skeleton_external.c:4-9`):
  `gcc -Wall -O2 -g -D_GNU_SOURCE -include ../config-host.h -shared -rdynamic -fPIC`
  (`CONFIG_STRSEP` comes from the generated `config-host.h`; the skeleton's
  extra `-DCONFIG_STRSEP` is redundant and only warns — observed in §4.1).
- The engine needs the full header closure (`fio.h`, `optgroup.h`), not just
  `ioengines.h`: `ioengines.h` includes `io_u.h`/`flist.h`/`dataplacement.h`
  and the ops callbacks traffic in `thread_data`/`fio_file`.
- `config-host.h` is generated by `./configure` in the frozen tree; it must
  come from the same version as the headers.
- Unresolved fio-core symbols (e.g. `generic_open_file`) are resolved at
  `dlopen` time from the host fio process (`RTLD_LAZY`, ioengines.c:131);
  verified present in `/usr/sbin/fio` (§1).
- License: fio is **GPLv2** (`COPYING`). An external engine that includes
  fio's headers and links its structures is a derivative work — the Sluice
  engine source must be distributable under GPLv2-compatible terms. (fio also
  ships `MORAL-LICENSE` for special exceptions; not applicable here.)

## 4. Empirical receipts

All receipts executed 2026-11-19 on this host. Scratch directory
`build/scratch-abi/` (git-ignored), removed at the end of the audit.

### 4.1 Build the skeleton external engine and run it under system fio

```
$ cd build/fio/upstream && ./configure            # generates config-host.h (exit 0)
$ cd build/fio/upstream
$ gcc -Wall -O2 -g -D_GNU_SOURCE -DCONFIG_STRSEP -include config-host.h -I. \
      -shared -rdynamic -fPIC -o ../scratch-abi/skeleton_external.so \
      engines/skeleton_external.c
config-host.h:34:9: warning: 'CONFIG_STRSEP' redefined   # harmless: config-host.h already defines it
gcc exit: 0
$ nm -D build/scratch-abi/skeleton_external.so | grep -E "ioengine$"
0000000000003020 D ioengine
```

Run (tiny job; 4 × 4 KiB writes to a scratch regular file):

```
$ /usr/sbin/fio --name=abi-smoke \
    --ioengine=external:/home/jnhu/Source/sluice-workload-bench/build/scratch-abi/skeleton_external.so \
    --rw=write --bs=4k --size=16k --filename=build/scratch-abi/abi-smoke.bin
abi-smoke: (g=0): rw=write, bs=(R) 4096B-4096B, (W) 4096B-4096B, (T) 4096B-4096B, ioengine=engine_name, iodepth=1
fio-3.40
Starting 1 process
abi-smoke: Laying out IO file (1 file / 0MiB)
abi-smoke: (groupid=0, jobs=1): err= 0: pid=66544: Thu Nov 19 00:26:35 2026
  write: IOPS=4000, BW=15.6MiB/s (16.4MB/s)(16.0KiB/1msec); 0 zone resets
...
     issued rwts: total=0,4,0,0 short=0,0,0,0 dropped=0,0,0,0
Run status group 0 (all jobs):
  WRITE: bw=15.6MiB/s (16.4MB/s), ... io=16.0KiB (16.4kB), run=1-1msec
fio exit: 0
```

Interpretation: the distro fio-3.40 accepted the engine built from the frozen
tag — `ioengine=engine_name` (the skeleton's `.name`) appears in the header,
`queue()` returning `FIO_Q_COMPLETED` was exercised 4 times (`total=0,4`),
`err= 0`, exit 0. (The skeleton performs no real I/O; this proves the ABI
channel, not throughput. `skeleton_external.c` was used verbatim, unmodified.)

### 4.2 Version gate negative test

Variant with `.version = FIO_IOOPS_VERSION - 1` (sed copy in scratch):

```
$ /usr/sbin/fio --name=badver \
    --ioengine=external:.../build/scratch-abi/skeleton_badver.so \
    --rw=write --bs=4k --size=16k --filename=build/scratch-abi/abi-smoke.bin
bad ioops version 36 (want 37)
fio: failed to load engine
fio exit: 1
```

The system binary demands exactly version 37, matching `ioengines.h:12` of the
frozen tag — the strongest available evidence that the distro binary's ABI
matches the frozen source.

### 4.3 Engine option parsing

```
$ /usr/sbin/fio --name=opt-test --ioengine=external:.../skeleton_external.so \
    --rw=write --bs=4k --size=4k --dummy=1 --filename=build/scratch-abi/abi-smoke.bin
opt-test: (groupid=0, jobs=1): err= 0 ...
fio exit: 0
```

`--dummy` (declared via `.options` + `option_struct_size`) is parsed by fio
core into `td->eo`. The options channel works against the distro binary.

### 4.4 C++20 header-safety matrix (`g++ -std=c++20 -x c++ -fsyntax-only -I build/fio/upstream -include build/fio/upstream/config-host.h`)

| Translation unit | Result |
| --- | --- |
| `#include "os/os.h"` | **pass** (exit 0) |
| `#include "ioengines.h"` | **pass** (exit 0) |
| `#include "fio.h"` + `#include "optgroup.h"` (bare) | **pass** (exit 0) |
| same two, wrapped in `extern "C" { ... }` | **fail**: `error: template with C linkage` — `arch.h` pulls C++ `<atomic>` under `__cplusplus` (arch.h:3-8), and C++ standard headers cannot live inside `extern "C"` |
| `<string>` included **after** `fio.h` | **fail**: `minmax.h`'s `min`/`max` function-like macros (minmax.h:4-18) collide with `std::min(a, b, comp)` declarations in libstdc++ (`stl_algobase.h:280: macro 'min' passed 3 arguments`) |

Conclusion: fio's headers are C++-aware by design (`arch.h:3-8` switches to
`std::atomic` under `__cplusplus`), but they are **macro-polluting**: every
C++ standard header must be included *before* `fio.h`, and `extern "C"`
wrapping is actively wrong.

### 4.5 C++20 engine probe — pure C++ plugin loaded by system fio

`build/scratch-abi/t_cpp_engine.cpp` (deleted afterwards; shape preserved
here): includes `<string>` first, then bare `#include "fio.h"` /
`"optgroup.h"`; `init` does `td->io_ops_data = new cppabi_state{"..."}`,
`cleanup` does `delete`; `queue` returns `FIO_Q_COMPLETED`; `open_file`/
`close_file` forward to `generic_open_file`/`generic_close_file`; exports
`struct ioengine_ops ioengine` with `.flags = FIO_SYNCIO`,
`.version = FIO_IOOPS_VERSION`.

```
$ g++ -std=c++20 -Wall -O2 -g -shared -fPIC -I build/fio/upstream \
      -include build/fio/upstream/config-host.h \
      -o build/scratch-abi/cppabi_probe.so build/scratch-abi/t_cpp_engine.cpp
build exit: 0
$ nm -D build/scratch-abi/cppabi_probe.so | grep -E " ioengine$"
0000000000003040 D ioengine          # global variables are not mangled → dlsym finds it

$ /usr/sbin/fio --name=cpp-probe \
    --ioengine=external:.../build/scratch-abi/cppabi_probe.so \
    --rw=write --bs=4k --size=16k --filename=build/scratch-abi/abi-smoke.bin
cpp-probe: (g=0): rw=write, ..., ioengine=cppabi_probe, iodepth=1
cpp-probe: (groupid=0, jobs=1): err= 0: pid=66820: Thu Nov 19 00:28:27 2026
  write: IOPS=4000, BW=15.6MiB/s (16.4MB/s)(16.0KiB/1msec); 0 zone resets
fio exit: 0
```

A C++20 TU that includes `fio.h` bare, uses `new`/`delete`/`std::string`
inside callbacks, and exports `ioengine` as a plain global loads and runs
under the distro fio with `err=0` — C++ runtime startup under `dlopen` works,
and the C++-linkage static callbacks assigned into `ioengine_ops` are called
correctly.

## 5. C/C++ boundary decision

**Decision: pure C++ plugin — no C shim.** One engine library compiled as
C++20 with g++, including fio's headers directly.

Basis (all from this audit):

1. Bare `fio.h` compiles cleanly as C++20 (§4.4); fio upstream maintains a
   `__cplusplus` branch in `arch.h` and explicitly anticipates C++ engines
   (ioengines.c:148-153). A C shim would duplicate the ops-table wiring for
   zero proven safety gain — the only C++ hazards found are include-order and
   exception discipline, both local to the C++ TU.
2. Rules to enforce in the engine sources (cheap, mechanical):
   - A single wrapper header (e.g. `src/fio-engine/fio_cxx.hpp`) includes
     the fixed set of C++ std headers first, then `"fio.h"`/`"optgroup.h"`,
     and is the only place fio headers are included. Nothing else includes
     std headers after it (§4.4 collision receipt).
   - No `extern "C"` around fio headers (breaks, §4.4). The dlsym'd entry
     point needs no mangling workaround anyway: the `ioengine` *variable* is
     a plain global and stays unmangled (§4.5 `nm -D` receipt). If the
     follow-up prefers `get_ioengine`, declare that one function
     `extern "C"` (functions are mangled; variables are not).
   - Exceptions must never escape a callback: fio's threads are C; an escaped
     exception means `std::terminate`. Callback bodies that call throwing
     Sluice code get `try/catch` translating to `io_u->error = errno`-style
     negative returns. (Rule; runtime escape path not exercised in Phase 1.)
   - Link with `g++ -shared -fPIC` so `libstdc++` is a dependency of the .so;
     fio-core symbols stay undefined and resolve from the host process
     (verified pattern in §4.5).
3. Alternative rejected: thin C shim (`engine.c` exporting `ioengine`,
   forwarding to a C++ core behind a C API). It adds a translation layer to
   maintain, a second compilation language, and an internal C API that must
   mirror fio's anyway — without removing any of the real risks (which live
   in the C++ TU regardless).

## 6. Compatibility scope and caveats

Qualified scope: fio **3.40** (distro `fio-3.40-3.fc44.x86_64`) × external
engines built from the frozen tag `fio-3.40`
(`ff930c4653ae3952d6b09ab3ec89671aeabf2cbe`), `FIO_IOOPS_VERSION 37`, loaded
via `ioengine=external:<path>`.

Caveats:

1. **Version pinning is hard.** The loader rejects any other
   `FIO_IOOPS_VERSION` (§4.2), and upstream moves it fast — 3.33 → 31,
   3.36 → 33, 3.40 → 37 (checked from the respective tags' `ioengines.h`).
   Between 3.36 and 3.40 alone: `errdetails` changed signature (gained the
   `td` argument), `move_zone_wp` was added, and the flags enum grew
   (`FIO_MULTI_RANGE_TRIM`, `FIO_ATOMICWRITES` absent in 3.36's header,
   present in 3.40's ioengines.h:100-101 and 125-126). Any fio upgrade on the host
   requires rebuilding and re-qualifying the engine against the matching tag.
   Guard in tooling: assert `fio --version` == the version the engine was
   built for before running.
2. **Host symbol exports are observed, not contractual.** The engine's
   undefined references (e.g. `generic_open_file`) resolve because this
   distro binary exports them (§1). Keep the set of called fio-internal
   symbols minimal and pinned (`generic_open_file`, `generic_close_file`,
   `fio_ro_check` macro-only) — a differently-linked fio could stop
   exporting others.
3. **Engine-name dlopen convention is configuration-dependent** (needs
   `CONFIG_DYNAMIC_ENGINES` + `/usr/local/lib/fio/fio-<name>.so`,
   ioengines.c:98-116). Not probed on the distro binary; the qualified
   invocation is always the explicit `external:` path. Job files in this
   repo must use `ioengine=external:<abs path>`.
4. **`td->eo` first field is reserved for `td`** (init.c:1276). Engine
   options struct must start with a pad pointer; also the reason `off1`
   must never be 0 (skeleton:27-34).
5. **Async engines must deliver every queued completion exactly once**
   through `getevents`/`event` (§3.5/§3.6). Returning `FIO_Q_QUEUED` from a
   commit-less engine corrupts core accounting (ioengines.c:428-431).
   `FIO_Q_BUSY` obliges a `->commit` implementation.
6. **Not verified in Phase 1:** cross-version loading (only 3.40 host × 3.40
   engine tested); `ioengine=` short-name dlopen on the distro binary;
   offload (`io_submit_mode=offload`) interaction with a custom engine;
   behavior under `--output-format=json` job matrices. None of these are
   needed for qualification but they should be exercised when the real
   Sluice engine lands.
7. **No system fio development package exists** (`/usr/include` has no fio
   headers). Builds depend on the frozen tree at `build/fio/upstream` plus a
   `./configure` run inside it (generates `config-host.h`). Both are
   git-ignored; recreate with the clone in §2. Do not vendor fio headers
   into the repository.
8. **GPLv2** applies to the engine (§3.9). Keep the engine source in this
   (open) repository; distribution of a binary engine implies source
   availability under GPLv2.

## 7. Frozen-artifact policy

- The repository carries **no** fio-internal headers or sources. The frozen
  upstream tree lives only at `build/fio/upstream` (git-ignored), pinned by
  tag `fio-3.40` / commit `ff930c4653ae3952d6b09ab3ec89671aeabf2cbe`.
- Implementers build against that tree directly (`-I build/fio/upstream
  -include build/fio/upstream/config-host.h`) and must not copy its headers
  elsewhere.
- Scratch evidence files (`build/scratch-abi/*`: both `.so`, test `.cpp`
  TUs, modified skeleton copy, downloaded `ioengines_3_33.h` /
  `ioengines_3_36.h`, and the fio job's target file) were deleted after this
  audit; the receipts above embed the exact commands to regenerate them.

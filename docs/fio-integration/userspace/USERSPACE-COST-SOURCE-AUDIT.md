# Sluice × fio 用户态成本：源码审计合并报告

**合并对象**：call-graph、ownership、lock-graph、machine-work 四份源码审计。

**文档定位**：只汇总四份审计实际给出的事实；四份审计之间互相矛盾或口径不同的断言，合并时回源码/反汇编核实，并在 §2 标注。本文不新增性能测量，不把 CPU 采样份额换算成锁等待时间。

**证据前缀**（全文统一）：

- `S:` = `/home/jnhu/Source/Sluice/`
- `B:` = `/home/jnhu/Source/sluice-workload-bench/`
- `F:` = `B:build/fio/upstream/`
- `D:` = `B:build/linux/x86_64/release/fio-sluice-ring-sym.so` 内的 VMA

---

## 0. 结论摘要

1. **提交侧是一条确定性的单链**：每个被接受的 I/O 恰好 1 次 `sluice_queue` → `RequestScope::submit_*` → `AsyncIoContext::submit_*` → 虚 `backend_->submit_*`；io_uring 臂每 op 1 个 SQE、1 次 `io_uring_submit`，提交不跨 `io_u` 批量（`D:0x13bab`；`S:src/async/uring_transport.cpp:87,231`）。
2. **完成侧不是回调**：CQE 只在共享 CQ ring 中，由 owner 的 `sluice_getevents` → `poll_progress` → `UringAsyncBackend::poll` → `reap_cqes` → `handle_one_cqe` 主动 peek；同一 poll pass 内 `publish_one` 发布，随后 `harvest` → `take` 才取走（`S:src/async/uring_completion.cpp:200-229`；`B:adapters/fio/src/events.cpp:202-246`）。
3. **关键内联已发生**：`RequestCore::consume_public_result` 在 release 中完全内联进 `RequestBridge::consume`，独立符号 `0x15560` 零调用点；`RequestScope` 的 `resolve_` / `reserve_slot_` / `release_slot_` 也全部内联（§3.4）。
4. **查找与对表是 O(C) 线性扫描**：`RequestScope::take` 内的 `resolve_` 从 index 0 全表扫描并逐项比对 `RequestId`，**从不使用 ticket 自带的 `id().slot_` 作为下标**；`find_live_router_cookie_` 对每个 CQE 从尾部倒扫 router（`S:include/sluice/async/request_scope.hpp:118-128`；`S:src/async/uring_transport.cpp:551-557`）。
5. **core 锁计数不是平坦的 10 次**：对非零长度、正常完成的 accepted I/O，固定 9 次生命周期锁（提交 3 + 完成 6），加上 `harvest` 期间每次 `take` 触发的一次 `consume_public_result` core 锁。B=1 时每次 getevents 只完成约 1 个 I/O，但 `take` 次数 T 可以是 `K + k`（K=在飞占用槽数，k=首个 ready 槽下标），满占用时 ≤2C；因此合计是 **9 + T**，“10 次/I/O”只在 T=1 时成立（§2 C-01）。
6. **buffer/fd 是纯 borrow**：Sluice 不复制、不 pin、不 dup/close 调用方的 fd；publication 准入显式要求 `execution_refs == 0`，所以主路径上“借用触碰引用退休”严格早于 publication（`S:src/async/detail/request_core.cpp:393-409,447`）。
7. **锁图只有 5 把 `std::mutex`**：`RequestCore::mutex_`、`UringAsyncBackend::dispatch_mtx_`、`ThreadPoolBackend::work_mtx_`、`AsyncIoContext::access_mtx_`、`ProgressSource::mtx_`；不存在 `ThreadPoolBackend::queue_mtx_`。观测到的锁序是 `access_mtx_` → {`dispatch_mtx_`, `work_mtx_`} → `RequestCore::mutex_`，以及 `dispatch_mtx_` → `ProgressSource::mtx_`、`access_mtx_` → `ProgressSource::mtx_`（`S:include/sluice/async/detail/request_core.hpp:295`；`S:include/sluice/async/detail/uring_backend.hpp:320`；`S:include/sluice/async/detail/threadpool_backend.hpp:247`；`S:include/sluice/async/async_io_context.hpp:336`；`S:include/sluice/async/detail/progress_source.hpp:491`；§5）。
8. **shutdown 结算到 published，不结算到 consumed**：`settlement_converged_` 要求 `outstanding == 0` 等五项，但不要求 `public_bindings == 0`；已发布未消费的 Request 仍可查询/消费，析构再以 `public_bindings == 0` fail-fast 兜底（`S:src/async/async_io_context.cpp:469-477,81-85`）。
9. **cancellation 不冒充物理完成**：core 只在 `!execution_claimed` 时选零效果取消；kernel cancel CQE 只释放 control、不选终态；字节 op 的 kernel-cancel 结果是 `uncertain` 而非 success；threadpool 把 running cancel 报成 `physical_interruption_unsupported`（`S:src/async/detail/request_core.cpp:260-275`；`S:src/async/uring_completion.cpp:99-143`；`S:src/async/threadpool_backend.cpp:797-803`；§4.4）。
10. **机器工作量**：稳态每 I/O 0 次堆分配；提交路径有 1 条 64 位 `div`（fd→FileAccess 的 unordered_map 桶取模，`B:adapters/fio/src/queue.cpp:56`；`D:0x3fc3`）；正常 read/write 提交路径可确定 2 条 `lock` 前缀 RMW（`live_cookies_++`、`submit_flushes_++`），完成路径再加 `live_cookies_--`；threadpool 每完成额外 1 次 `write(eventfd)` 与 1 次 futex 唤醒/线程切换，uring 正常完成无用户态写（§6）。

---

## 1. 审计基线与方法

### 1.1 基线

| 项 | 值 | 证据 |
|---|---|---|
| Sluice 产品代码 | `5d94f233967c3aef993eb19ba9e3252c532b0951`，工作树干净（仅 3 个未跟踪 evidence 目录） | `git -C /home/jnhu/Source/Sluice rev-parse HEAD`；`git -C /home/jnhu/Source/Sluice status --porcelain` |
| bench / adapter 代码 | `61baa1f2273f2469736c5f29cbc4868bbeac9f40` | `git rev-parse HEAD` |
| fio 冻结树 | `build/fio/upstream`，tag `fio-3.40`，HEAD `ff930c4653ae3952d6b09ab3ec89671aeabf2cbe` | `git -C build/fio/upstream describe --tags --exact-match`；`git -C build/fio/upstream rev-parse HEAD` |
| release `.so` | `fio-sluice-ring-sym.so`，BuildID `738d6167a7f494122c78d9e1dab2801941e5d442`，`.text` = `0x158a1`，GCC 16.2.1 | `readelf -n`；`readelf -S -W`；`readelf -p .comment` |
| `.so` 与随包 stripped 版的关系 | 两者 `.text` 逐字节相同，sha256 均为 `3d3b9ec449f5c982e243f360cfaf75a747b1226884a06a2b736f5db692c49ad6` | `objcopy --dump-section .text=...` + `sha256sum` + `cmp`（输出 `IDENTICAL`） |
| 链接内容 | 受审 7 个 TU 全在 `libsluice_async.a` 内：`async_io_context`、`request_scope`、`threadpool_backend`、`uring_backend`、`uring_completion`、`uring_transport`、`request_core` | `ar t B:build/ring-pic/sdk/lib/libsluice_async.a` |
| capacity | `capacity == td->o.iodepth`，全链路同一值 | `B:adapters/fio/src/engine.cpp:88,112,127`；`F:ioengines.c:304-307` |

> 说明：`fio-sluice-ring.so` 与 `fio-sluice-ring-sym.so` 的整文件 sha256 不同（前者 stripped），但当前 `.text` 段相同；符号化/内联结论对实际 release `.text` 成立。本文不声称两个整文件相同。

### 1.2 方法

- Sluice 源码用 `git` + 行号阅读；adapter 与 fio 冻结树同样按源码行阅读。
- 反汇编用 `objdump -d -C --no-show-raw-insn`；调用点、内联、去虚化以“符号是否 emit + 是否存在 `call`”判定。
- **Sluice 库 TU 没有行号表**：`readelf --debug-dump=info` 只有 5 个 compile unit，均为 adapter 源码（如 `B:adapters/fio/src/state.hpp`、`B:adapters/fio/src/engine.cpp`）。因此 Sluice 内部指令不能逐条映射到源码行；inline 结论不依赖 DWARF line。
- 除 §8 明确列出的既有 release 测试重跑外，本文没有新的运行时测量；lock-graph 审计给出的 7.8/6.1/18.8/3.7/73.4/345.3 ns 只是 `std::mutex` / `std::atomic` 原语校准，**不是产品锁等待时间**。

---

## 2. 冲突核实记录

> 本节只列四份审计之间口径不同或表面矛盾的点。未列出的内容按原审计事实合并。

| ID | 冲突/差异 | 涉及审计 | 核实结论 | 证据 |
|---|---|---|---|---|
| C-01 | core 锁是“固定 9 + T”还是“每 I/O 10 次” | call-graph vs lock-graph/machine-work | **9 + T 正确**；“10”是 T=1 的特例。T 是本次 getevents 内 `take` 的次数，不是完成数 | `S:include/sluice/async/request_scope.hpp:118-142`；`S:include/sluice/async/request.hpp:146-152`；`S:src/async/detail/request_core.cpp:323-325`；`B:adapters/fio/src/events.cpp:92-109`；`D:0x93ea`、`D:0x15df6` |
| C-02 | `ThreadPoolBackend::queue_mtx_` 是否存在 | lock-graph 澄清 vs 请求中的提法 | **不存在**。线程池的 `dispatch_` 与 `publication_pending_` 都由 `work_mtx_` 单锁保护；io_uring 的两个队列由 `dispatch_mtx_` 保护 | `S:include/sluice/async/detail/threadpool_backend.hpp:247-251`；`S:include/sluice/async/detail/uring_backend.hpp:320-322`；`grep -rn queue_mtx` 无源码命中 |
| C-03 | 测试是否运行过 | ownership vs lock-graph | **既有 release 二进制可以运行且本次重跑全部 EXIT=0**；但本轮没有重新构建。ownership 的 `xmake build` 失败是构建树被 `/home/jnhu/sluice-e5-elim` shadow 的环境问题，不等于二进制不可用 | §8 的命令与输出；`git diff --name-only 27d907d..HEAD -- src include tests` 为空；二进制时间戳 2026-11-12 21:42 |
| C-04 | `iodepth_batch_complete_max` 默认值是否已核实 | machine-work（未核实） vs call-graph（=1） | **默认 B=1**：min 的 `.def="1"`；max 无 `.def`；`init.c` 在 min>max 时令 max=min。仓库 workload 不覆盖 batch | `F:options.c:2299-2311`；`F:options.c:2314-2324`；`F:init.c:770-771`；对 `tools/ adapters/ workloads/` 搜索 `iodepth_batch` 与 `batch_complete` 无结果 |
| C-05 | `dispatch_mtx_` 提交侧是 3 次还是 2 次 | call-graph vs lock-graph | **每 accepted I/O 2 次**（delivery 复位 + dispatch_after_accept）；call-graph 的第 3 次是 poll 入口，属于每 poll pass，不是每 I/O | `S:src/async/uring_backend.cpp:489`；`S:src/async/uring_transport.cpp:49`；`S:src/async/uring_backend.cpp:621` |
| C-06 | 全 `.so` mutex 引用数 156/200 vs 155/199 | machine-work vs 本次反汇编统计 | 本次对 `pthread_mutex_lock@plt` 与 `pthread_mutex_unlock@plt` 的 call/jmp 引用分别计数得 **155 / 199**，与 machine-work 的 156/200 各差 1；单函数 core 方法仍为 1L/1U。差异不影响任何路径结论 | `D` 全反汇编；§8 |
| C-07 | `sluice_event` 是否“无调用” | call-graph | **正常路径无调用**（`addq $1,0xf8(%rax)` 后返回）；错误路径仍 `call fail_closed`。不是严格意义的全函数叶子 | `D:0x3850-0x3890`（正常）；`D:0x389f`（错误） |
| C-08 | `take` 典型值 `≈2C` | call-graph vs machine-work 的 `K` | `T ≈ K + k`，`K` 为占用槽数；只有满占用 `K=C` 时才接近 `2C`。K<C 时低于 2C | `B:adapters/fio/src/events.cpp:92-109`；`S:include/sluice/async/request_scope.hpp:118-128` |
| C-09 | 提交路径“3 次 lock 前缀原子” | machine-work | 普通 read/write 提交路径可确定 2 条：`live_cookies_++`（`D:0x12c98`）与 `submit_flushes_++`（`D:0x13b9b`，内联的 `submit_transport_locked`）。第 3 条 `live_cookies_--` 在完成路径 `retire_router_entry_`（`D:0x11f70`）；control/cancel 另有 `live_control_sqes_` 计数 | `D:0x12c98`、`D:0x13b9b`、`D:0x11f70`；`S:include/sluice/async/detail/uring_backend.hpp:324-326` |
| C-10 | `.so` 整文件哈希与 `.text` 哈希 | call-graph E1 | 审计引用的是 `.text` 段哈希，正确；整文件哈希因 stripped/debug 元数据不同而不同，不能拿整文件哈希否定 `.text` 一致性 | §1.1；`sha256sum` 与 `objcopy` 命令 |
| C-11 | 提交阶段 mutex 合计是 5 还是 6 | machine-work | **6**：access 1 + core 3（reserve/accept/claim_execution）+ dispatch 2（delivery 复位/dispatch_after_accept）；machine-work 的 5 漏了 `claim_execution` 的 core 锁 | `S:src/async/async_io_context.cpp:255`；`S:src/async/uring_backend.cpp:447,489,514`；`S:src/async/uring_transport.cpp:49,152`；`D:0x12ba9` |

### 2.1 C-01 核实：为什么是 9 + T

源码路径是确定的：

1. `harvest` 每轮从 index 0 扫描 `slots`；对每个非空槽调用一次 `take`，pending 时只 `break` 出 switch，槽仍在，下一轮会再次 `take`（`B:adapters/fio/src/events.cpp:92-109`）。
2. `RequestScope::take_` 先 `resolve_` 线性扫描，命中后调用 `take_result()`；pending 也返回 observation，不释放槽（`S:include/sluice/async/request_scope.hpp:118-142`）。
3. `Request::take_result()` 无条件调用 `RequestBridge::consume`（`S:include/sluice/async/request.hpp:146-152`）；`RequestBridge::consume` 调用 `core->consume_public_result`（`S:src/async/detail/request_core.cpp:735-740`）。
4. `consume_public_result` 第一行就是 `std::lock_guard<std::mutex> lock(mutex_)`，然后才判断 `published`；pending 也要付锁（`S:src/async/detail/request_core.cpp:323-331`）。
5. 反汇编确认 `take` 在命中槽后 `call 15dd0`，`consume` 内 `pthread_mutex_lock@plt` 位于 `0x15df6`（`D:0x93ea`、`D:0x15df6`）。

因此：

- 提交侧固定 3 次 core 锁：`reserve`、`accept`、`claim_execution`。
- 完成侧固定 6 次 core 锁：`offer_terminal`、`release_execution`、`begin_publication`、`complete_publication`、`claim_observer_delivery`、`retire_observer_delivery`。
- 每次 `take` 再加 1 次 `consume_public_result` 锁；每次 getevents 有 T 次 `take`。
- **每 accepted I/O 的 core 锁 = 9 + T。**

B=1 时若第一个非空槽就是 ready，T=1，合计 10；这正是另外两份审计的“10 次/I/O”口径。若本次 getevents 先空扫 K 个 pending 槽、poll 后再扫到下标 k 的 ready 槽，T≈K+k；满占用 K=C 时 T≤2C，合计 9+2C。call-graph 的“C=16 时约 32 次 core mutex 仅来自 harvest，再加固定 9 次”即 9+2C=41。

### 2.2 C-03 核实：测试运行状态

- ownership 审计执行 `xmake build -y request_scope_test` 得到 `error: cannot change directory /home/jnhu/sluice-e5-elim, not found this directory`，因此该轮没有构建/测试通过。其观察“二进制 2026-11-12 21:42，HEAD 提交 2026-11-13 00:06”本身为真，但 `src/include/tests` 自 `27d907d`（2026-11-12 21:04）后无 diff；HEAD 之前的后续提交只涉及 docs/CHANGELOG/xmake 测试清单。
- lock-graph 审计运行了既有 release 二进制；本次合并按 §8 的命令重跑 10 个二进制，全部 `EXIT=0`。
- 结论：**这些二进制是行为 oracle，可用于验证当前源码对应的行为；它们不是本次重新构建的产物，也不是无数据竞争/无锁等待的证明**。`request_scope_test` 输出中的 `terminate called without an active exception` 是预期的 fail-fast 路径，最终仍为 `25 of 25`、EXIT=0。

### 2.3 C-04 核实：默认 batch

- `iodepth_batch_complete_min` 的 `.def = "1"`（`F:options.c:2309`）。
- `iodepth_batch_complete_max` 没有 `.def` 字段（`F:options.c:2314-2324`）。
- `init.c` 在 `min > max` 时执行 `max = min`（`F:init.c:770-771`），所以默认 max=1。
- `td_io_getevents` 再把 max 夹到 `td->cur_depth`（`F:ioengines.c:304-305`）。
- 仓库 `tools/ adapters/ workloads/` 中没有 `iodepth_batch` / `batch_complete` 覆盖。
- 因此默认 **B=min=max=1**；如果运行器在命令行覆盖，B 需按覆盖值重算。

---

## 3. Call Graph

### 3.1 提交侧：fio `io_u` → sluice_queue → … → kernel

| # | 边（调用者 → 被调用者） | 源码调用点 | 次数/被接受 I/O | release 反汇编证据 | inline 判定 |
|---|---|---|---|---|---|
| S1 | fio `do_io`/`io_u_submit` → `td_io_queue` → `td->io_ops->queue`（= `sluice_queue`） | `F:backend.c:731,604`；`F:ioengines.c:388` | 1 | fio 侧未反汇编；经 ops 函数指针 | 否（C 函数指针） |
| S2 | `sluice_queue` → `RequestScope::submit_read/write/sync_data/sync_all` | `B:adapters/fio/src/queue.cpp:90-98,110-111` | 1（READ/WRITE 走 `submit_read/write`；SYNC/DATASYNC 走 `submit_sync_*`） | `D:0x41d8 call 8af0`；`D:0x4257 call 8cf0`；`D:0x4097 call 8ef0`；`D:0x4320 call 90f0` | 否（独立符号，直接 call） |
| S3 | `RequestScope::submit_*` → `AsyncIoContext::submit_*` | `S:src/async/request_scope.cpp:152,178,202,226` | 1 | `D:0x8b89 call 4c00`（read 实例） | 否 |
| S4 | `AsyncIoContext::submit_*` → `backend_->submit_*` | `S:src/async/async_io_context.cpp:259`（锁在 254-256） | 1 | `D:0x4c6e call *0x50(%rax)`（vtable 间接） | 否（虚调用；`backend_` 为 `unique_ptr<AsyncBackend>`，无去虚化） |
| S5 | `UringAsyncBackend::submit_read` → 模板 `submit_request<ReadOp>` | `S:src/async/uring_backend.cpp:532-533`（模板 423-530） | 1 | 无 `submit_request` 符号；`reserve()` 调用出现在 `submit_read` 体内 `D:0xefd6` | 是（模板已内联） |
| S6 | `submit_request` → `RequestCore::reserve()` | `S:src/async/uring_backend.cpp:447` | 1 | `D:0xefd6 call 147b0` | 否 |
| S7 | `submit_request` → `RequestCore::accept()` | `S:src/async/uring_backend.cpp:514` | 1 | `D:0xf18c call 14890` | 否 |
| S8 | `submit_request` → `dispatch_after_accept` | `S:src/async/uring_backend.cpp:527` | 1（非 zero-len） | `D:0xf26e call 13a20` | 否 |
| S9 | `dispatch_after_accept` → `dispatch_one_locked` | `S:src/async/uring_transport.cpp:83` | 1（稳态；积压时每项 1 次） | `D:0x13ab4 call 12ad0` | 否；`BoundedDispatchQueue::push_back` 被内联（`0x13d2c` 仅异常路径副本） |
| S10 | `dispatch_one_locked` → `RequestCore::claim_execution` | `S:src/async/uring_transport.cpp:152` | 1 | `D:0x12ba9 call 14dd0` | 否 |
| S11 | `dispatch_one_locked` → `io_uring_get_sqe` / `io_uring_prep_read` / `io_uring_prep_write` / `io_uring_sqe_set_data64` | `S:src/async/uring_transport.cpp:141,168,172,189` | 各 1 | 函数体内无这些 call；`nm -D` 无任何 `io_uring_prep_*` 导入 | 是（liburing 头内 `static inline`） |
| S12 | `dispatch_after_accept` → `submit_transport_locked` → `io_uring_submit` | `S:src/async/uring_transport.cpp:87,231` | 1（+SQ 满/重试次数 R，UNKNOWN） | `D:0x13bab call io_uring_submit@plt` 直接位于 `dispatch_after_accept` 内（`submit_transport_locked` 被内联）；独立副本 `D:0x12aac` | 是（内联进 `dispatch_after_accept`） |
| S13 | ThreadPool `submit_request` → `reserve/accept/dispatch_after_accept` | `S:src/async/threadpool_backend.cpp:228,281,294` | 1/1/1 | `D:0xae41 call 147b0`；`D:0xaffa call 14890`；`D:0xb0e3 call aae0` | 模板内联；三个被调者否 |
| S14 | ThreadPool `dispatch_after_accept` → `work_cv_.notify_one` | `S:src/async/threadpool_backend.cpp:330` | 1 | 解锁后尾跳 `D:0xab66 jmp notify_one@plt` | 尾调用（非 call） |
| S15 | `sluice_commit` → `io_u_mark_submit`（fio） | `B:adapters/fio/src/queue.cpp:29` | 1 次/`getevents(min>0)` | `D:0x3f29 call io_u_mark_submit@plt`（仅 `pending_submits != 0`） | fio 符号，不内联 |

### 3.2 完成侧：kernel CQE → getevents → harvest → take → consume → fio event

| # | 边（调用者 → 被调用者） | 源码调用点 | 次数/完成（默认 B=1） | release 反汇编证据 | inline 判定 |
|---|---|---|---|---|---|
| C1 | kernel → CQ ring（CQE） | `S:src/async/uring_completion.cpp:210`（`io_uring_peek_batch_cqe`） | 1 CQE | 非调用；共享内存轮询（`D:0x115dc`/`D:0x116e8 call io_uring_peek_batch_cqe@plt`） | — |
| C2 | fio `wait_for_completions` → `io_u_queued_complete` → `td_io_getevents` → `ops->getevents` | `F:backend.c:428-436`；`F:io_u.c:2291`；`F:ioengines.c:311` | 1（默认 min=max=1；末尾 drain 可批量） | fio 侧未反汇编 | 否（函数指针） |
| C3 | `sluice_getevents` → `harvest` | `B:adapters/fio/src/events.cpp:203`（健康路径 153） | h 次；典型 2（poll 前后各 1），精确值 UNKNOWN | `D:0x35cf`/`D:0x36d5`/`D:0x3480 call 2df0` | 否 |
| C4 | `harvest` → `RequestScope::take` | `B:adapters/fio/src/events.cpp:100,114` | T 次；T ≤ h·C，典型 ≈ K + k（K=在飞占用槽数，k=首个 ready 槽下标） | `D:0x2e65 call 92f0`；`D:0x2f7e call 9540` | 否 |
| C5 | `take` → `RequestBridge::consume` | `S:include/sluice/async/request_scope.hpp:138` + `S:include/sluice/async/request.hpp:151`（`take_result` 内联） | T（含 pending：pending 判定在 consume 返回之后） | `D:0x93ea call 15dd0`（data）/`D:0x962a call 15dd0`（void） | `take_result` 内联；bridge 否 |
| C6 | `consume` → `pthread_mutex_lock` + `RequestCore::consume_public_result`（内联）+ `try_reclaim_` | `S:src/async/detail/request_core.cpp:323-344,592-597` | 每 T 一次 | `D:0x15df6 pthread_mutex_lock@plt`；`D:0x15e95 call 152b0`；`consume_public_result` 独立符号 `0x15560` **零调用点** | 是（`consume_public_result` 完全内联进 bridge） |
| C7 | `sluice_getevents` → `poll_progress` | `B:adapters/fio/src/events.cpp:218` | ≈1（≥1；空 poll 会进 wait） | `D:0x3719`/`D:0x375e call 62d0` | 否 |
| C8 | `poll_progress` → `run_progress_pass_` → `backend_->poll_progress`（虚） | `S:src/async/async_io_context.cpp:432,446` | 1/次 pass | `D:0x632e call 5750`；`run_progress_pass_` 内 `D:0x5784` 比较 base 版、`D:0x5860 call *%rdx` 走 override | 未去虚化（speculative devirt：仅 base 版走内联快路径） |
| C9 | `UringAsyncBackend::poll` → `reap_cqes` | `S:src/async/uring_backend.cpp:636` | 1/pass | `D:0x10650 call 11580` | 否 |
| C10 | `reap_cqes` → `handle_one_cqe` | `S:src/async/uring_completion.cpp:222` | 1/CQE | `D:0x1163f`/`D:0x1174f call 11170` | 否 |
| C11 | `handle_one_cqe` → `finalize_operation_terminal_` | `S:src/async/uring_completion.cpp:197` | 1 | `D:0x113df call 10d60` | 否 |
| C12 | `finalize_operation_terminal_` → `offer_terminal` / `release_execution` / `retire_router_entry_` | `S:src/async/uring_completion.cpp:70,89,94-96` | 1/1/1 | `D:0x10e84 call 14ce0`；`D:0x10ec9 call 15660`；`D:0x10fce call 11ef0` | 否 |
| C13 | `poll` → `publish_one` | `S:src/async/uring_backend.cpp:646-657` | 1/完成（同一次 poll 先 reap 再 publish） | `D:0x106da call 104b0` | 否 |
| C14 | `publish_one` → `begin_publication` / `complete_publication` / `claim_observer_delivery` / `retire_observer_delivery` | `S:src/async/uring_backend.cpp:584,598,611-613` | 1/1/1/1 | `D:0x104f1 call 14f80`；`D:0x1052c call 157b0`；`D:0x10562 call 15160`；`D:0x105bf call 15900` | 否；`deliver_event` 被内联进 `publish_one`（独立副本 `0x10b40` 零调用点） |
| C15 | `deliver_event` → `sink.on_ready` | `S:src/async/uring_backend.cpp:612` | 0（本构建） | `D:0x10b93` 将 vtable slot 与 `ReferenceReadySink::on_ready`（`0xd650`）比较，相等则跳过间接 call | 去虚化 + 空实现消除 |
| C16 | `sluice_getevents` → `wait_one` | `B:adapters/fio/src/events.cpp:232-235` | 正常路径 0..1/次 getevents（CQE 已在本次 poll 前就绪则 0）；`control_interrupted` 确认后可再次 park | `D:0x369a`/`D:0x3800 call 6fc0`；`D:0x37c3 call 65a0` | 否 |
| C17 | `wait_one` → `run_progress_pass_` + `ProgressSource::wait_if_unchanged` → `poll(2)` | `S:src/async/async_io_context.cpp:611,655,663`；`S:include/sluice/async/detail/progress_source.hpp:203` | 1/park | `D:0x677a`/`D:0x6a56 call 5750`；`D:0x6dd1`/`D:0x6e5f call poll@plt` | `wait_if_unchanged` 内联进 `wait_one`（另有 `.part.0` 副本） |
| C18 | fio `ios_completed` → `sluice_event`（返回后 fio 再调 `io_completed`） | `B:adapters/fio/src/events.cpp:254-266`；`F:io_u.c:2225-2227` | 1/交付的完成 | `sluice_event` `0x3850` 正常路径仅 `addq $1,0xf8(%rax)`（`D:0x3880`）后返回，无调用；错误路径 `D:0x389f call fail_closed` | 正常路径叶/全内联；错误路径非叶 |
| C19 | ThreadPool `worker_loop` → `run_syscall` → `pread/pwrite/fdatasync/fsync/fstat` | `S:src/async/threadpool_backend.cpp:432,487,501,516,522,530` | 1/op | `D:0xcb09 call c530`；`D:0xc67f pread@plt`；`D:0xc63f pwrite@plt`；`D:0xc613 fdatasync@plt`；`D:0xc5a3 fsync@plt`；`D:0xc739 fstat@plt` | 否（`run_syscall` 独立；glibc 符号） |
| C20 | ThreadPool `worker_loop` → `offer_terminal` / `release_execution` / `signal_ready_progress` | `S:src/async/threadpool_backend.cpp:454,457,466` | 1/1/1 | `D:0xcc2a call 14ce0`；`D:0xcc57 call 15660`；`D:0xcd38 write@plt`（signal 内联） | signal 全内联（`ProgressSource::signal` 无符号） |
| C21 | ThreadPool `poll_progress` → `publish_one` / `begin/complete_publication` / `claim/retire observer` | `S:src/async/threadpool_backend.cpp:589,358,370,377,383` | 1/pass-per-completion | `D:0xc20d call bed0`；`D:0xbf11 call 14f80`；`D:0xbf4c call 157b0`；`D:0xbf82 call 15160`；`D:0xbfdc call 15900` | 否；`deliver_event` 同样被内联（`0xc490` 零调用点） |

### 3.3 每 I/O 调用次数（公式；C = capacity = iodepth，B = `iodepth_batch_complete_max`）

| 计数器 | 公式 | 本仓库默认（B=1，C=iodepth） | 依据 |
|---|---|---|---|
| `sluice_queue` | 1 | 1 | `B:adapters/fio/src/queue.cpp:35`；`F:ioengines.c:388` |
| scope submit | 1 | 1 | `B:adapters/fio/src/queue.cpp:90-111` |
| ctx submit | 1 | 1 | `S:src/async/async_io_context.cpp:249-269` |
| backend submit | 1 | 1 | 虚调用 `D:0x4c6e` |
| core `reserve` / `accept` / `claim_execution` | 1 / 1 / 1 | 3 | `S:src/async/uring_backend.cpp:447,514`；`S:src/async/uring_transport.cpp:152` |
| `io_uring_prep_*` | 1 | 1 | `S:src/async/uring_transport.cpp:166-189` |
| `io_uring_submit` | 1 + R（R=SQ 满/重试次数，UNKNOWN） | 1（queue_depth==capacity，稳态无重试） | `S:src/async/uring_transport.cpp:218-235` |
| kernel 物理 I/O | 1 | 1 | prep_read/write |
| CQE | 1 | 1 | `S:src/async/uring_completion.cpp:210` |
| `handle_one_cqe` / `finalize_operation_terminal_` | 1 / 1 | 1 / 1 | `S:src/async/uring_completion.cpp:222,197` |
| `offer_terminal` / `release_execution` | 1 / 1 | 1 / 1 | `S:src/async/uring_completion.cpp:70,89` |
| `publish_one` / begin / complete publication | 1 / 1 / 1 | 1 / 1 / 1 | `S:src/async/uring_backend.cpp:646-657,584,598` |
| `claim_observer_delivery` / `retire_observer_delivery` | 1 / 1 | 1 / 1 | `S:src/async/uring_backend.cpp:611-613` |
| `poll_progress`（每次完成） | ≥1；典型 1（空 pass 则再加 1 次 wait 内 pass） | 1（UNKNOWN 精确值） | `B:adapters/fio/src/events.cpp:218`；`S:src/async/async_io_context.cpp:611` |
| backend `poll` / `reap_cqes` | 1/pass | 1 | `S:src/async/uring_backend.cpp:636` |
| `sluice_getevents`（全程） | `ceil(N/B)` + O(1) 尾部 drain | ≈ N（B=1；末尾一次 drain 可批量，故略小于 N） | `F:io_u.c:2284-2291`；`F:options.c:2299-2325`；`F:init.c:768-771` |
| `harvest`（每次 getevents） | h；h = 1 + 产生完成的 pass 数，典型 2 | 2（1 次空扫 + 1 次命中） | `B:adapters/fio/src/events.cpp:202-203` |
| `RequestScope::take`（每次完成） | T ≤ h·C；B=1 时典型 ≈ K + k，满占用 ≈ 2C | ≈K+k（满占用 ≈2C；C=16 时 ≈32） | `B:adapters/fio/src/events.cpp:92-93`；`S:include/sluice/async/request_scope.hpp:118` |
| `RequestBridge::consume`（每次完成） | T（pending 也 consume；仅 1 次成功） | ≈K+k（满占用 ≈2C） | 表 3.2 C5/C6 |
| `sluice_event` / fio `io_completed` | 1 / 1 | 1 / 1 | `F:io_u.c:2225-2227` |
| `sluice_commit` / `io_u_mark_submit` | 1 / 1 每 getevents(min>0)（仅 `pending_submits != 0`） | 1 / 1 | `F:ioengines.c:299-303`；`B:adapters/fio/src/queue.cpp:22-33` |
| core `context()` | 3（dispatch_one_locked、finalize、publish_one） | 3 | `D:0x12b7f`、`D:0x10daf`、`D:0x104c5` |
| `find_live_router_cookie_` | 1/CQE（O(C) 反向扫） | 1 | `S:src/async/uring_transport.cpp:551-557` |
| `try_reclaim_` / `reclaimable_` | 正常路径 4 / 4（release_execution、complete_publication、retire_observer_delivery、consume_public_result 内各 1；均在同一 core 锁内） | 4 / 4 | `S:src/async/detail/request_core.cpp:341,407,482,570,592` |
| core `release_slot_` 真正回收 | 1（consume 时 binding_live 已清、observer 已退休、refs 为 0） | 1 | `S:src/async/detail/request_core.cpp:334-343,580-597` |

> `try_reclaim_` 的审计表写“3/3”，按源码正常路径有 4 个调用点（上表）；审计的 3 很可能只数了 `release_execution`、`complete_publication`、`retire_observer_delivery` 三处，未把 `consume_public_result` 内的那一处计入。两者不改变“所有调用都在同一 core 锁内”的结论。

### 3.4 inline / 模板 / 去虚化判定（nm + objdump 调用点计数）

| 函数 | 观测 | 结论 |
|---|---|---|
| `RequestScope::reserve_slot_` (`0x8320`) | 调用点 0 | 全内联（`submit_*` 内 O(C) 扫描） |
| `RequestScope::release_slot_` (`0x8370`) | 调用点 0 | 全内联（`take_` ready 分支、settle） |
| `RequestScope::resolve_` / `take_` / `commit_` | 无符号 | 模板全内联进 `take` / `submit_*`（`0x9320` 扫描、`0x8b56` 后 commit 字段写入） |
| `RequestCore::consume_public_result` (`0x15560`) | 调用点 0 | 完全内联进 `RequestBridge::consume` |
| `RequestCore::reclaimable_` (`0x15260`) | 调用点 0 | 内联进 `try_reclaim_` |
| `RequestCore::release_slot_` (`0x159a0`) | 调用点 1（仅 `rollback` `D:0x15bd8`） | 内联进 `try_reclaim_` |
| `RequestCore::lookup` (`0x14ae0`) | 调用点 1 | 内联进 `RequestBridge::ready`；仅 `AsyncIoContext::lookup` 保留独立调用 |
| `UringAsyncBackend::deliver_event` (`0x10b40`) | 调用点 0 | 内联进 `publish_one` 与 `poll` owed sweep |
| `ThreadPoolBackend::deliver_event` (`0xc490`) | 调用点 0 | 内联进 `publish_one` 与 `poll_progress` owed sweep |
| `ProgressSource::signal` | 无符号、无调用 | 全内联（`worker_loop` 内 `write@plt` `D:0xcd38`） |
| `ReferenceReadySink::on_ready` | `D:0x10b93` 指针比较后跳过 call | 去虚化 + 空实现消除 |
| `submit_request<Op>` 模板 | 无符号 | 内联进各 `submit_read/write/sync_*/file_info/size` |
| liburing `*_prep_*` / `*_get_sqe` / `sqe_set_data64` | 无导入、无 call | liburing 头内 `static inline` |

### 3.5 内核 / 系统调用（每 I/O）

| 阶段 | io_uring 臂 | ThreadPool 臂 | 证据 |
|---|---|---|---|
| 提交 | 1 次 `io_uring_submit` → `io_uring_enter`（每 accepted op 1 次，**不跨 io_u 批量**） | 0（但提交侧 1 次 `notify_one` futex 尾跳） | `D:0x13bab`；`D:0xab66` |
| 物理执行 | kernel / io-wq（是否落到 io-wq 工作线程由内核与文件/flag 决定，**UNKNOWN**） | worker 线程内 1 次 `pread`/`pwrite`（EINTR 重试另计） | `D:0xc67f`/`D:0xc63f` |
| 完成通知 | kernel 在 CQE 时向注册的 eventfd 写入（`io_uring_register_eventfd`，init 时 1 次）；无用户态 write | 1 次 `write(eventfd)`（`ProgressSource::signal`，可被 `notification_pending_` 合并，≤1） | `S:src/async/uring_backend.cpp:259-283`；`D:0xcd38` |
| 收割 | `io_uring_peek_batch_cqe` 读共享 CQ，**0 syscall** | `poll_progress` 纯用户态，0 syscall | `D:0x115dc` |
| 空闲等待 | `poll(2)`（`wait_if_unchanged`），每 park 1 次；次数 UNKNOWN（取决于 CQE 是否已就绪） | 同左 | `D:0x6dd1`/`D:0x6e5f` |
| 溢出 | `io_uring_get_events` 仅在 CQ overflow 时（罕见） | — | `S:src/async/uring_completion.cpp:266` |

fio 调用形态：`io_u_queued_complete(td,1)` ⇒ `min>0` ⇒ `tvp = NULL`（无超时，故适配器 `remaining()` 不调用 `Clock::now()`）；max = `iodepth_batch_complete_max`，再被 `td_io_getevents` 夹到 `td->cur_depth`；`min>0` 时每次 getevents 前还会调一次 `sluice_commit`（`F:io_u.c:2275-2291`；`F:ioengines.c:299-311`）。

### 3.6 契约 requirement ID ↔ 调用边

| Requirement | 约束的边 | 代码锚点 |
|---|---|---|
| PROG-01 | getevents 在 `completed/immediate_work_remains/dispatch_retry_remains` 时不得 park；poll pass 有界、residue 如实上报 | `B:adapters/fio/src/events.cpp:222-227`；`S:src/async/uring_backend.cpp:304-328`；`S:src/async/threadpool_backend.cpp:550-612` |
| PROG-02 | `wait_one` 的 token→drain→probe→poll 握手，禁止丢失唤醒 | `S:src/async/async_io_context.cpp:636-664`；`S:include/sluice/async/detail/progress_source.hpp:108-232` |
| PROG-04 | wait 结果区分 progress/interrupted/deadline/health；deadline 不取消 I/O | `S:src/async/async_io_context.cpp:608-695`；`B:adapters/fio/src/events.cpp:212-246` |
| THREAD-01 | poll/prepare/wait 仅 owner；submit 可跨线程；本适配器单 job 线程即 owner | `S:src/async/request_scope.cpp:15-26`；`S:src/async/async_io_context.cpp:210-224` |
| REQ-04 / REQ-05 | 只有 publication 后 take 才取得结果并释放借用；reclaim 需 binding/execution/control/observer 全清 | `S:src/async/detail/request_core.cpp:323-344,580-597`；`S:include/sluice/async/request_scope.hpp:131-143` |
| LIFE-01 / LIFE-02 | fd 只借不关；buffer 借到 publication | `B:adapters/fio/src/files.cpp:10-47`；`B:adapters/fio/src/queue.cpp:62`；`S:src/async/detail/request_core.cpp:277-294` |
| SHUT-01 / SHUT-03 / SHUT-04 | 仅 owner 驱动 shutdown；析构前置条件；结算后才释放 borrow | `B:adapters/fio/src/events.cpp:138-158`；`S:src/async/async_io_context.cpp:858-882`；`B:adapters/fio/src/state.hpp:39-42` |
| CANCEL-01 / CANCEL-02 | 适配器不实现 cancel（ops 表无 cancel），阻塞 drain | `B:adapters/fio/src/engine.cpp:184-199`；`B:docs/fio-integration/ADAPTER-ARCHITECTURE.md:415-426` |
| BACKEND-01 | 后端 seam 为内部接口，不新增第三后端 | `S:include/sluice/async/detail/async_backend.hpp:17-24`；`B:adapters/fio/src/state.hpp:4-7` |
| ERR-01 / INV-02 | 拒绝路径无副作用；errno 无损映射 | `B:adapters/fio/src/queue.cpp:76-86,122-132`；`B:adapters/fio/src/errno_map.cpp` |
| HANDLE-02 | 非终态 Request 析构 fail-fast；take 成功才清空 handle | `S:include/sluice/async/request.hpp:124,146-159,177-185`；`S:src/async/detail/request_core.cpp:22-28` |

### 3.7 初始化 / 收尾 / 错误路径（非 per-I/O）

| 路径 | 调用链 | 次数 | 证据 |
|---|---|---|---|
| init | `sluice_init` → `make_threadpool_context`/`make_uring_context` → `AsyncIoContext` ctor → `attach_progress_port`（`io_uring_register_eventfd`）→ `RequestScope` ctor（`claim_progress_owner`） | 1/job | `B:adapters/fio/src/engine.cpp:107-128`；`D:0x2111`/`D:0x247f`/`D:0x215c` |
| open_file | `sluice_open_file` → `generic_open_file` + `fcntl(F_GETFL)` | 1/file | `B:adapters/fio/src/files.cpp:10-47` |
| cleanup | `sluice_cleanup` → `scope->finish()` → `settle_and_release_`（每 owning 槽 `RequestBridge::ready` + 必要时 `wait_one`）→ `~RequestScope` → `~AsyncIoContext`（`drive_settlement_` → backend stop/retire） | 1/job | `B:adapters/fio/src/engine.cpp:155-177`；`D:0x1f3c`；`S:src/async/request_scope.cpp:82-135`；`S:src/async/async_io_context.cpp:94,479-563` |
| health failure | `sluice_getevents` → `settle_health_failure` → `context->shutdown(drain)` → `drive_settlement_` → `harvest` | 仅失败 job | `B:adapters/fio/src/events.cpp:138-158`；`S:src/async/async_io_context.cpp:858-882` |
| queue 拒绝 | `sluice_queue` → `map_errno` + `log_err` → `FIO_Q_COMPLETED`（无 Sluice 请求） | 仅拒绝 io_u | `B:adapters/fio/src/queue.cpp:76-86` |
| 短 I/O | fio `io_completed` 自行 requeue 残余 → 额外 1 次 `sluice_queue` | 仅短读/短写 | `F:io_u.c:2141-2154` |

---

## 4. Ownership Graph

### 4.1 所有权实体

| 实体 | 所有者 | 所有权性质 | 生命周期锚点 | 证据 |
|---|---|---|---|---|
| `Request<T>` 句柄 | 调用方；adapter 中由 `RequestScope` 的槽持有 | 独占、move-only；只持“公共绑定”(`binding_live`)，不持槽存储 | `submit_*` 返回 → `take`/`discard`/移动；非终态销毁或覆盖 = always-on fail-fast | `S:include/sluice/async/request.hpp:101-124,146-185`；`S:src/async/detail/request_core.cpp:754-771` |
| RequestCore 槽表 | `AsyncIoContext`（`core_` 在 `progress_`/`backend_` 之前声明，故最后析构） | 独占、定长、generation 复用；槽是“责任/存储”而不是 I/O 本身 | 上下文存活期；按 REQ-05 谓词回收 | `S:include/sluice/async/async_io_context.hpp:276-291`；`S:src/async/detail/request_core.cpp:580-597,599-631` |
| buffer（`io_u->xfer_buf`） | 调用方 / fio `io_u` | borrow：读目标独占、写源只读共享；Sluice 不复制、不 pin、不取引用 | initiation → acquired publication；只有 kernel（uring SQE）或 worker（pread/pwrite）解引用 | `S:docs/explicit-io-contract.md:953-972`；`S:src/async/detail/request_core.cpp:200`；`S:src/async/uring_transport.cpp:161-187`；`S:src/async/threadpool_backend.cpp:481-502` |
| fd（`io_u->file->fd`） | fio 的 `fio_file` | borrow（`NativeFileRef` 按值复制 fd+access）；Sluice 无 dup/close，只按值传递 | fio 的 `f->references` 归零才真正 `close` | `S:include/sluice/async/async_io_context.hpp:30-38`；`B:adapters/fio/src/files.cpp:21-46`；`F:filesetup.c:1897-1898` |
| result（`IoOutcome`/`FileInfo`/`EffectReport`） | 终态前由槽独占；consume 时按值拷出 | 槽内独占 → 出槽归调用方；`take` 后句柄被清空 | 终态选择 → consume；槽回收时清零 | `S:src/async/detail/request_core.cpp:323-344,438-484,615-620`；`S:include/sluice/async/request.hpp:146-159,187-209` |
| `ScopeTicket<T>` | 调用方（可复制） | 纯查找 token（内含 `RequestId`），零所有权，也不延长 scope 寿命 | 仅当 scope 槽仍 `owning` 且 id 匹配 | `S:include/sluice/async/request_scope.hpp:24-38,114-129` |
| `ProgressOwner` | `RequestScope` 成员（或显式 `claim_progress_owner()` 者） | 独占驱动权、move-only RAII；不参与 refcount | claim → 句柄析构/移动赋值 | `S:include/sluice/async/async_io_context.hpp:97-111`；`S:src/async/async_io_context.cpp:54-75,210-224` |
| `owner_thread_`（驱动线程绑定） | `AsyncIoContext`，但可被“第一次 drive”隐式设置 | 隐式共享/可转移，外部不可查询 | 到 `release_progress_owner_` / 上下文 move | `S:src/async/async_io_context.cpp:179-202,869-873,113-126` |
| `io_u*`（adapter 槽 / `staged`） | fio；adapter 只借指针 | borrow | `IO_U_F_FLIGHT` 期间，至 `event()` 归还 | `B:adapters/fio/src/state.hpp:27-34`；`B:adapters/fio/src/events.cpp:79-86,102-108,265`；`F:io_u.c:2102-2103` |
| observer 注册 | 持有 Request 句柄者（每请求至多 1 个） | 独占注册 + 一个槽 pin；adapter 未使用 | attach → delivery/cancel 退休 | `S:src/async/detail/request_core.cpp:486-578`；`S:include/sluice/async/async_io_context.hpp:259-271` |

### 4.2 阶段与所有权变化（queue → submit → 完成 → take → event → 回收）

| # | 阶段 | 代码 | 所有权 / 引用变化 | 失败/越界行为 |
|---|---|---|---|---|
| 1 | fio 组 `io_u` 并 `queue()` | `B:adapters/fio/src/queue.cpp:35-62` | buffer/fd/io_u 仍全部属 fio；engine 只拿到 `NativeFileRef{fd, access}` | 未注册 fd → `EBADF` 完成；非法 ddir → `EINVAL` 完成（INV-02 无副作用） |
| 2 | adapter 选自己的槽、写下 `io_u*` 与 ticket | `B:adapters/fio/src/queue.cpp:64-74,99-121` | adapter 槽 free→占用；`io_u*` 是 borrow | 槽记账破坏 → `std::terminate`（`queue.cpp:64-71`） |
| 3 | scope 预留 + `Reservation` guard | `S:src/async/request_scope.cpp:142-153`；`S:include/sluice/async/request_scope.hpp:74-87` | scope 槽 free→reserved；guard 拥有“未提交即归还”的责任 | submit 失败 → guard 归还 scope 槽；异常 → adapter 归还自己的槽 |
| 4 | `ctx.submit_*` → backend 校验 → `core_->reserve()` | `S:src/async/uring_backend.cpp:430-457`；`S:src/async/threadpool_backend.cpp:218-239` | 槽 reserved（**尚非** accepted；REQ-02 明确 pending reservation 不算接受） | 校验失败/容量/admission-closed → 无副作用拒绝 |
| 5 | prepare：把 fd/buffer/len 复制进 backend 数组 | `S:src/async/uring_backend.cpp:479-486`；`S:src/async/threadpool_backend.cpp:257-259` | 产生**借用记录**（不是所有权）；此时尚无 kernel 使用 | 注入失败 → `rollback` 归还 reserved 槽 |
| 6 | `core_->accept()` | `S:src/async/detail/request_core.cpp:184-210` | 槽 accepted、`binding_live=true`、`borrow` 入槽（`:200`）、`execution_refs=1`（`:201`）、`outstanding++`；zero-op 直接 `terminal_chosen` 且 refs=0 | accept 失败 → `rollback`；`~RequestCore` 见 reserved/accepted 残留即 fail-fast（`request_core.cpp:54-59`） |
| 7 | 返回 `Request<T>`（绑定）→ scope `commit_` move 进槽 → 返回 ticket | `S:src/async/async_io_context.cpp:264-268`；`S:include/sluice/async/request_scope.hpp:99-109` | Request 责任 **move** 给 scope 槽；ticket 只是 token | `commit_` 为 noexcept move（static_assert） |
| 8 | dispatch / claim | `S:src/async/uring_transport.cpp:130-159`；`S:src/async/threadpool_backend.cpp:402-412` | `execution_refs` 现在 pin 住槽与借用；uring 在**写 SQE 之前** claim，threadpool 在**系统调用之前** claim | claim 非 claimed → 说明 cancel 已获胜或条目陈旧；条目被丢弃、不产生第二终态 |
| 9 | 物理执行 | `S:src/async/uring_transport.cpp:161-187`；`S:src/async/threadpool_backend.cpp:486-502` | kernel / worker 独占使用 buffer+fd；这是借用真正“被触碰”的窗口 | 失败仍以 CQE/errno 作为终态来源（`uring_completion.cpp:161-171`） |
| 10 | 终态选择 `offer_terminal` | `S:src/async/detail/request_core.cpp:346-365` | 不可变终态写入槽；此后不可改写 | 重复/陈旧 → rejected，backend fail-fast |
| 11 | `release_execution` | `S:src/async/detail/request_core.cpp:393-409` | refs→0，借用触碰引用退休（`borrow_touch_fully_retired`） | 若有 refs 但无终态 → `premature_rejected`（禁止提前放借用） |
| 12 | publication | `S:src/async/detail/request_core.cpp:438-484` | 准入 = `terminal_chosen && execution_refs==0 && binding_live`（`:447`）；`published=true`、`outstanding--`（`:473`） | 不满足 → not_eligible/duplicate；`publication_inflight` 期间丢弃句柄会 fail-fast（`discard_public_result` 返回 not_visible_yet） |
| 13 | delivery（observer / owed event） | `S:src/async/uring_backend.cpp:578-614`；`S:src/async/threadpool_backend.cpp:356-384` | sink 只拿 `{key,kind}`；zero-op 用 `acquire_control` + `event_owed` 把槽 pin 到投递完成 | `release_control` 后才可能回收 |
| 14 | ready / `take` / consume | `S:src/async/detail/request_core.cpp:323-344`；`S:include/sluice/async/request_scope.hpp:131-143` | 绑定释放、槽可回收；`take_result` 清空句柄（`request.hpp:155-158`）；scope 槽在 ready 时释放（销毁已空 Request） | 陈旧/失配 → `empty`，adapter 视为 fatal（`events.cpp:104-106`） |
| 15 | adapter `harvest` → `event()` | `B:adapters/fio/src/events.cpp:91-130,254-266` | `u->error/resid` 在 staged 前写入；`io_u*` 在 `staged` 中留到 `event()`；adapter 槽在 harvest 即释放 | 上一批未消费完再进 getevents → fail-closed（`events.cpp:168-170`） |
| 16 | reclaim | `S:src/async/detail/request_core.cpp:580-631` | 谓词：published && !binding && refs==0 && control==0 && !inflight && observer retired；清 borrow/outcome/metadata、generation+1 | 槽复用必须重新初始化（mutant 覆盖的正是这些清零） |
| 17 | shutdown / 析构 | `S:src/async/async_io_context.cpp:469-563,77-97` | 结算到 **published**（不结算到 consumed）；析构另要求 `public_bindings==0` | 结算未完成 → `async_context_settlement_fail_fast`（`:94-96`）；有绑定 → `async_context_outstanding_fail_fast`（`:81-85`） |

### 4.3 borrow / move / shared 分类

| 类别 | 对象 | 事实 | 证据 |
|---|---|---|---|
| **move** | `Request<T>` 的责任 | move ctor/assign `noexcept`，源被清空；覆盖非终态目标 = 违约；同一句柄不可并发可变 | `S:include/sluice/async/request.hpp:110-122`；`S:docs/explicit-io-contract.md:704-724` |
| **move** | `ProgressOwner` | 驱动权句柄；move 后源置空；**不更新** `owner_thread_` | `S:src/async/async_io_context.cpp:54-68` |
| **move/值** | `RequestObservation`、`Result`、`EffectReport`、`FileInfo` | 按值返回给调用方，出槽后与槽无关 | `S:include/sluice/async/request.hpp:63-70,146-159`；`S:src/async/detail/request_core.cpp:735-752` |
| **borrow** | buffer、fd、`io_u*` | 见 §4.1；不复制、不延长寿命 | `S:src/async/detail/request_core.cpp:200`；`S:src/async/uring_transport.cpp:161-187`；`S:src/async/threadpool_backend.cpp:486-502`；`B:adapters/fio/src/files.cpp:21-46` |
| **borrow（裸反向指针）** | `RequestCore*`、`AsyncBackend*` | Request 持裸指针但不拥有；上下文必须先于句柄存活；析构以 `public_bindings==0` 反向兜底 | `S:include/sluice/async/request.hpp:211-213`；`S:docs/explicit-io-contract.md:744-755` |
| **shared（引用计数 pin，非所有权）** | `execution_refs` | 借用触碰 pin；禁止在无终态时归零 | `S:src/async/detail/request_core.cpp:380-409` |
| **shared（引用计数 pin）** | `control_refs` | owed-delivery 与 in-flight cancel 的 pin；可**晚于 publication** 存在，但不得触碰 buffer/改写结果（REQ-03 允许） | `S:src/async/detail/request_core.cpp:411-436,580-590`；`S:src/async/uring_backend.cpp:553-576,660-686` |
| **shared（无同步）** | `RequestScope::slots_`/`free_slots`/`finished_` | 无锁；类头未写线程契约；adapter 单线程使用（`FIO_NO_OFFLOAD` + `verify_async` 拒绝） | `S:include/sluice/async/request_scope.hpp:67-72,197-207`；`S:src/async/request_scope.cpp:46-55`；`B:adapters/fio/src/engine.cpp:71-73,184-199` |
| **复制（token）** | `RequestId`、`ScopeTicket` | 复制只获得查找/取消权，不获得存储或消费权 | `S:docs/explicit-io-contract.md:583-601` |

### 4.4 五个特别问题的判定

| 问题 | 判定 | 依据 | 残余风险 |
|---|---|---|---|
| buffer borrow 会否提前释放？ | **主路径不会** | publication 准入显式要求 `execution_refs==0`（`S:src/async/detail/request_core.cpp:447`）；`release_execution` 只在终态后才能归零并返回 `borrow_touch_fully_retired`（`:393-409`）；uring 在 `release_execution` 之后才 push 发布（`S:src/async/uring_completion.cpp:85-92`）；zero-op 无保留借用，LIFE-01 明确允许（`explicit-io-contract.md:968-971`）；非终态 `Request` 销毁/覆盖 fail-fast（`request_core.cpp:760-770`） | ① backend `prepared_ops_[slot]` 保留 fd/buffer 指针到下次 submit 才覆盖，虽不再解引用；② adapter `staged` 的 `io_u*` 已脱离 `close_file` 守卫（见 F2/F3） |
| fd 生命周期与 kernel 借用冲突？ | **不冲突（契约内），但库无法自证** | Sluice 在 `S:src/async` 内无对调用方 fd 的 `::close/::dup`；publication 只发生在 op 的 CQE 被 reap 之后；fio 侧 close 由 `f->references` 门控；adapter 的 `close_file` 见在飞槽即 terminate（`B:adapters/fio/src/files.cpp:52-68`），且 `queue` 用 `fd→access` 注册表拦截未注册/复用 fd（`B:adapters/fio/src/files.cpp:21-46`；`B:adapters/fio/src/queue.cpp:56-61`） | 若调用方在 acquired publication 前自行 close fd，库检测不到（LIFE-01 只能靠契约）；kernel 对在飞 op 自持 file 引用这一点未在本仓验证。`ProgressSource` 会关闭自己的 eventfd（`S:include/sluice/async/detail/progress_source.hpp:83,287`），那不是 fio fd。 |
| progress owner 会否被隐式转移？ | **会，存在 4 条隐式路径 + 1 个未防护 move** | ① `RequestScope` 构造时直接 claim（`S:src/async/request_scope.cpp:15-34`）；② 首次 `poll_progress`/`wait_one` 无句柄也把调用线程绑定为 owner（`S:src/async/async_io_context.cpp:184-188`）；③ `release_progress_owner_` 清空 `owner_thread_`，下一驱动者（可换线程）自动接上（`:198-202`）；④ `shutdown()` 在未绑定时把调用者绑定为 owner（`:869-873`）；⑤ move ctor 只拒绝“存在 ProgressOwner 句柄”，不检查 `drive_active_`，并把 `owner_thread_`/`notification_interest_live_` 复制到目标、清空源（`:99-128`） | 若另一线程在 drive 期间 move 上下文，驱动方随后会对已搬空对象解引用 `backend_`（`async_io_context.cpp:429-447`）；`operator=` 对目标有 `fail_fast_if_progress_binding_live_`，对源只查 `owner_claimed_`，不对称 |
| cancellation 被当成物理完成？ | **否** | `RequestCore::cancel` 只在 `!execution_claimed` 时选零效果取消（`S:src/async/detail/request_core.cpp:260-275` + `canceled_before_dispatch` 的 accounted/0，`S:include/sluice/detail/file_semantics.hpp:339-341`）；uring 在写 SQE **前** claim（`S:src/async/uring_transport.cpp:151-159`），threadpool 在系统调用**前** claim（`S:src/async/threadpool_backend.cpp:405-412`）；kernel cancel CQE 只释放 control、**不选终态**（`S:src/async/uring_completion.cpp:99-143`，伪造终态的 mutant 被 `#if` 排除）；字节 op 的 kernel-cancel 结果是 `canceled_racing_in_flight_attempt(0)` = **uncertain** 而非 success（`:48-49`；`S:include/sluice/detail/file_semantics.hpp:343-345`）；threadpool 把 running cancel 报成 `physical_interruption_unsupported`（`S:src/async/threadpool_backend.cpp:797-803`） | adapter **根本不用 cancel**：ops 表无 `.cancel`，scope 策略是 `drain`，`finish()` 又刻意不施加 cancel 策略（`B:adapters/fio/src/engine.cpp:127-128`；`S:src/async/request_scope.cpp:264-281`；行为由 `S:tests/request_scope_test.cpp:1126-1135` 钉住），故取消路径只有测试覆盖 |
| shutdown 是否结算已接受 Request？ | **是，结算到 published，不结算到 consumed** | `settlement_converged_` 要求 `internal_work_retired` + `outstanding==0` + `execution_refs==0` + `control_refs==0` + `publication_inflight==0` + `observer_registrations==0`（`S:src/async/async_io_context.cpp:469-477`），**不含** `public_bindings==0`（只有 mutant M4 加它，`:514-517`）；关闭 admission 后先 `cancel_outstanding_for_stop_`（只收 accepted&&!published，`request_core.cpp:98-108`）；已发布未消费的 Request 仍可查询/消费（SHUT-01/03，`explicit-io-contract.md:1216-1219,1255-1270`） | 健康失败/poison 时返回 `health_unresolved` 并**保留**借用（“CQE 缺席不能证明操作结束”，`S:src/async/uring_transport.cpp:445-465`），adapter 此时 terminate 而不把 buffer 交回 fio（`B:adapters/fio/src/events.cpp:138-158`）；`drain` 对挂死设备无有限上界（文档明示） |

### 4.5 发现清单

| ID | 级别 | 结论 | 证据 |
|---|---|---|---|
| F1 | 通过 | 借用边界正确：publication 严格晚于 `execution_refs` 归零，借用触碰引用不可提前释放 | `S:src/async/detail/request_core.cpp:393-409,447`；`S:src/async/uring_completion.cpp:85-92` |
| F2 | 残余风险 | backend 的 `prepared_ops_[slot]` 在借用结束后仍保留 fd/buffer/len，只在下一次 submit 覆盖；当前无解引用，属“借用记录残留” | `S:include/sluice/async/detail/uring_backend.hpp:159-167,302`；`S:src/async/uring_backend.cpp:479-486`；`S:src/async/threadpool_backend.cpp:257-259` |
| F3 | 残余风险 | `harvest` 在 ready 时释放引擎槽，但 `staged` 仍持 `io_u*` 至 `event()`；`sluice_close_file` 只扫引擎槽，该窗口仅由 fio 的 `f->references` 保护 | `B:adapters/fio/src/events.cpp:79-86,108,265`；`B:adapters/fio/src/files.cpp:52-68`；`F:filesetup.c:1897-1898` |
| F4 | 通过 | fd 借用与 close 顺序一致：在飞槽会阻止 `close_file`，fio 自身在 references 归零前不会 close | `B:adapters/fio/src/files.cpp:52-68`；`B:adapters/fio/src/queue.cpp:56-61`；`F:filesetup.c:1880-1921` |
| F5 | 风险（需并发误用） | progress owner 无外部可观测身份；线程绑定可被首次驱动/shutdown/释放隐式改变；`AsyncIoContext` move 未防护 `drive_active_`，且源侧不做 binding 检查 | `S:src/async/async_io_context.cpp:179-202,869-873,99-152` |
| F6 | 通过 | cancellation 语义不冒充物理完成；running op 的取消只产生“请求/不支持”处置，终态仍来自物理结果 | `S:src/async/detail/request_core.cpp:260-275`；`S:src/async/uring_completion.cpp:99-143`；`S:src/async/threadpool_backend.cpp:782-814` |
| F7 | 通过 | shutdown 结算全部 accepted 到 publication；不要求消费；析构再以 `public_bindings==0` fail-fast 兜底 | `S:src/async/async_io_context.cpp:469-563,77-97` |
| F8 | 文档缺口 | `RequestScope` 的线程/所有权契约未写在头文件：无锁、`take` 对陈旧 ticket 返回 `empty`、`wait_for` 不转移所有权、`finish()` 刻意忽略 cleanup policy | `S:include/sluice/async/request_scope.hpp:40-65,131-143,197-207`；`S:src/async/request_scope.cpp:264-281`；`S:tests/request_scope_test.cpp:1126-1135` |
| F9 | 潜在（序列化缺口） | `Request::cancel()` 经 `RequestBridge` 直接调 `backend->cancel_identity`，绕过 `AsyncIoContext::access_mtx_`（上下文级 `cancel()` 则持锁）；靠“结算前必先发布”才不与 `retire_execution_resources` 相撞 | `S:include/sluice/async/request.hpp:163-168`；`S:include/sluice/async/detail/async_backend.hpp:61-63`；`S:src/async/async_io_context.cpp:372-396`；`S:src/async/uring_backend.cpp:375-402` |
| F10 | 潜在（失败模式） | 若 scope 容量与 adapter 容量恒等式被破坏，`would_block` 会被映射成 `EAGAIN` 并以 `FIO_Q_COMPLETED` 上报（不是可重试的 BUSY），把瞬态容量问题变成 I/O 错误 | `B:adapters/fio/src/queue.cpp:8-14,76-86`；`B:adapters/fio/src/errno_map.cpp:19-20`；`S:src/async/request_scope.cpp:46-55,137-155` |

### 4.6 fio 侧强约束（由 fio 3.40 冻结树给出）

| 约束 | 内容 | 证据 |
|---|---|---|
| io_u / buffer 归 fio | engine 只在 `IO_U_F_FLIGHT` 期间借指针与缓冲区；`event()` 归还后 fio 可立即复用 | `B:adapters/fio/src/state.hpp:30`；`B:adapters/fio/src/events.cpp:108,265`；`F:io_u.c:2102-2103` |
| 每个在飞 io_u 持一个 file 引用 | `get_file(f)` 在取 io_u 时执行，`put_io_u` 时 `put_file_log`；`close_file` 只在 `f->references` 归零时调用 | `F:io_u.c:1488,856`；`F:filesetup.c:1897-1898` |
| 在飞上限 = `iodepth` | io_u freelist 尺寸即 `td->o.iodepth`，故 adapter 的 `capacity == iodepth` 恒等式成立 | `F:backend.c:1306`；`B:adapters/fio/src/queue.cpp:9-14,64-71` |
| 退出前排空 | `do_io` 末尾 `if (td->cur_depth) io_u_queued_complete(td, td->cur_depth)`，所以 `close_and_free_files`/`cleanup_io_u`/`close_ioengine` 之前已无在飞 | `F:backend.c:254-255,2087-2089` |
| 无 `.cancel` 钩子 | ops 表没有 cancel，`do_io` 的 cancel 分支被跳过；fio 只能等完成 | `B:adapters/fio/src/engine.cpp:184-199`；`F:backend.c:242-255` |
| 单驱动线程 | `FIO_NO_OFFLOAD` 使 fio 拒绝 `io_submit_mode=offload`；`verify_async` 被 init 拒绝 → queue/getevents/cleanup 同一 job 线程，与 `RequestScope` 的 owner claim 对齐 | `B:adapters/fio/src/engine.cpp:71-73,187`；`F:ioengines.c:58-62` |
| 批次契约 | 上一批 `staged` 未全消费就再进 getevents → adapter fail-closed | `B:adapters/fio/src/events.cpp:168-170` |

---

## 5. Lock Graph

### 5.1 锁清单与符号澄清

产品代码实际只有 **5 个 `std::mutex`**：

| 锁 | 定义 | 说明 |
|---|---|---|
| `RequestCore::mutex_` | `S:include/sluice/async/detail/request_core.hpp:295` | core 槽状态机单一 authority，叶锁 |
| `UringAsyncBackend::dispatch_mtx_` | `S:include/sluice/async/detail/uring_backend.hpp:320` | uring 的 dispatch_/publication_pending_/delivery_/router_/ledger/ring SQ |
| `ThreadPoolBackend::work_mtx_` | `S:include/sluice/async/detail/threadpool_backend.hpp:247` | 线程池的 dispatch_ 与 publication_pending_ 合并单锁；`work_cv_` 在 `:248` |
| `AsyncIoContext::access_mtx_` | `S:include/sluice/async/async_io_context.hpp:336` | 公共 API、接受事务、drive/owner/execution_closed 状态 |
| `ProgressSource::mtx_` | `S:include/sluice/async/detail/progress_source.hpp:491` | no-lost-wake/控制代际/eventfd drain 与 signal |

**不存在 `ThreadPoolBackend::queue_mtx_`**：`grep -rn queue_mtx` 在 Sluice 源码与 adapter 中均无命中；线程池的 `dispatch_` 与 `publication_pending_` 都由 `work_mtx_` 单锁保护（`S:include/sluice/async/detail/threadpool_backend.hpp:129-133,247-251`）。`S:docs/explicit-io-contract.md:1596` 也把 ThreadPool backend 描述为 merged-lock pass。io_uring 的两个队列由 `dispatch_mtx_` 保护。

### 5.2 Lock Graph 主表

P/T/R/U = publication / terminal visibility / reclamation / unique consumption。✅=该锁是当前实现的必需同步；◐=间接必需（数据可见性由 core 锁提供，该锁负责后端交接/顺序）；—=不承担该职责。

| 锁 | 获取点（代表；完整计数见下） | 持有范围 | 保护状态 | 快路径 / 慢路径 | 可竞争线程 | 同线程重入 | 必需 P/T/R/U |
|---|---|---|---|---|---|---|---|
| `RequestCore::mutex_` | 28 个产品获取点；`S:src/async/detail/request_core.cpp:166,187,213`（reserve/accept/rollback）；`227/236/241/247`（admission/health）；`252/261/278/298/325`（lookup/cancel/discard/observe/consume）；`348/368/381/394/412/425`（terminal/execution/control）；`439/463/487/514/534/561/575`（publication/observer）；`67/94/99`（occupancy/outstanding_summary/collect） | 整个 public 方法体；无回调；不再取其它锁（叶锁）；O(1) 状态迁移或 O(capacity) 扫描；`collect_outstanding` 在锁内向调用方 vector `push_back`（可能分配；方法为 noexcept，失败即 terminate） | `slots_` 全部字段：phase/generation/binding_live/terminal_chosen/published/publication_inflight/execution_claimed/observer_phase/execution_refs/control_refs/descriptor/borrow/outcome/metadata；`free_slots_`、`retired_count_`、`outstanding_`、`admission_open_`、`health_failed_` | 快：reserve/accept/lookup/claim_execution/release_execution/acquire/release_control/offer_terminal/begin_publication/complete_publication/consume（均 O(1)）。慢：occupancy/collect_outstanding/snapshot（O(capacity)）；shutdown 每轮 `settlement_converged_` 都调用 occupancy | 几乎所有线程：多 submitter、uring progress owner、threadpool workers、handle 的 ready/take/discard、cancel/observer/close/shutdown/dtor | 否；非递归，且其下不再取任何锁 | ✅ / ✅ / ✅ / ✅ |
| `UringAsyncBackend::dispatch_mtx_` | 14 个产品获取点；`S:src/async/uring_backend.cpp:489`（submit delivery 复位）、`565`（zero-op owed）、`621`（poll dispatch）、`641/650`（publication 快照/弹出）、`660/668`（owed 检查/清扫）、`311/365`（residue/internal_work）、`340`（dtor）；`S:src/async/uring_transport.cpp:49`（dispatch_after_accept）、`587`（cancel）；`S:src/async/uring_completion.cpp:100`（CQE）、`269`（overflow reconcile/poison） | 短：delivery 复位、zero-op owed 标志、publication 弹出、owed 记录清除。长：poll 的整个 dispatch 排空 + `io_uring_submit` 系统调用 + control service；dispatch_after_accept 同；cancel 的 remove_exact + running-cancel；CQE 的 router 反向 O(capacity) 查表；poison 的 O(capacity) 恢复；dtor 的 O(capacity) 扫描。宿主回调不在锁内 | `dispatch_`、`publication_pending_`、`delivery_`/`owed_count_`、`router_`/`cookie_free_list_`、`prepared_ops_` 的读、`next_cookie_`、`live_cookies_`/`live_control_sqes_` 的记账、`transport_ledger_`、`fatal_error_`、ring SQ 的 get_sqe/sqe_tail/submit。`prepared_ops_` 的写在锁外（`S:src/async/uring_backend.cpp:479`），靠保留槽所有权 + 入队锁建立交接 | 快：delivery 复位、zero-op owed、publication 弹出。慢：poll 排空/提交、dispatch_after_accept、cancel、CQE、poison、dtor | 多 submitter、uring progress owner、任意 cancel 线程、内部 work 查询/dtor；fio+uring 部署中只有 job 线程（绝大多数无竞争） | 否；非递归；嵌套 core 与 progress，但无同锁二次获取；无宿主回调在锁内 | ✅（后端发布交接）/ ◐（数据可见性在 core，锁负责 CQE→发布队列顺序）/ ◐（router/ledger 钉住借用）/ — |
| `ThreadPoolBackend::work_mtx_` | 15 个产品获取点；`S:src/async/threadpool_backend.cpp:262`（submit delivery 复位）、`302`（dispatch_after_accept 入队）、`345`（zero-op owed）；`394`（worker wait/dequeue/claim）、`436`（active--）、`462`（publication push）；`561/607`（poll 快照/残留）；`786`（cancel）、`825/830`（查询）、`857`（internal_work）、`869`（stop）、`881`（retire）；`118`（ctor 失败） | 短：入队/出队、delivery 复位、active--、publication push。worker 在锁内做 core claim 与 prepared 拷贝，但不跨 syscall；`work_cv_.wait` 期间释放锁。慢：poll 的 owed O(capacity) 扫描、cancel 的 remove_exact O(capacity)、quiescence O(capacity) | `dispatch_`、`publication_pending_`、`publish_batch_`、`owed_batch_`、`delivery_`/`owed_count_`、`active_workers_`、`stopping_`、`work_cv_` 的谓词 | 快：enqueue/dequeue、delivery 复位、owed 标志。慢：owed sweep、remove_exact、quiescence、stop | 多 submitter、N 个 worker、progress owner、cancel 线程、shutdown/dtor；fio+threadpool 为 job 线程 + 默认 4 workers | 否；非递归；嵌套 core；notify 在锁外；cv wait 不是重入 | ✅（worker→owner 发布交接）/ ◐ / ◐ / — |
| `AsyncIoContext::access_mtx_` | 34 个产品获取点；`S:src/async/async_io_context.cpp:79/101/114/133/148/161`（dtor/move）；`180/194/199/211`（drive/owner）；`255/276/296/316/336/356`（6 个 submit）；`376/402`（cancel/lookup）；`705/721/739/746`（progress control/notification）；`755/790/827`（observer）；`838/843/850/860/885/890`（query/close/stop/shutdown）；`558/582/590`（settlement/wait） | 长：整个 backend submit（含 uring 的 `io_uring_submit` 与可能的 poison O(capacity)）与整个 cancel；中：lookup/observer；短：drive entry/exit、query、close。**不跨 park**：wait_one 在进入等待前释放。destructor 在同一锁内做 `public_bindings==0` + `close_admission` | `backend_`/`core_` 指针生命周期、`drive_active_`、`owner_claimed_`、`owner_thread_`、`notification_interest_live_`、`execution_closed_`、`settlement_outcome_`；并串行化所有公共 API 与接受事务 | 快：drive entry/query/observer。慢：submit/cancel/shutdown/dtor。`Request::ready()`/`take_result()` 绕过它直达 core | 所有公共 API 调用者、progress owner 的入口/退出、dtor；fio 单 job 线程下无竞争 | 否；非递归；嵌套 backend/core/progress；宿主回调不在锁内；递归 poll 被 `drive_active_` 拒绝而非死锁 | —（仅接受线性化）/ — / — / — |
| `ProgressSource::mtx_` | 13 个产品获取点；`S:include/sluice/async/detail/progress_source.hpp:43/55/64`（control/health）；`92/123/154/184/205`（snapshot/wait 复核）；`235/253`（interrupt/signal）；`267/275/285`（exhausted/ack/retire） | 短：epoch/control/health 读写。`drain_notification_nolock_`/`wake_notification_nolock_` 在锁内做 eventfd read/write（非阻塞，含 EINTR 循环）；wait 不跨 poll/probe 持锁 | `progress_epoch_`、`control_epoch_`、两个 exhaustion、observed/acknowledged control、`health_failed_`、`notification_fd_`、`notification_pending_` | 快：signal/snapshot/interrupt。慢：wait_if_unchanged 的多次复核 + drain；但锁本身不跨 poll | 所有 signal 生产者（threadpool worker 每完成一次、zero-op、cancel、close/stop、poison）与 progress owner 的 snapshot/wait/drain；uring 正常完成由内核 eventfd 触发，不经过此 mtx | 否；非递归；叶锁；无回调 | — / — / — / —（仅 no-lost-wake/control 活性） |

### 5.3 锁序与嵌套

| 外层 → 内层 | 证据 |
|---|---|
| `access_mtx_` → `RequestCore::mutex_` | `S:src/async/async_io_context.cpp:255` → `S:src/async/uring_backend.cpp:447/514`；`755` → `S:src/async/detail/request_core.cpp:487`；`838` → `request_core.cpp:93` |
| `access_mtx_` → `dispatch_mtx_` | `async_io_context.cpp:255` → `uring_backend.cpp:489` 与 `uring_transport.cpp:49`；cancel `376` → `uring_transport.cpp:587` |
| `access_mtx_` → `work_mtx_` | `async_io_context.cpp:255` → `threadpool_backend.cpp:262/302`；cancel `376` → `threadpool_backend.cpp:786` |
| `dispatch_mtx_` → `RequestCore::mutex_` | `uring_transport.cpp:49` → `:152`；`uring_completion.cpp:100` → `request_core.cpp:70/89`；`uring_transport.cpp:587` → `request_core.cpp:590` |
| `dispatch_mtx_` → `ProgressSource::mtx_` | poison 路径：`uring_transport.cpp:233/278` → `:337` → `:523`（`signal_ready_progress` 在 dispatch_mtx 内）；`uring_completion.cpp:269` → `:272` |
| `work_mtx_` → `RequestCore::mutex_` | `threadpool_backend.cpp:394` → `:406`；`786` → `790`；`302` → `313`（测试注入分支） |
| `access_mtx_` → `ProgressSource::mtx_` | `async_io_context.cpp:843` → `845`；`850` → `853/855`；`705` → `707`；`739` → `741` |

- **未发现反向边**：`RequestCore::mutex_` 不调用任何 backend/context；`ProgressSource::mtx_` 不调用任何外部代码（probe/poll 均在锁外）。因此观测到的总序是 `access_mtx_` → {`dispatch_mtx_`, `work_mtx_`} → `RequestCore::mutex_`，以及 `dispatch_mtx_` → `ProgressSource::mtx_`、`access_mtx_` → `ProgressSource::mtx_`。
- `work_cv_.wait` 在等待时释放 `work_mtx_`（`S:src/async/threadpool_backend.cpp:394-395`），不构成持锁睡眠。
- 宿主回调在锁外运行：`deliver_event` 在 `S:src/async/uring_backend.cpp:610-614`（由 `607/680` 调用）与 `S:src/async/threadpool_backend.cpp:376-384`（由 `356/592` 调用）执行，符合 OBS-02（`S:docs/explicit-io-contract.md:793-798`）与 THREAD-02（`1023-1037`）。
- **未找到集中式锁序文档**；THREAD-02 要求 multi-lock 路径在引入前记录顺序与无反转（`S:docs/explicit-io-contract.md:1029-1030`）。当前顺序只能从调用链推断。

### 5.4 每次 I/O 的锁操作计数（静态路径计数，不是测量）

假设：一次非零长度 read；无 cancel、无 observer attach；正常完成；`deliver_event` 的 claim/retire 即使未 attach 也会取 core 锁；不含 `RequestScope::resolve_` 的 O(capacity) 无锁扫描，也不含 `AsyncStats`（fio adapter 不传 stats）。pass 级获取可能被多个 I/O 摊薄，故单列。

| 路径 | `RequestCore::mutex_` | `dispatch_mtx_` | `work_mtx_` | `access_mtx_` | `ProgressSource::mtx_` |
|---|---|---|---|---|---|
| uring 提交 | 3（reserve/accept/claim_execution） | 2（delivery 复位 + dispatch_after_accept） | 0 | 1（跨 backend/syscall 持有） | 0 |
| uring 完成（每 I/O） | 6 固定（offer/release/begin/complete/observer claim/retire）+ T 次 consume（T=take 次数） | 2（CQE + publication 弹出）+ pass 级 1–2 | 0 | 0（pass 级另计） | 0–1（zero-op/cancel；正常 CQ 由内核 eventfd） |
| uring 每 pass 额外 | 1–2（health + outstanding） | 1–2（立即工作/残留） | 0 | 2（drive entry/exit） | 1（exhausted） |
| threadpool 提交 | 2（reserve/accept） | 0 | 2（delivery 复位 + enqueue） | 1 | 0 |
| threadpool 完成（每 I/O） | 7 固定（claim/offer/release/begin/complete/observer×2）+ T 次 consume | 0 | 4–5（dequeue/active--/pub push + pass 快照/残留） | 0（pass 级另计） | 1（worker signal） |
| threadpool 每 pass 额外 | 1–2（health + outstanding） | 0 | 2（快照 + 残留） | 2 | 1（exhausted） |

**合计公式**：

- uring 每 accepted I/O 的 core 锁 = 3（提交固定）+ 6（完成固定）+ T = **9 + T**。
- threadpool 每 accepted I/O 的 core 锁 = 2（提交固定）+ 7（完成固定，claim 在 worker 侧）+ T = **9 + T**。
- 因此 “10 次/I/O” 只是 T=1 的特例；T 的定义与上界见 §2.1。
- T 是本次 getevents 的 take 次数，多个完成共享同一批次时 T 不按完成数线性增长；本节按 B=1、每批≈1 个完成计。

### 5.5 删除影响矩阵

| 锁 | happens-before | terminal visibility | borrow retirement | 并发错误语义 |
|---|---|---|---|---|
| `RequestCore::mutex_` | **是**。`offer_terminal` 写 outcome/metadata 与 `observe/consume` 读共用此锁（`S:src/async/detail/request_core.cpp:346-365`、`296-344`）；`begin_publication` 的资格检查与 `complete_publication` 的 published/outstanding 共用此锁（`438-484`）。删除后 threadpool worker→job thread 无 release/acquire | **是**。`ready()`/`take_result()` 都经 `RequestBridge::lookup/consume` 取同一 mutex（`S:include/sluice/async/request.hpp:130-159`；`S:src/async/detail/request_core.cpp:251-258,323-344`） | **是**。execution_refs 递减、published、binding_live、control_refs、observer_phase、publication_inflight 的联合谓词 `reclaimable_` 在同一锁内求值并推进 generation（`request_core.cpp:580-631`） | **是**。reserve/accept/rollback、cancel vs claim_execution、offer_terminal 去重、consume 唯一性、observer 状态机都依赖同一串行化；删除产生 double accept/double terminal/double consume/duplicate free |
| `UringAsyncBackend::dispatch_mtx_` | **是（后端侧）**。prepared_ops 写（`uring_backend.cpp:479`）经 dispatch_mtx 的 enqueue/dequeue 交给 owner；router/ledger/SQ 摘要也经此锁（`uring_transport.cpp:49,130-208`；`uring_completion.cpp:100`） | **间接是**。CQ 线程在锁内 offer_terminal/release_execution 并把 reservation 放入 publication_pending_（`uring_completion.cpp:70-92`）；删除后发布队列可能丢条目/重排，published 永不置位或过早 begin（begin_publication 会 not_eligible 并 fail-fast）。数据本身仍由 core 锁发布 | **是（后端侧）**。router 条目把 cookie 钉到 reservation/borrow，直到 CQE/control 退休；cancel 的 remove_exact 与 dispatch 的 pop/claim 在同一锁内（`uring_transport.cpp:587-600` vs `130-159`）。删除后队列/路由竞争可丢 CQE（execution_ref 永不释放→借用无法退休）或错配 terminal。不会绕过 core 的 reclaimable_ 谓词提前复用仍借用的槽 | **是**。SQ get_sqe/submit、ledger 物理序号、router/cookie、publication/owed 队列都从定义良好变为数据竞争；结果为 UB/不变式 terminate/请求滞留，而不是线性化的 terminal |
| `ThreadPoolBackend::work_mtx_` | **是**。prepared_ops 写（`threadpool_backend.cpp:257-259`）经 dispatch_ enqueue（`302-325`）与 worker dequeue（`394-411`）的 work_mtx 交给 worker；worker 的 publication_pending push（`461-464`）经同一锁交给 owner（`561-568`） | **间接是**。worker 在锁外 offer_terminal/release_execution 后才在锁内 push publication（`454-464`）；删除后 owner 可能看不到 push 或看到 reorder，published 永不置位。数据可见性仍由 core 锁 | **是（协议顺序）**。worker 在锁内 claim 后复制 PreparedBlockingOp（`406-411`），删除后可能用未发布的 prepared 参数执行错误 fd/buffer（直接内存/数据错误）；publication push 丢失会使 execution_ref 已释放但结果不发布。core 的 reclaimable_ 仍阻止提前回收 | **是**。work_cv 的谓词/唤醒、dispatch/publication 环、active_workers_/stopping_ 都依赖此锁；删除导致丢失唤醒、队列损坏、提前/错误 quiescence 统计 |
| `AsyncIoContext::access_mtx_` | **是（接受与生命周期）**。submit 在 access_mtx 内完成整个 backend reserve/accept（`async_io_context.cpp:255-268`），destructor 在同一锁内观察 `public_bindings==0` 并 close_admission（`79-93`）；删除后这些不再是同一线性化域。结果数据的 HB 不由它提供 | **否**。`ready()`/consume 绕过 access_mtx 直达 core（`request.hpp:130-159`） | **否**（不参与 execution_refs/reclaimable_）；但 submit 与 close/destruction 的线性化会失去 | **是**。多 submitter 的串行接受（THREAD-01、submit_concurrency_test 的窗口）、poll/wait/shutdown 的单一 drive 许可（drive_active_）、context move/destroy 与 API 调用的互斥，都依赖它；删除把可检测的 invalid_state/串行化变成 UB（双 poll、move/destroy 竞态） |
| `ProgressSource::mtx_` | **是（通知域）**。signal 的 epoch 递增/eventfd 写与 wait 的 epoch 复核/drain 共用 mtx（`progress_source.hpp:121-166,252-264,414-489`）；删除后丢失唤醒/控制代际竞态 | **否**。outcome/published 由 core 锁 | **否**（不直接参与 execution_refs/reclaim）；但丢唤醒可使 settlement 永不收敛，间接使借用无法退休 | **是**。PROG-02 no-lost-wake、PROG-04 控制粘性、health 失败粘性、eventfd drain 与写者的互斥都依赖它；删除是协议错误/挂起，不是直接数据 UAF |

### 5.6 跨线程交接与部署映射

| 生产者 → 消费者 | 机制 | 锁 | 证据 |
|---|---|---|---|
| fio job 线程 submit → uring progress owner | `dispatch_` 队列 + `publication_pending_` + 内核 CQ/eventfd | `dispatch_mtx_`；fio 中 owner 就是 job 线程 | `S:src/async/uring_transport.cpp:49`；`S:src/async/uring_backend.cpp:621`；`B:adapters/fio/src/engine.cpp:127-128` |
| submit → threadpool worker | `dispatch_` + `work_cv_` | `work_mtx_` | `S:src/async/threadpool_backend.cpp:302-331,394-411` |
| threadpool worker → progress owner | `publication_pending_` + eventfd signal | `work_mtx_` + `ProgressSource::mtx_` | `S:src/async/threadpool_backend.cpp:457-466,561-568` |
| backend → handle 消费者 | core slot 的 outcome/published/binding_live | `RequestCore::mutex_` | `S:src/async/detail/request_core.cpp:346-365,323-344`；`S:include/sluice/async/request.hpp:130-159` |
| cancel → backend | `access_mtx_` → dispatch/work → core；publication_pending_ | 全部相关锁 | `S:src/async/async_io_context.cpp:376-395`；`S:src/async/uring_transport.cpp:587-618`；`S:src/async/threadpool_backend.cpp:782-814` |
| 内核 CQ → uring owner | CQ ring + eventfd（peek 不加 C++ 锁；router 处理加 dispatch_mtx_） | `dispatch_mtx_` | `S:src/async/uring_completion.cpp:200-229,100` |
| fio adapter 内部 | **无 adapter 锁**；`EngineState` 只在 job 线程 | 无 | `B:docs/fio-integration/ADAPTER-ARCHITECTURE.md:155-163,249-252`；`B:adapters/fio/src/engine.cpp:187`；`B:adapters/fio/src/events.cpp:91-130` |

| 部署 | 主要竞争形态 | 结论 |
|---|---|---|
| fio + uring | job 线程同时是 submitter 与 progress owner；Sluice 不创建用户态线程 | 这 5 把用户态锁基本无竞争；每 I/O 成本主要是固定 lock/unlock + 系统调用/ring 内存交接，不是 futex 等待 |
| fio + threadpool | job 线程 + 默认 4 workers（`B:adapters/fio/src/engine.cpp:34`） | `work_mtx_`、`RequestCore::mutex_`、`ProgressSource::mtx_` 有真实跨线程竞争、唤醒与 cacheline 迁移；每完成一次 worker 还要 signal progress |
| 多线程 product API | 任意线程 submit/cancel/probe/observer；owner 固定 | `access_mtx_` 串行化全部 submit；core 单锁串行全部槽；dispatch/work 为 backend 私有锁；ready/consume 只取 core |

### 5.7 原子操作、无锁点与锁缺失

| 同步/无锁点 | 位置 | 作用与风险 |
|---|---|---|
| `stop_policy_` atomic | `S:include/sluice/async/async_io_context.hpp:345`；`S:src/async/async_io_context.cpp:465/487/506` | 故意允许 owner 在锁外读取升级后的 policy；只承载 shutdown policy |
| uring live counters | `S:include/sluice/async/detail/uring_backend.hpp:324-326`；`S:src/async/uring_transport.cpp:195/295/427/490/581` | relaxed 记账；生命周期权威仍是 queue/ledger/core refs，不是这些 counter |
| threadpool `syscall_count_` | `S:include/sluice/async/detail/threadpool_backend.hpp:258`；`S:src/async/threadpool_backend.cpp:434` | 纯 telemetry |
| `AsyncStats` | `S:include/sluice/measurement.hpp:83-97` | **非 atomic**。`canceled_ops` 可由并发 `Request::cancel`（绕过 access_mtx_，`request.hpp:163-168`）与 CQ 完成路径在不同锁下写（`uring_transport.cpp:602`、`uring_completion.cpp:78`、`threadpool_backend.hpp:234`）；fio adapter 不传 stats，故当前 adapter 路径不触发 |
| `publish_one` 在 backend 锁外读 `delivery_` 记录 | `S:src/async/uring_backend.cpp:589-593`；`S:src/async/threadpool_backend.cpp:361-365` | 由 core 的 `publication_inflight` 钉住槽（`begin_publication` 置位 `request_core.cpp:450`；`reclaimable_` 要求其为 false `586-589`）；`kind` 在 `complete_publication` 前复制，之后不再读记录。该读不受 backend 锁保护，但受 core 发布 pin 保护 |
| `RequestScope` 无锁槽表 | `S:include/sluice/async/request_scope.hpp:46-60,97-143`；`S:src/async/request_scope.cpp:46-60` | scope 级无锁；依赖单 owner/单 job 线程。ADAPTER-ARCHITECTURE 明确说明 THREAD-01 的他线程 submit 是 context 级承诺、不是 scope 级（`B:docs/fio-integration/ADAPTER-ARCHITECTURE.md:249-252`） |
| fio adapter 无锁 | `B:adapters/fio/src/state.hpp:21-68`；`engine.cpp`；`queue.cpp`；`events.cpp` | 单线程不变式；`FIO_NO_OFFLOAD`（`engine.cpp:187`）+ 拒绝 verify_async（`engine.cpp:71-73`）排除 offload/verify 线程 |
| uring submit 的无锁读 | `S:src/async/uring_backend.cpp:433`（have_ring_）、`436`（fatal_error_） | writer 在 `dispatch_mtx_` 下写 `fatal_error_`（`uring_transport.cpp:340`）；submit 读不持锁。若 submit 与 poll/poison 合法并发，这是形式上的数据竞争（未用 TSan 验证）；后续 `dispatch_after_accept` 会在锁内重新检查并走 poison 退休，因此功能上可能被兜底，但不改变 C++ 语义风险 |
| `execution_closed_` 的无锁读 | `S:src/async/async_io_context.cpp:480` | 写者在 `access_mtx_` 下（`558-560`）；该读依赖单一 owner drive 纪律（THREAD-01），非该锁保护 |

### 5.8 与合同条款的对应

| 合同条款 | 锁图对应 |
|---|---|
| REQ-02 接受事务/关闭竞争（`S:docs/explicit-io-contract.md:603-627`） | `access_mtx_` 串行化整个 backend submit；`RequestCore::mutex_` 串行化 reserve/accept/rollback 与 admission close |
| REQ-04 发布/借用边界（`656-671`） | `RequestCore::mutex_` 提供 outcome/published 的 release/acquire；`begin_publication` 要求 execution_refs==0（`request_core.cpp:447`） |
| REQ-05 回收谓词（`673-687`） | `reclaimable_` 与 `release_slot_`/generation 推进均在 core 锁内（`request_core.cpp:580-631`） |
| OBS-02 投递在锁外（`779-809`） | `deliver_event` 在 backend/core 锁外调用 sink（`uring_backend.cpp:610-614`；`threadpool_backend.cpp:376-384`） |
| THREAD-01/02（`1001-1037`） | `access_mtx_` 负责串行接受/驱动许可；backend 私有锁分别保护 ring/queue；core 单锁负责槽状态机；未发现锁反转，但无集中式锁序文档 |
| PROG-02/PROG-04（`865-949`） | `ProgressSource::mtx_` 保护 epoch/control/drain 的 no-lost-wake 与粘性控制 |
| SHUT-02/SHUT-04（`1228-1295`） | `access_mtx_` 的 destructor 临界区（`async_io_context.cpp:79-93`）保证 `public_bindings==0` 与 close_admission 同一线性化域；backend 的 quiescence 检查在 `dispatch_mtx_`/`work_mtx_` 下 |

### 5.9 成本分类（不得用采样份额替代锁等待时间）

| 成本类别 | 在锁图中的落点 | 本次数据 |
|---|---|---|
| uncontended lock/unlock 固定成本 | 每次 acquire/release；每次 I/O 的固定获取次数见 §5.4。原语校准：本机 `g++ -O2` 下 `std::mutex` 无竞争 lock+unlock 约 **7.8 ns/次** | 只有原语校准，无产品路径计时 |
| contended waiting | futex 慢路径；最长候选：`access_mtx_`（跨整个 backend submit，uring 含 `io_uring_submit`）、`dispatch_mtx_`（整个 dispatch 排空/提交/poison）、core 的 O(capacity) occupancy/collect、progress 的 eventfd 系统调用。短候选：worker 的 `work_mtx_`（不跨 syscall） | **未测量**；未使用任何 perf 采样份额换算锁等待 |
| cacheline bouncing | 锁字本身；`RequestCore` 的 counters（`S:include/sluice/async/detail/request_core.hpp:291-295`）与 `slots_`；`BoundedDispatchQueue` 的 `head_/size_/capacity_`（`S:src/async/uring_internal.hpp:140-143`）；`BoundedHandleRing`（`S:include/sluice/async/detail/threadpool_backend.hpp:161-164`）；`delivery_` 记录（`S:include/sluice/async/detail/uring_backend.hpp:198-204`、`S:include/sluice/async/detail/threadpool_backend.hpp:134-140`）；`router_`（`S:include/sluice/async/detail/uring_backend.hpp:178-191`）；progress 的 mtx+epochs（`S:include/sluice/async/detail/progress_source.hpp:491-509`） | 原语校准：两线程同 cacheline atomic 约 **18.8 ns/op**，分离 cacheline 约 **3.7 ns/op**；非产品测量 |
| atomic operations | `stop_policy_`（`S:include/sluice/async/async_io_context.hpp:345`；store/load `S:src/async/async_io_context.cpp:465/487/506`）；uring `live_cookies_`/`live_control_sqes_`/`submit_flushes_`（`S:include/sluice/async/detail/uring_backend.hpp:324-326`；`S:src/async/uring_transport.cpp:195/295/427/490/581`）；threadpool `syscall_count_`（`S:include/sluice/async/detail/threadpool_backend.hpp:258`；`.cpp:434`）；测试 gate 指针 | 原语校准：atomic fetch_add 约 **6.1 ns/op**；非产品测量 |
| critical-section work | `access_mtx_` 跨 backend submit（`S:src/async/async_io_context.cpp:255-263` → `S:src/async/uring_backend.cpp:447-529` → `S:src/async/uring_transport.cpp:49-97` → `218-235` 的 `io_uring_submit`）；`dispatch_mtx_` 的 poll 排空（`S:src/async/uring_backend.cpp:621-634`）、router O(capacity) 扫描（`S:src/async/uring_transport.cpp:551-556`）、poison O(capacity)（`337-524`）；`work_mtx_` 的 owed O(capacity) 扫描（`S:src/async/threadpool_backend.cpp:570-583`）与 remove_exact O(capacity)（`64-87`）；core 的 occupancy/collect O(capacity)（`S:src/async/detail/request_core.cpp:67-90,98-108`）；progress 的 eventfd read/write（`S:include/sluice/async/detail/progress_source.hpp:414-460,466-489`） | 静态 O(1)/O(n)/syscall 分类，非计时 |
| cross-thread handoff | 见 §5.6。所有数据交接都是 mutex/condvar 协议；没有 lock-free 的 ready/publication 路径 | 静态分类 |

原语校准命令：`g++ -O2 -std=c++20 -pthread /tmp/lock_calib_audit/calib.cpp -o /tmp/lock_calib_audit/calib && /tmp/lock_calib_audit/calib`；`.../bounce`。机器：Intel Xeon E5-2666 v3，20 CPU。**这些数字不是产品锁的等待时间，也不替代产品锁等待时间。**

---

## 6. Machine-Work Inventory

### 6.1 基线与二进制来源

| 项 | 值 | 证据 |
|---|---|---|
| 产品代码 | Sluice `5d94f233`，工作区干净 | `git -C /home/jnhu/Source/Sluice log --oneline -1`；`git status --porcelain -uno` 空 |
| `.so` 构建源 | 同一 revision 的独立 checkout，未被改动 | `B:SLUICE_REVISION`=`5d94f233...`；`B:build/ring-pic/sdk/.ready` 同 SHA；`git -C B:build/ring-pic/sluice log --oneline -1` 同 SHA，`status --porcelain -uno` 空 |
| 链接内容 | 7 个受审 TU 全部在 `libsluice_async.a` 内 | `ar t B:build/ring-pic/sdk/lib/libsluice_async.a` |
| 反汇编对象 | release `fio-sluice-ring-sym.so`（未 strip，GCC 16.2.1，约 1.13 MB） | `objdump -d --no-show-raw-insn -C`；`.comment` = `GCC: (GNU) 16.2.1` |
| capacity 定义 | `capacity == td->o.iodepth`，全链路同一值 | `B:adapters/fio/src/engine.cpp:88,112,127`；`F:ioengines.c:304-307` |

### 6.2 提交阶段（每次 `sluice_queue` → 返回 `FIO_Q_QUEUED`）

| # | 工作项 | 实现位置 | 每次 I/O 工作量公式 | 反汇编证据 |
|---|---|---|---|---|
| 1 | fd→FileAccess 哈希查找 | `B:adapters/fio/src/queue.cpp:56` | O(1) 均摊：**1 条 64 位 `div`**（桶取模）+ 桶头链遍历（命中 1 节点，每多一节点再加 1 条 `div`） | `D:0x3fc3 div %r10`、`D:0x4001 div %r10` |
| 2 | 适配器自由槽栈 | `B:adapters/fio/src/queue.cpp:64,72-73` | O(1)：1×empty + 1×back + 1×pop_back | — |
| 3 | ddir 运行时分支 | `B:adapters/fio/src/queue.cpp:50,89-98,110-111` | ≤4 次比较 + 1 次跳转到 4 个 `submit_*` 之一 | — |
| 4 | `RequestScope::reserve_slot_` first-fit 扫描 | `S:src/async/request_scope.cpp:47-53`；被内联进 `submit_read` | **1…C 次** 单字节 `state` 读，步长 64 B，从 index 0 起；空表时 C | `D:0x8b51 cmpb $0x0,(%rax)`、`D:0x8b44 add $0x40,%rax`、`D:0x8b48 cmp %rdx,%rbx` |
| 5 | `Reservation` guard | `S:include/sluice/async/request_scope.hpp:79-87` | 2 次写；失败路径 `release_slot_`（variant 索引 switch + 4 次写） | `D:0x8b66 movb $0x1,(%rax)`（state=reserved） |
| 6 | `AsyncIoContext::submit_read` | `S:src/async/async_io_context.cpp:249-269` | 1×precheck + **1 mutex**（`access_mtx_`）+ **1 次间接虚调用** + 1 mutex | `D:0x4c36 pthread_mutex_lock`、`D:0x4c6e call *0x50(%rax)`、`D:0x4d02 pthread_mutex_unlock` |
| 7 | `RequestCore::reserve` | `S:src/async/detail/request_core.cpp:165-182` | **1 mutex** + O(1) `free_slots_.pop_back` + 4 次槽字段写 | 每函数统计：`RequestCore::reserve` 1L/1U |
| 8 | `RequestCore::accept` | `S:src/async/detail/request_core.cpp:184-210` | **1 mutex** + O(1) 索引校验 + descriptor/borrow 约 80 B 写 + `++outstanding_` | `accept` 1L/1U |
| 9 | delivery 记录初始化 | `S:src/async/uring_backend.cpp:488-500` | **1 mutex**（`dispatch_mtx_`）+ 3 字段写 | — |
| 10 | `dispatch_after_accept` | `S:src/async/uring_transport.cpp:43-102` | **1 mutex** + 1×push_back + 内层 dispatch 循环（常态 1 次） | — |
| 11 | `dispatch_one_locked` | `S:src/async/uring_transport.cpp:130-209` | `io_uring_get_sqe` + **1 core mutex**（`claim_execution`）+ cookie 分配 + SQE 约 16 B 写 + router 32 B 初始化 + **1 × `lock addq`**（`live_cookies_++`）+ ledger append + `remove_exact` O(1)（命中队首，无搬移） | `D:0x12c98 lock addq $0x1,0x128(%rbx)` = `live_cookies_` |
| 12 | `submit_transport_locked` | `S:src/async/uring_transport.cpp:218-235` | **1 × `lock addq`**（`submit_flushes_++`）+ **1 次 `io_uring_enter` 系统调用** + 按 rc 逐条 ledger `pop_front` | `D:0x12a97`/`D:0x13b9b lock addq $0x1,0x120`；`D:0x13bab call io_uring_submit` |
| 13 | `RequestScope::commit_` | `S:include/sluice/async/request_scope.hpp:99-109` | 1 次 variant 索引 switch + **48 B 拷贝**（`sizeof(Request)`）+ 2 次单字节写（索引、state=owning） | `D:0x8c8f-0x8ca9` 3×`movups`（48 B）、`D:0x8ca1 movb $0x1,0x38`、`D:0x8cb9 movb $0x2,(%rbx)` |
| 14 | 适配器槽写入 | `B:adapters/fio/src/queue.cpp:102-107` | `slot.u` + `slot.kind` + `slot.data`（32 B）+ `slot.sync`（32 B）+ `++pending_submits` | `EngineState::Slot` = 80 B（harvest 末尾 `/80` 魔数 `0xcccccccccccccccd`）⇒ `RequestId`/`ScopeTicket` = 32 B |

提交阶段合计（uring 常态）：**6 次 mutex 加解锁**（access_mtx 1、core 3 = reserve/accept/claim_execution、dispatch_mtx 2 = delivery 复位/dispatch_after_accept）+ **2 条普通 read/write 可确定的 `lock` 前缀 RMW**（`live_cookies_++`、`submit_flushes_++`；完成路径再加 `live_cookies_--`，每 I/O 合计 3 条）+ **1 次 `io_uring_enter`** + **1 条 `div`** + 1 次虚调用 + (1…C) 次字节扫描。

**结构性事实：四张并行槽表，索引互不相通。** 适配器 `slots` 为 80 B×C（`B:adapters/fio/src/state.hpp:27-34`；harvest 的 `/80` 魔数 `D:0x2f23`）；`RequestScope::slots_` 为 64 B×C（`S:include/sluice/async/request_scope.hpp:67-72`；take 扫描步长 `D:0x9374 add $0x40`）；`RequestCore::slots_` 为 168 B×C（`D:0x14e91 imul $0xa8`）；uring `router_` 为 32 B×C（`D:0x11ec0 sub $0x20`），另有 `delivery_` 与 `prepared_ops_`。同一请求在这些表里的下标彼此独立，这是必须靠 O(C) 扫描来“对表”的结构性原因。

### 6.3 harvest（每次 `sluice_getevents`，A 轮）

| # | 工作项 | 实现位置 | 工作量公式 | 证据 |
|---|---|---|---|---|
| 1 | 适配器槽遍历 | `B:adapters/fio/src/events.cpp:92-129` | **O(C) 每轮 harvest**：C 次 80 B 槽读（空槽仅 1 次判空）；缓存足迹 80·C B/轮（C=128 → 10 KB） | `B:adapters/fio/src/events.cpp:92`；循环上界每次迭代重算 `slots.size()`（`sar $4` + 魔数 `/5`） |
| 2 | 每占用槽 `scope->take()` | `B:adapters/fio/src/events.cpp:100,114` → `S:include/sluice/async/request_scope.hpp:132-143` | **O(C) 每 take**（最坏 C，期望=命中槽下标）：C 次 `state` 字节比较；对每个 owning 槽再 +1 次 variant 索引字节比较 +1 次 `core_` 判空 + **4 次 id 字段比较** | `D:0x9320 cmpb $0x2`（owning）、`D:0x9325/0x932a`（variant idx==1）、`D:0x9330`（valid）、`D:0x9342/0x9346/0x9351/0x935b`（context/slot/valid_/generation） |
| 3 | 每次 take 的 core 锁 | `S:src/async/detail/request_core.cpp:325` | **1 mutex 每次 take**，pending 也要付：`consume_public_result` 先加锁再判 `published` | `RequestBridge::consume` 1L/3U；`consume_public_result` 1L/1U |
| 4 | ready 时的核心回收 | `S:src/async/detail/request_core.cpp:339-343,592-631` | O(1) + `release_slot_`：约 20 次字段零写（3×`movups`）+ generation 自增 + `free_slots_.push_back` | `D:0x159ab-0x15a12`（`release_slot_` 106 条指令） |
| 5 | 结果聚合拷贝 | `S:include/sluice/async/request.hpp:146-159`；`S:src/async/detail/request_core.cpp:735-752` | 每次 take ≥2 份约 40–48 B 的 `RequestObservation`/`RequestResultData` 按值拷贝；pending 还要构造一次默认 `Result{invalid_state}`（`request.hpp:66`） | take 内 `movdqa/movaps` 序列（`D:0x93ef-0x94e7`） |
| 6 | 暂存与槽释放 | `B:adapters/fio/src/events.cpp:108-109,79-86` | O(1)：1×`staged.push_back`（`reserve(capacity)`，无分配）+ 4 写 + `free_slots.push_back` | `vector<Completion>::allocate` 只出现在增长分支（`D:0x302b`） |
| 7 | getevents 循环 | `B:adapters/fio/src/events.cpp:202-246` | 每轮：harvest + poll_progress（+ 必要时 wait_one）；**A ≥ 1，稳态 A=2**（203 行 harvest 在前、218 行 poll 在后，poll 新产出的完成要到下一轮才被 harvest） | `D:0x3510-0x3850` 中 harvest 2 个调用点、poll_progress 2 个、wait_one 2 个 |

**harvest 合计上界：每轮 O(C + K·C)，其中 K=在飞请求数；每次 getevents 为 A·O(C + K·C)**。C=K=128、A=2 时代数上界 ≈ 2×(128 + 128×128) ≈ 33k 次槽访问。

### 6.4 CQE / publication / 唤醒（uring）

| # | 工作项 | 实现位置 | 工作量公式 | 证据 |
|---|---|---|---|---|
| 1 | `reap_cqes` 批量取 CQE | `S:src/async/uring_completion.cpp:200-229` | 每批 ≤32：`io_uring_peek_batch_cqe` + 每 CQE `io_uring_cqe_seen`（CQ head 写） | 源码；无锁 |
| 2 | `handle_one_cqe` | `S:src/async/uring_completion.cpp:99-198` | **1 mutex 每 CQE**（`dispatch_mtx_`）+ O(1) 控制 CQE 判定 | `D:0x1118d pthread_mutex_lock`，尾部 `D:0x111b2 jmp pthread_mutex_unlock`（tail-call，故函数内 unlock 计数为 0） |
| 3 | cookie→router 定位 | `S:src/async/uring_transport.cpp:551-557` | **O(C) 每 CQE**：从 `router_.size()-1` 倒序扫，每项 2 次比较（`in_use` 字节 + `cookie` 8 B），步长 32 B | `D:0x11ecd cmpb $0x0,-0x5(%rax)`、`D:0x11ed3 cmp %rsi,-0x20(%rax)`、`D:0x11ec0 sub $0x20,%rax` |
| 4 | `finalize_operation_terminal_` | `S:src/async/uring_completion.cpp:70-95` | **2 mutex**（`offer_terminal` + `release_execution`）+ publication 入队 O(1) + `retire_router_entry_`（**1 × `lock subq`**） | `D:0x112c2 lock subq $0x1,0x130`（control）/`D:0x11f70 lock subq $0x1,0x128`（`live_cookies_--`） |
| 5 | 发布批边界 | `S:src/async/uring_backend.cpp:638-657` | 每批 1 mutex + **每 publication 1 mutex**（逐条 `pop_front`） | `poll[clone .part.0]` 5L/7U（循环内单点复用） |
| 6 | `publish_one` | `S:src/async/uring_backend.cpp:578-608` | **2 mutex**（begin/complete_publication）+ O(1) + `try_reclaim_` | 源码；`complete_publication` 1L/1U |
| 7 | `deliver_event` | `S:src/async/uring_backend.cpp:610-614` | **2 mutex**（claim/retire_observer_delivery）+ **1 次虚调用位置**，但 release 下被去虚化：仅一次 vtable 槽比较，回调体为空 | `D:0x10b93 lea ReferenceReadySink::on_ready` → `D:0x10ba1 cmp %rdx,%rax` → `jne` 才 `call *%rax`；相等时直接尾跳到 retire |
| 8 | owed 事件扫描 | `S:src/async/uring_backend.cpp:658-686` | 仅当 `owed_count_ != 0`：O(C) 扫描 + **每 owed 项 1 mutex**（普通字节 I/O 路径不触发） | 源码；`owed_count_` 是 `size_t`，非原子 |
| 9 | 完成唤醒 | `S:src/async/uring_backend.cpp:259-283`（`io_uring_register_eventfd`）+ `S:include/sluice/async/detail/async_backend.hpp:33-39` | 0 次用户态写：构造期把 CQE 通知绑到 eventfd，内核在 CQE 到达时写；用户态 `signal_ready_progress()` 仅在 dispatch-retry / zero-op / cancel 路径 | `S:src/async/uring_backend.cpp:263-281` |
| 10 | 停车等待 | `S:include/sluice/async/detail/progress_source.hpp:108-232`（`wait_if_unchanged`）、`S:src/async/async_io_context.cpp:573-696`（`wait_one`） | 每次 `wait_one`：**3 mutex**（`access_mtx_`：execution_closed、stats 计数、drive_entry_admitted_）+ 每轮 park **4 mutex**（progress `mtx_`）+ **1 次 `poll()` 系统调用** | `wait_one` 13L/24U；`poll` 见 `progress_source.hpp:203` |
| 11 | 每轮 progress 通过 | `S:src/async/async_io_context.cpp:429-439` | **2 次虚调用**（`backend_->poll_progress`、`backend_->outstanding`）+ **1 core mutex**（`health_failed`）+ **1 progress mutex**（`wait_health_failed`） | `D:0x578d call *0x10(%rax)`、`D:0x5799 call *0x20(%rax)`、`D:0x57ac call RequestCore::health_failed` |

### 6.5 每 I/O 计数器汇总（对比两后端）

| 开销类别 | uring 后端 | threadpool 后端 | 证据 |
|---|---|---|---|
| `RequestCore::mutex_` 加解锁 | **9 + T 次/I/O**（固定 9 + harvest 的 T 次 consume；T=1 时 10） | 同 9 + T（固定 9 的 claim 在 worker 侧） | `S:include/sluice/async/detail/request_core.hpp:295`；各方法 1L/1U |
| 后端私有 mutex | `dispatch_mtx_` 约 2 次/I/O + 3 次/轮 | `work_mtx_` 约 5 次/I/O + 2 次/轮 | `S:src/async/threadpool_backend.cpp:262,302,436,462,561,607` |
| `access_mtx_` | 1 次/提交 + 3 次/getevents | 同 | `S:src/async/async_io_context.cpp:255,582,590,595` |
| 原子 RMW（`lock` 前缀） | **3 次/I/O**：`live_cookies_++`（提交）、`submit_flushes_++`（提交）、`live_cookies_--`（完成） | 1 次（`syscall_count_++`） | `D:0x12c98`,`D:0x11f70`,`D:0x13b9b`；`S:src/async/threadpool_backend.cpp:434` |
| 非原子共享计数 | `owed_count_`、`outstanding_` 等普通字段，靠 mutex 保护 | 同（`owed_count_`、`active_workers_`） | `S:include/sluice/async/detail/uring_backend.hpp:304-305`；`S:include/sluice/async/detail/threadpool_backend.hpp:244,253` |
| 堆分配/释放 | **0/I/O**（稳态）；`operator new/delete` 只存在于各 vector 增长分支，被 `reserve(capacity)` + `capacity==iodepth` 排除 | 同 | 增长分支：`harvest D:0x302b`、`queue D:0x43f2`、`release_slot_ D:0x15ac8`；reserve：`engine.cpp:133,138`、`request_scope.cpp:34`、`request_core.cpp:41` |
| 虚调用 | 1/提交（`submit_read`）+ 2/轮（`poll_progress`、`outstanding`）+ 1/完成（sink，**已去虚化**） | 同 | `D:0x4c6e`、`D:0x578d`、`D:0x5799`、`D:0x10ba1` |
| variant 访问 | 热路径为**索引字节比较**，无抛异常路径；`__throw_bad_variant_access` 只被 `settle_and_release_` 冷路径和 `wait_for` 引用 | 同 | objdump 交叉引用：`D:0x114c`（settle cold）、`D:0x11aa/0x11b8`（wait_for cold） |
| 系统调用 | 1 × `io_uring_enter`/I/O（逐条提交，未批处理）+ 停车时 1 × `poll` | 1 × `pread/pwrite` + 1 × `eventfd write` + 1 × futex wake + 1 次线程切换 | `S:src/async/uring_transport.cpp:87,231`；`S:src/async/threadpool_backend.cpp:466`、`S:include/sluice/async/detail/progress_source.hpp:473` |

### 6.6 上下界汇总

| 工作项 | 下界 | 上界 | 均值 |
|---|---|---|---|
| `reserve_slot_`（每提交） | 1 | C | 依赖空闲槽分布（UNKNOWN） |
| `resolve_`（每 take） | 1 | C | 命中槽下标（UNKNOWN，非均匀） |
| 适配器 harvest 槽扫描（每轮） | C | C | C（固定） |
| harvest（每轮，K 在飞） | C + K | C + K·C | C + K·C/2（若命中位置均匀） |
| getevents（每调用） | 1 轮 harvest | A 轮，A 无静态上界 | 稳态 A=2 |
| `find_live_router_cookie_`（每 CQE） | 1 | C | 取决于 router 占用位置（UNKNOWN） |
| mutex 次数（每 I/O） | 9 + 1（T=1） | 9 + 2C（满占用） | 取决于 T（UNKNOWN） |
| 分配次数（每 I/O） | 0 | 0（不变量成立时） | 0 |

---

## 7. UNKNOWN / NOT_IDENTIFIED

> `UNKNOWN` = 静态无法确定或本次未测量；`NOT_IDENTIFIED` = 预期存在的符号/工件/机制在受审对象中未找到。

| ID | 类型 | 项 | 当前证据/原因 | 影响 / 解除条件 |
|---|---|---|---|---|
| U-01 | UNKNOWN | 每次 getevents 的 harvest 轮数 A、take 次数 T 的精确值 | A 依赖完成到达顺序；T 依赖占用槽分布与 ready 槽下标；本文只给 `A≥1`、`T≤h·C`、B=1 典型 `K+k` | 需要运行时插桩/计数；不影响公式上界 |
| U-02 | UNKNOWN | `reserve_slot_`、`resolve_` 的平均扫描长度 | 只给 `[1,C]` 硬界；均值取决于槽占用/释放顺序 | 需要运行时直方图 |
| U-03 | UNKNOWN | `find_live_router_cookie_` 的平均扫描长度 | 只给 `[1,C]`；取决于 router 占用位置 | 需要运行时直方图 |
| U-04 | UNKNOWN | `io_uring_submit` 的 SQ 满/重试次数 R | 代码允许 `submit_transport_locked` 重试；稳态 `queue_depth==capacity` 时通常 0 | 需要内核 SQ 满事件计数 |
| U-05 | UNKNOWN | io_uring 物理执行落点（内核内联 vs io-wq 工作线程） | 由内核版本/文件系统/IOCB flag 决定，仓库内无法静态推导 | 需要内核侧 tracing |
| U-06 | UNKNOWN | 内核 CQE 投递与注册 eventfd 的合并行为 | `io_uring_register_eventfd` 只保证通知；合并/延迟由内核决定 | 需要内核实验 |
| U-07 | UNKNOWN | `div` 的实际延迟/周期成本 | 本文只说它是提交路径最贵的 ALU 指令，未测周期数 | 需要微基准 |
| U-08 | UNKNOWN | 产品锁的 contended wait / futex / cacheline bouncing 成本 | 没有 futex/off-CPU/perf lock/TSan 数据；§1.2 的 ns 数字只是原语校准 | 需要产品路径测量 |
| U-09 | UNKNOWN | threadpool 的 futex 唤醒与线程切换成本 | worker 每次完成 1 次 `write(eventfd)` + 1 次 `notify`；未量化调度延迟 | 需要调度/唤醒 tracing |
| U-10 | UNKNOWN | `AsyncIoContext` move-while-driving 的实际后果 | F5 从代码推理（未检查 `drive_active_` + driver 持旧对象地址），未用 TSan/最小复现验证；该路径需违反 THREAD-01 前提 | 需要并发误用测试 |
| U-11 | UNKNOWN | F9 `Request::cancel()` 与 `retire_execution_resources` 的竞争窗口 | 依赖“结算前必已发布”不变量论证当前不可达；未用并发测试证明 | 需要 cancel 竞争测试 |
| U-12 | UNKNOWN | fio 异常退出/信号路径是否总在 close 前排空 | 只读了 `backend.c:254-255` 的正常 `do_io` 退出；未穷举所有错误/信号退出，也未验证 `cleanup_io_u` 对在飞 io_u 的处理 | 需要 fio 异常路径审计/故障注入 |
| U-13 | UNKNOWN | 同一 `thread_data` 上不同 `fio_file` 对象打开同一路径/同一 fd 号时，`sluice_close_file` 的 `slot.u->file == f` 与 `state->files` 的 fd 键是否失配 | 未找到 fio 允许该用法的证据，也未穷举 fio 文件复用模式 | 需要 fio 文件生命周期审计 |
| U-14 | UNKNOWN | `prepared_ops_` 无锁写与持锁读的 happens-before 是否在所有路径成立 | 按 `work_mtx_`/`dispatch_mtx_` 的 push/pop 建立 HB 论证；未用 TSan 验证，依赖“push 前写、pop 后读”顺序不被后续修改破坏 | 需要 TSan / 代码不变量测试 |
| U-15 | UNKNOWN | `AsyncStats` 并发写（`canceled_ops`）的 UB 风险 | 字段非 atomic；并发 `Request::cancel` 与 CQ 路径可在不同锁下写；fio adapter 不传 stats，当前路径不触发 | 多线程 product API 需要文档化同步或 atomic |
| U-16 | UNKNOWN | `uring_backend.cpp:433/436` 的 `have_ring_`/`fatal_error_` 无锁读是否与 poll/poison 合法并发 | writer 在 `dispatch_mtx_` 下；submit 读不持锁；未用 TSan 验证；后续 dispatch_after_accept 会在锁内重查 | 需要 TSan / 并发 submit+poll 测试 |
| U-17 | UNKNOWN | `async_io_context.cpp:480` 的 `execution_closed_` 无锁读 | 写者在 `access_mtx_` 下；读依赖单一 owner drive 纪律；未用 sanitizer 验证 | 需要 THREAD-01 违约测试 |
| U-18 | UNKNOWN | `RequestScope` 跨线程使用的安全性 | scope 槽表/free list/resolve/take 全部无锁；THREAD-01 只承诺 context 级他线程 submit，不承诺 scope 级 | 其他宿主跨线程使用同一 scope 会有数据竞争 |
| U-19 | UNKNOWN | `RequestCore::acquire_execution` 的设计意图 | 全仓 `grep -rn acquire_execution` 仅命中声明/定义，无调用者；`execution_refs` 实际只有 0/1 两态 | 需 owner 确认是遗留接口还是长期设计 |
| U-20 | UNKNOWN | `RequestScope::finish()` 忽略 `cancel_then_drain` 是否是产品意图 | 源码 `settle_and_release_(false)`；测试 `S:tests/request_scope_test.cpp:1126-1135` 钉住“explicit finish under cancel_then_drain settles naturally instead of being canceled”；头文件未写 | 需 owner 确认；若意图是应用策略，adapter 的 `drain` 会让 `cancel_then_drain` 永远无效 |
| U-21 | UNKNOWN | io_uring “cancel 成功必伴随目标 op 的 -ECANCELED CQE”这一内核协议假设 | 代码在 `uring_completion.cpp:99-143` 依赖它才能让 route 退休；本仓无内核侧验证 | 需要内核实验 |
| U-22 | UNKNOWN | 具体一次运行的 `iodepth_batch_complete_min/max` | 默认 B=1 已核实；运行器/命令行可覆盖 | 需要看实际 fio 命令行/作业文件 |
| U-23 | UNKNOWN | `sluice_getevents` 尾部 drain 的精确批量 | `ceil(N/B)` + O(1) drain；drain 一次可批量，故总数略小于 N | 需要运行时计数 |
| U-24 | UNKNOWN | owed / zero-op / cancel 路径的完整成本 | 本文按普通 read/write 稳态计量；owed 扫描 O(C)+每项 1 mutex，zero-op/cancel 另走 control | 需要专门路径审计 |
| U-25 | UNKNOWN | ThreadPool `work_mtx_` 与 core mutex 的精确嵌套（`0xc980` 附近） | objdump 在 `0xc980` 附近遇到反汇编错位（`.byte 0xe8`），未逐指令确认 `claim_execution` 是否仍在 `work_mtx_` 临界区；core 方法自身 1L/1U 已逐方法核验 | 需要换反汇编工具/边界重解 |
| U-26 | UNKNOWN | 本次 `.so` 是否与当前源码逐字节可重建 | 只核到 revision、SDK `.ready`、`ar t` 与 `.text`；未重新构建 | 需要可复现构建 |
| U-27 | UNKNOWN | 既有测试二进制是否与 HEAD 源码逐字节对应 | 二进制时间戳 2026-11-12 21:42，`src/include/tests` 自 `27d907d`（21:04）后无 diff；但未重建 | 需要重新构建后重跑 |
| N-01 | NOT_IDENTIFIED | `ThreadPoolBackend::queue_mtx_` / `queue_mtx` | 源码与 adapter 均无命中；线程池队列由 `work_mtx_` 保护，uring 由 `dispatch_mtx_` 保护 | 审计对象中不存在；不要按独立 queue 锁建模 |
| N-02 | NOT_IDENTIFIED | `submit_request<Op>` 符号 | 模板被内联，`nm` 无符号 | 按调用点计 1 次；不能按独立函数计 |
| N-03 | NOT_IDENTIFIED | `RequestScope::resolve_` / `take_` / `commit_` 符号 | 模板全内联，`nm` 无符号 | 按内联代码计 |
| N-04 | NOT_IDENTIFIED | `ProgressSource::signal` 符号 | 全内联，无独立符号 | 按 `write@plt` 调用点计 |
| N-05 | NOT_IDENTIFIED | `io_uring_prep_*` / `io_uring_get_sqe` 动态导入 | liburing 头内 `static inline`，`nm -D` 无这些符号 | 0 次动态调用；按内联指令计 |
| N-06 | NOT_IDENTIFIED | `RequestCore::consume_public_result` 独立调用点 | 符号 `0x15560` 存在但调用点 0；实际走内联进 `RequestBridge::consume` | 计数按 `take` 次数 T |
| N-07 | NOT_IDENTIFIED | `UringAsyncBackend::deliver_event` / `ThreadPoolBackend::deliver_event` 调用点 | 符号存在但调用点 0；被内联进 `publish_one` / owed sweep | 计数按内联路径 |
| N-08 | NOT_IDENTIFIED | `ReferenceReadySink::on_ready` 的实际间接调用 | 默认 sink 的 vtable 槽与 `0xd650` 比较相等，直接跳过 call | 本构建 on_ready 实际 0 次调用 |
| N-09 | NOT_IDENTIFIED | `RequestCore::acquire_execution` 调用者 | 仅声明/定义，无调用者 | `execution_refs` 实际 0/1 两态 |
| N-10 | NOT_IDENTIFIED | 集中式锁序文档 | 仓库未找到；THREAD-02 要求记录但当前只能从调用链推断 | 不能把推断顺序当成文档化契约 |
| N-11 | NOT_IDENTIFIED | Sluice 库 TU 的 DWARF 行号表 | `.so` 只有 5 个 compile unit，均为 adapter 源码 | Sluice 内部 inline 结论只能靠符号+调用点，不能逐指令映射源码行 |
| N-12 | NOT_IDENTIFIED | fio 二进制中 `ops->queue/getevents/event` 的间接目标 | 未反汇编 fio 二进制；只按源码阅读 | 需对 fio 构建产物反汇编/动态追踪 |
| N-13 | NOT_IDENTIFIED | `fio-sluice-ring.so`（stripped）的符号级调用图 | 只反汇编了 `-sym`；stripped 版仅用于 `.text` 一致性比较 | 符号结论对 `.text` 成立，但不声称 stripped 版有符号 |
| N-14 | NOT_IDENTIFIED | ThreadPool 侧的 `queue_mtx_` 获取点 | 同 N-01；不存在 | 模型应使用 `work_mtx_` |
| N-15 | NOT_IDENTIFIED | `submit_request` 的独立栈帧 | 模板内联；`submit_read` 体内直接出现 `reserve/accept/dispatch` 调用 | 不能把模板当独立函数计栈/调用 |

---

## 8. 本次合并实际执行的验证

以下命令在本次合并中执行；输出摘要按原样记录。除这些命令外，没有新的构建或性能测量。

### 8.1 基线

```text
$ git -C /home/jnhu/Source/Sluice rev-parse HEAD
5d94f233967c3aef993eb19ba9e3252c532b0951

$ git -C /home/jnhu/Source/Sluice status --porcelain
?? docs/evidence/621-progress-wait-path-20261118/
?? docs/evidence/621-round2-R1/
?? docs/evidence/e5-fullpath-machine-work/

$ cat SLUICE_REVISION
5d94f233967c3aef993eb19ba9e3252c532b0951

$ cat build/ring-pic/sdk/.ready
5d94f233967c3aef993eb19ba9e3252c532b0951

$ git -C build/fio/upstream describe --tags --exact-match
fio-3.40

$ git -C build/fio/upstream rev-parse HEAD
ff930c4653ae3952d6b09ab3ec89671aeabf2cbe
```

### 8.2 `.text` 一致性

```text
$ mkdir -p /tmp/audit-merge-text
$ objcopy --dump-section .text=/tmp/audit-merge-text/ring.text \
    build/linux/x86_64/release/fio-sluice-ring.so
$ objcopy --dump-section .text=/tmp/audit-merge-text/ring-sym.text \
    build/linux/x86_64/release/fio-sluice-ring-sym.so
$ sha256sum /tmp/audit-merge-text/ring.text /tmp/audit-merge-text/ring-sym.text
3d3b9ec449f5c982e243f360cfaf75a747b1226884a06a2b736f5db692c49ad6  /tmp/audit-merge-text/ring.text
3d3b9ec449f5c982e243f360cfaf75a747b1226884a06a2b736f5db692c49ad6  /tmp/audit-merge-text/ring-sym.text
$ cmp /tmp/audit-merge-text/ring.text /tmp/audit-merge-text/ring-sym.text
IDENTICAL
$ readelf -S -W build/linux/x86_64/release/fio-sluice-ring-sym.so | grep '\.text '
  [ 4] .text  PROGBITS  0000000000000780 000780 0158a1 00  AX  0  0 64
$ readelf -n build/linux/x86_64/release/fio-sluice-ring-sym.so | grep -A1 'Build ID'
    Build ID: 738d6167a7f494122c78d9e1dab2801941e5d442
```

### 8.3 反汇编统计

```text
$ objdump -d --no-show-raw-insn build/linux/x86_64/release/fio-sluice-ring-sym.so \
    > /tmp/audit-merge-text/dis.txt
$ grep -cE '^\s*[0-9a-f]+:\s+lock ' /tmp/audit-merge-text/dis.txt
14
$ grep -cE '(call|jmp).*<pthread_mutex_lock@plt>' /tmp/audit-merge-text/dis.txt
155
$ grep -cE '(call|jmp).*<pthread_mutex_unlock@plt>' /tmp/audit-merge-text/dis.txt
199
$ readelf --debug-dump=info build/linux/x86_64/release/fio-sluice-ring-sym.so \
    | grep -c 'DW_TAG_compile_unit'
5
$ nm -D build/linux/x86_64/release/fio-sluice-ring-sym.so | grep -i uring
                 U io_uring_free_probe@LIBURING_2.0
                 U __io_uring_get_cqe@LIBURING_2.0
                 U io_uring_get_events@LIBURING_2.3
                 U io_uring_get_probe_ring@LIBURING_2.0
                 U io_uring_peek_batch_cqe@LIBURING_2.0
                 U io_uring_queue_exit@LIBURING_2.0
                 U io_uring_queue_init@LIBURING_2.0
                 U io_uring_register_eventfd@LIBURING_2.0
                 U io_uring_submit@LIBURING_2.0
                 U io_uring_submit_and_wait@LIBURING_2.0
                 U io_uring_unregister_eventfd@LIBURING_2.0
```

### 8.4 既有 release 测试（未重建）

实际命令：

```text
$ cd /home/jnhu/Source/Sluice/build/linux/x86_64/release
$ for t in submit_concurrency_test submit_concurrency_uring_test \
    request_core_publication_test request_core_protocol_test \
    threadpool_progress_race_test uring_progress_race_test \
    request_scope_test request_core_ownership_test \
    shutdown_lifecycle_test shutdown_lifecycle_uring_test; do
    echo "=== $t"
    timeout 60 "./$t" >/tmp/audit-merge-text/$t.out 2>&1
    rc=$?
    echo "EXIT=$rc"
    tail -3 /tmp/audit-merge-text/$t.out
  done
```

本次实际运行与结果：

| 二进制 | 退出码 | 末尾输出 |
|---|---|---|
| `submit_concurrency_test` | 0 | `submit concurrency oracle passed` |
| `submit_concurrency_uring_test` | 0 | `submit concurrency oracle passed` |
| `request_core_publication_test` | 0 | `all 11 request core publication tests passed` |
| `request_core_protocol_test` | 0 | `all 46 request core protocol tests passed` |
| `threadpool_progress_race_test` | 0 | `all 22 threadpool progress race tests passed` |
| `uring_progress_race_test` | 0 | `all 23 uring progress race tests passed` |
| `request_scope_test` | 0 | `25 of 25 request scope tests passed`（另含预期的 fail-fast/terminate 诊断） |
| `request_core_ownership_test` | 0 | `11 of 11 request core ownership tests passed, 0 skipped` |
| `shutdown_lifecycle_test` | 0 | 尾部 PASS（含 `withheld_completion_fails_by_deadline_and_late_completion_passes` 等） |
| `shutdown_lifecycle_uring_test` | 0 | 尾部 PASS（含 `poison_with_in_flight_cqe_reports_unresolved_health` 等） |

补充证据：

```text
$ git -C /home/jnhu/Source/Sluice diff --name-only 27d907d6..HEAD -- src include tests
（空）

$ stat -c '%y %n' /home/jnhu/Source/Sluice/build/linux/x86_64/release/request_scope_test
2026-11-12 21:42:33.087910154 +0800 /home/jnhu/Source/Sluice/build/linux/x86_64/release/request_scope_test
```

### 8.5 fio 默认 batch

```text
$ nl -ba build/fio/upstream/options.c | sed -n '2296,2325p'
2299  .name = "iodepth_batch_complete_min"
2309  .def  = "1"
2314  .name = "iodepth_batch_complete_max"
（无 .def）

$ nl -ba build/fio/upstream/init.c | sed -n '764,773p'
770  if (o->iodepth_batch_complete_min > o->iodepth_batch_complete_max)
771      o->iodepth_batch_complete_max = o->iodepth_batch_complete_min;

$ grep -rnE 'iodepth_batch|batch_complete' tools adapters workloads
（无结果）
```

---

## 9. 适用边界

1. 本文的 per-I/O 计数默认 **B=min=max=1**、一次非零长度 read、无 cancel、无 observer attach、正常完成。覆盖 batch 或 cancel 时必须按公式重算。
2. “每 I/O 9 + T 次 core 锁”里的 T 是 **getevents 批次的 take 次数**，不是完成数；B=1 时完成数≈1，但 pending 槽会被重复 take。把 10 当成常量会低估 harvest 成本。
3. 所有 `D:` 证据来自 `fio-sluice-ring-sym.so` 的 `.text`；随包 `fio-sluice-ring.so` 的 `.text` 与其逐字节相同。整文件哈希不同不影响该结论。
4. 本文没有把 lock-graph 的原语校准（7.8/6.1/18.8/3.7/73.4/345.3 ns）当作产品锁等待时间；它们是 `std::mutex`/`std::atomic` 的微基准，只说明量级。
5. 本文没有重新构建 Sluice 或 adapter，也没有运行 fio 作业；测试重跑使用的是 2026-11-12 21:42 的既有 release 二进制。

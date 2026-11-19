# Phase 0 仓库现实审计（fio 外部引擎集成前）

审计日期：2026-11-19。审计人角色：只读审计（本文件是本次审计产出的唯一写入物）。
方法：所有结论均来自本机执行的命令或逐行阅读的源码；每条给出 `path:line` 或命令与输出。
凡未执行的检查，在 §8 明确列为"未核实"。

---

## 1. Git 状态

### 1.1 主开发仓库 `/home/jnhu/Source/sluice-workload-bench`

执行的命令（均成功，`git fetch origin --prune` 退出码 0、无输出）：

```
git fetch origin --prune        # exit 0
git rev-parse HEAD              # 2abda4dfa4c87c3f8a7249b712ad0c7b82e4a7cd
git status                      # On branch feat/fio-sluice-engine; nothing to commit, working tree clean
git log -5 --format='%H %d %s'
```

`log -5` 实际输出（仓库只有 2 个提交）：

```
2abda4dfa4c87c3f8a7249b712ad0c7b82e4a7cd (HEAD -> feat/fio-sluice-engine, origin/main, origin/HEAD, main) feat: add deterministic Sluice workload benchmark
a27e5b1e596915aa6a1a673d7068130ef3a08957 docs: initialize workload benchmark repository
```

**与任务预期的偏差（如实记录）**：任务说"当前 main"；实际当前分支是
`feat/fio-sluice-engine`（已被预先创建并检出），指向与 `main`、`origin/main` 完全相同的
提交 `2abda4d`，工作区干净。这恰好满足硬约束 1（只在 feat/fio-sluice-engine 分支开发），
本次审计文档即写入该分支。`git fetch --prune` 没有裁剪任何远端分支。

仓库根内容：`Makefile`、`README.md`、`SLUICE_REVISION`、`src/`（5 个文件）、`tools/`
（`build_sdk.sh`、`check.py`、`run_matrix.py` 三个文件）、`.gitignore`
（内容为 `/build/`、`/results/`、`__pycache__/`）。`results/` 目录不存在。
`build/` 被忽略，当前含 `build/fio/upstream/`（fio 上游浅克隆）与本次审计构建出的
`build/noring/`（见 §4.3）。

### 1.2 Sluice 只读 checkout `/home/jnhu/Source/Sluice`

执行的命令：

```
git fetch origin --prune        # exit 0
git rev-parse HEAD              # 5d94f233967c3aef993eb19ba9e3252c532b0951
git status
git log -5 --format='%H %d %s'
```

- HEAD = `5d94f233967c3aef993eb19ba9e3252c532b0951`，分支 `master`，与 `origin/master` 一致。
  **`SLUICE_REVISION` 核对通过**：bench 仓库 `SLUICE_REVISION` 文件内容恰为
  `5d94f233967c3aef993eb19ba9e3252c532b0951`（41 字符，含换行），两者相等。
- `git status`：**工作区不是完全干净**——有 3 个未跟踪目录（纯 evidence 产物，无已跟踪文件改动）：
  `docs/evidence/621-progress-wait-path-20261118/`、`docs/evidence/621-round2-R1/`、
  `docs/evidence/e5-fullpath-machine-work/`。如实记录；该仓库本次仅被读取（fetch 只写
  `.git` 的远端引用，未触碰工作区）。
- `log -5`：HEAD 是 `Merge pull request #635 from jnhu76/product/621-progress-drain-single-read`，
  其下依次为 f707ff39（621 evidence index）、86955da7（621 evidence index and PR body
  correction）、092e88cb（621 progress-drain ledger row）、27d907d6（async: remove
  guaranteed-EAGAIN drain probe read #621）。

### 1.3 fio 上游克隆（bench 仓库内，被 .gitignore 忽略）

`build/fio/upstream/` 是 `https://github.com/axboe/fio.git` 的浅克隆（grafted），
HEAD = `ff930c4653ae3952d6b09ab3ec89671aeabf2cbe`，tag `fio-3.40`（`git describe --tags`
输出 `fio-3.40`）。与系统 fio 同版本（§6）。

---

## 2. 现有 benchmark 能力清单核对（逐条，读码确认）

核对对象：`src/main.cpp`、`src/runner.cpp`、`src/runner.hpp`、`src/workload.cpp`、
`src/workload.hpp`、`tools/check.py`、`tools/run_matrix.py`、`Makefile`、`README.md`。
逐条结论（"确认"= 源码中真实存在且与 README.md 描述一致）：

| # | 能力 | 结论 | 依据 |
|---|------|------|------|
| 1 | 确定性请求生成 | 确认 | `std::mt19937_64` + 取模拒绝采样（`src/workload.cpp:103-123`）、按权重选尺寸（125-132）、顺序/均匀随机/双区热点偏移（138-154）、closed-loop 或按速率的到达时刻（134-136）、读写洗牌与写区间不相交排布（251-256, 306-311）；FNV 风格 64 位 trace 指纹覆盖 seed、字节数、每请求 op/offset/size/arrival（156-161, 314-324）。同一 seed 生成同一 trace，`run_matrix.py:77-79` 在多臂间强制 trace_id 一致 |
| 2 | POSIX baseline | 确认 | `--backend posix` 走裸 `::pread/::pwrite`（`src/runner.cpp:277-281`），不打开 sluice::File（445-449 只在非 posix 臂打开）；posix/direct 强制 depth 1（`src/workload.cpp:231-234`） |
| 3 | Sluice Direct | 确认 | `--backend direct` 经 `sluice::File::open`（`src/runner.cpp:447-449`）+ `sluice::blocking::read_at/write_at`（272-273），即同步 blocking API（README.md:97 "Sluice Direct is its synchronous API"） |
| 4 | ThreadPool | 确认 | `make_threadpool_context({request_capacity=depth, worker_count=workers})`（`src/runner.cpp:302-305`）；`--workers` 仅 threadpool 臂可用（`src/workload.cpp:235-237`）；无隐藏 fallback（`include/sluice/async/threadpool_backend.hpp:14-15` 注明 no fallback） |
| 5 | io_uring | 确认 | `make_uring_context`（`src/runner.cpp:307-313`）；SDK 无 liburing profile 时映射 `not_supported` → `BackendUnavailable`（309-311）；运行时 ring 初始化失败（消息前缀 `sluice::async::UringAsyncBackend: io_uring `）同样转 unavailable（314-320）→ main 打印 JSON reason 并 **exit 77**（`src/main.cpp:156-160`），与 README.md:22-26 一致；`run_matrix.py:70-75` 把 77 记为 `unavailable` 行 |
| 6 | request depth | 确认 | `--depth` 1..1024（`src/workload.cpp:218-219`）；异步臂预建 depth 个槽（`src/runner.cpp:336-343`），`RequestScope scope(context, options.depth, drain)`（345），提交前检查空槽（351-354），`peak_inflight_requests` 上报（380） |
| 7 | max in-flight bytes | 确认 | `--max-inflight-bytes` 0..1GiB（`src/workload.cpp:222-223`），0=不启用；准入检查 `request.size <= byte_limit - in_flight_bytes`（`src/runner.cpp:351-354`），小于最大请求尺寸直接拒绝（`src/workload.cpp:301-304`，check.py:87 验证该报错）；`peak_inflight_bytes`、`blocked_admission_checks` 上报（`src/runner.cpp:380-384`） |
| 8 | data oracle | 确认 | 两遍执行：先 untimed 遍历逐字节校验每个读（`src/runner.cpp:453-461` 的 `for (const bool timed : {false, true})`；非计时分支在 `Observation::completed` 231-235 用独立生成的 pattern 对比）；每遍前 `fixture.prepare` 重建数据（112-121），每遍后 `check_final_bytes` 用 POSIX `pread` 复核读区+每个写 extent（123-149）；读 pattern = `mix(seed + offset/8)`（54-70），写 payload 首字嵌请求序号+1（146, 261-265）；短读/短写抛错（47-52）。失败→异常→exit 1（`src/main.cpp:161-164`）；成功记录硬编码 `"byte_oracle":"passed"`（`src/main.cpp:55`，只在 oracle 全过后才会打印） |
| 9 | JSONL 报告 | 确认 | 单次调用输出单行 JSON（schema=1：workload/execution/timing/classes 段，`src/main.cpp:48-140`）；`run_matrix.py` 逐行写 JSONL，附 `case/trial/arguments/runner_sha256`（`tools/run_matrix.py:66-82`），`--repeat` 交替正反序（68） |
| 10 | 可复现 trace | 确认 | trace_id 进 JSON（`src/main.cpp:75-77`）；矩阵跨臂校验 trace 一致（`tools/run_matrix.py:77-79`）；SDK revision、profile、编译器、内核、文件系统 magic、mincore 驻留比例均进记录（`src/main.cpp:60-73`；`src/runner.cpp:151-180`） |

其他 README 声明核对：
- "Invalid options … exit 1"（README.md:24-25）：`src/main.cpp:161-164`，stderr 报错、无 stdout。
- `--help`（README.md:44）：`src/main.cpp:146-149` → `print_help()`（`src/workload.cpp:328-339`），选项全集与 `parse_options`（183-242）一致。
- 矩阵用例（README.md:66-72）：`tools/run_matrix.py:11-18` 的 6 个 CASES 与文字描述一致；async depth 16 的 mixed-size/background-write 额外加 128 KiB byte cap（37-38）。
- 定时口径（README.md:83-93）：计时含提交与完成观察，启动/造数/校验/拆除在计时区间外（`src/runner.cpp:208-219` 的 start/stop 只包住执行循环）；scheduled 延迟从应到达时刻起算（229-230）。

**构建与测试现状**：
- 本次审计执行了 `make SLUICE_SOURCE=/home/jnhu/Source/Sluice JOBS=8 all`（noring profile）——构建成功（SDK 安装 + 二进制链接，见 §4.3），退出码 0。这是构建，不是负载运行。
- **`make check`（`tools/check.py`）未运行**：check.py 会实际执行 benchmark 二进制的负载（tmpfs 上 120 请求的小负载），按硬约束 7"除性能测量阶段外不得运行 benchmark 负载"，Phase 0 不运行，列为未核实项（§8）。
- 构建系统现状：`Makefile`（30 行）是当前唯一构建入口（`make [PROFILE=ring|noring] [check]`），`tools/build_sdk.sh` 用 xmake 构建 Sluice SDK。按用户指令，bench 仓库构建系统后续将迁移到 xmake；本次仅记录现状，未做任何改动。

---

## 3. Sluice 侧事实（只读，HEAD 5d94f23）

### 3.1 AGENTS.md 工作规则（`AGENTS.md:1-196`）

- 权威顺序：v2 root（`docs/explicit-io-contract.md`）→ v2 conformance ledger → 实际 C++/测试；"代码与契约不符是 conformance gap，不是改写契约的许可"（AGENTS.md:15-18）。
- 工程顺序（AGENTS.md:27-38）：先找 requirement ID 与独立 oracle，选最小机制，测试先行/同行，更新 ledger；"不得无 root 修订引入新的兼容层或运行时权威"。
- 测试纪律（AGENTS.md:42-87）：测试是行为/不变式证据，不是实现的镜像；不得用 sleep/重试膨胀把失败测试刷绿。
- 架构纪律（AGENTS.md:89-98）：保持 direct 执行不依赖 RequestCore/Scheduler；"Do not add hidden blocking fallback"；不新增投机性公共框架。
- 机制正当性门（AGENTS.md:100-136）：每个新运行时状态/计数器/守护要回答 7 个问题。
- 证据纪律（AGENTS.md:138-149）：架构性 PR 必须引用 root ID、before/after、验证与局限。
- C++ 注释规则（AGENTS.md:151-173）：默认不写解释性注释。
- 性能与仓库卫生（AGENTS.md:175-196）：`profile -> hotspot -> local hypothesis -> isolated change -> benchmark`，无测量热点不开投机优化任务；**未经明确指示不合并**；一次性探针脚本进 `/tmp` 或实验分支，不进 master。

### 3.2 v2 root contract 要点（`docs/explicit-io-contract.md`，revision v2-r1）

文件头（1-11 行）声明：v2-r1 已采纳，是唯一 normative root；实现基线 `b870a7c0`；conformance 由 `docs/roadmap/v2-conformance.md` 跟踪。与 fio 引擎集成直接相关的条款：

- **INV-02（328-345）**：submission 不得等待容量、不得在提交线程执行阻塞文件系统调用、不得等结果；操作可以在 submit 返回前完成，但责任仍随 Request 交付。→ fio 引擎的 `queue()` 回调可直接映射 Sluice submit。
- **REQ-03/04/05（629-691）**：生命周期 Free→Reserved→Accepted→Executing→Chosen→Published→Reclaimable→Free；publication 是 release 边界，**borrow 到 publication 才结束**（缓冲区/File 只有在获得 publication 之后才能复用/关闭）；槽回收五条件。→ fio io_u 缓冲区在 event 返回前由 fio 持有，天然满足 borrow。
- **PROG-01（828-863）**：一个 context 一个固定 progress owner；只有 owner 驱动 poll/wait/driving shutdown；其他线程可 submit/cancel/request_stop（THREAD-01, 1003-1021）。progress pass 有界非阻塞并如实报告 residue。
- **PROG-02/03（865-935）**：no-lost-wake 契约；外部事件循环可用 pollable 通知 fd（`progress_notification_fd()`，借用语义，host 不得 close/独立 drain）。fio 引擎不需要外部 loop，用不到该 fd。
- **PROG-04（937-949）**：等待结果区分 progress / control_interrupted / deadline_expired / health_failure；deadline 到期**不取消** I/O；控制中断是粘性的，需 owner `acknowledge_progress_control()`。
- **LIFE-01/02（953-985）**：borrow 契约；`native_handle()` 返回借用不转移所有权；外来代码不得关闭它。→ fio 自己打开文件、把 fd 包进 `NativeFileRef` 是公共 interop 路径。
- **SHUT-01..04（1204-1295）**：shutdown 状态机与次序；只有 owner 驱动 shutdown；析构前置条件（无存活公共绑定/宿主注册）。
- **BACKEND-01..04（1077-1161）**：backend seam 是内部接口，**无插件 ABI、不支持第三方 backend**；v2 恰好两个 backend（ThreadPool、io_uring）。→ fio 引擎绝不能试图实现 Sluice backend，只能作为**消费者**使用公共 API。
- **W-04 / §16（248-257, 1163-1200）**：stackful host 已被硬移除；W-02 有界管线经 owned-driver RequestScope 保留。→ 与硬约束 2（不新增统一 Runtime/Scheduler/polling thread）同向。

### 3.3 `include/sluice/async/` 公共类型（已安装头，见 §4.3）

以下均为**公共安装面**（`xmake/libraries.lua:47-51` 的显式 allowlist）：

- `async_io_context.hpp`
  - `NativeFileRef`（30-38）：`{int fd, FileAccess access}`，可由 `sluice::File` 构造，也可 **`NativeFileRef(int native_fd, FileAccess)` 直接包外来 fd**（33-34）——fio 引擎对接 fio 打开的 fd 的公共入口。
  - 操作描述符 `ReadOp/WriteOp/SyncDataOp/SyncAllOp/FileInfoOp/SizeOp`（40-63），均为 `{NativeFileRef, buffer ptr, len, offset}` 形状。
  - `ProgressPass`（68-78）：一次有界非阻塞推进的后置事实（completed / immediate_work_remains / accepted_work_remains / dispatch_retry_remains / health_failed）。
  - `AsyncIoContext`（113-351）：`submit_read/submit_write/submit_sync_data/submit_sync_all/submit_file_info/submit_size`（124-129）、`cancel(RequestId)`（131）、`lookup(RequestId) const`（133）、`poll()`（135）、**`poll_progress()`**（152）、**`claim_progress_owner()`**（158）、**`wait_one()` / `wait_one(ns)`**（180-182）、`ProgressWaitOutcome{kind: progress|control_interrupted|deadline_expired|health_failure, completed}`（169-178）、`interrupt_progress_waiters/acknowledge_progress_control`（188-195）、外部通知借用三件套 `progress_notification_fd/acknowledge_progress_notification/detach_progress_host`（207-218）、shutdown 家族 `close_admission/request_stop/shutdown` + `admission_open/execution_closed`（221-227）、observer 家族（229-273）、`outstanding()`（273）。move-only，析构即受 SHUT-04 约束。
  - `ProgressOwner`（97-111）：独占驱动权的释放句柄；`RequestScope` 构造时内部认领（`request_scope.hpp:90`）。
- `request.hpp`
  - `RequestId`（29-51）、`CancelDisposition`（53-59）、`RequestReadiness{empty,pending,ready}`（61）。
  - `RequestObservation<T>{readiness, Result<T>, EffectReport}`（63-70）。
  - `Request<T>`（101-214）：move-only RAII；`ready()`（130）、**`try_result()` 非破坏探测**（134-144）、**`take_result()` 取走或仍 pending**（146-159）、`discard()`（161）、`cancel()`（163-168）；销毁非终态 Request 是 fail-fast 违约（HANDLE-02）。终态负载：读/写得 `std::size_t` 确认字节数。
- `request_scope.hpp`
  - `ScopeTicket<T>`（24-38，本质是 RequestId 的轻量票根）、`ScopeCleanupPolicy{drain, cancel_then_drain}`（19）、`ScopeWaitStatus{ready,timeout,interrupted,health_failure}`（21）。
  - `RequestScope`（40-208）：构造即认领 progress owner；`submit_read/submit_write/submit_sync_data/submit_sync_all`（51-54）、**`take(ticket)`**（56-57，pending 不会误取，ready 即回收槽）、`wait_for(ticket, ns)`（59-62）、`finish()`（64）；容量先于提交预留（97, 114-129）。
- `threadpool_backend.hpp`：`ThreadPoolConfig{request_capacity, worker_count}` + `make_threadpool_context`（16-17），失败抛异常、**无 fallback**（14-15）。
- `uring_backend.hpp`：`UringConfig{request_capacity, queue_depth}` + `make_uring_context` 返回 `Result`（19-20）：无 liburing profile → `not_supported`；有 profile 但 ring 建立失败 → 抛异常；**从不回退到别的执行**。

支撑头（core 安装面，`xmake/libraries.lua:17-22`）：`sluice/effect.hpp`、`error.hpp`、`file_resource.hpp`（`File`、`FileAccess/FileExistence/FileInitialContents/FileOpen`、`FileInfo/FileSize`，`file_resource.hpp:12-95`）、`measurement.hpp`（`AsyncStats`, 83-97）、`result.hpp`、`blocking/file.hpp`（direct 同步 API：`read_at/write_at/…/write_all_at`, 14-59）。

### 3.4 SDK 安装物（`tools/build_sdk.sh` 产出路径）

本次实际执行 `make SLUICE_SOURCE=/home/jnhu/Source/Sluice JOBS=8 all`（等价于
`bash tools/build_sdk.sh noring 8` 再编译链接 bench），构建成功（exit 0）。`build_sdk.sh`
逻辑（`tools/build_sdk.sh:1-40`）：在 `build/<profile>/sluice` 建独立 git 库、按 revision
浅取（本地 `SLUICE_SOURCE` 优先，否则 GitHub）、detach 到 `SLUICE_REVISION`、检查无 tracked
改动、`xmake f -m release --liburing=y|n`、`xmake build sluice_async`、
`xmake install -o build/<profile>/sdk sluice_core sluice_async`、写入 `.ready`。

实测产物（`find build/noring/sdk -type f`）：

```
build/noring/sdk/include/sluice/async/{async_io_context,request,request_scope,threadpool_backend,uring_backend}.hpp
build/noring/sdk/include/sluice/blocking/file.hpp
build/noring/sdk/include/sluice/{effect,error,file_resource,measurement,result}.hpp
build/noring/sdk/lib/libsluice_async.a
build/noring/sdk/lib/libsluice_core.a
build/noring/sdk/.ready            # 内容 = 5d94f233967c3aef993eb19ba9e3252c532b0951
```

安装头集合与 `xmake/libraries.lua:17-22,47-51` 的 allowlist 完全一致（无任何 detail 头被安装）。
`nm --defined-only` 确认两库符号齐备（示例，全部在静态库 T 符号中）：
`sluice::async::AsyncIoContext::submit_read/submit_write/poll/poll_progress/claim_progress_owner/wait_one()/wait_one(ns)/shutdown/cancel/lookup`、
`sluice::async::{make_threadpool_context,make_uring_context}`（分别在 threadpool_backend.cpp.o / uring_backend.cpp.o）、
`sluice::async::RequestScope::{submit_read,submit_write,take,wait_for,finish}`、
`sluice::File::{open,close,~File}`、`sluice::blocking::{read_at,write_at,…}`。
noring profile 下 `make_uring_context` 符号同样存在（运行时返回 `not_supported`，由
`src/runner.cpp:309-311` 显式处理）。ring profile 的实际构建未在本次运行（§8）。

---

## 4. fio 外部引擎实际可用的 Sluice 公共接口面

### 4.1 fio 侧接口（engine ABI，来自 build/fio/upstream @ fio-3.40）

- 外部引擎装载：`--ioengine=external:<path.so>`（`ioengines.c:186-209` 用
  `td->o.ioengine_so_path` dlopen，`RTLD_LAZY`），符号解析顺序：`dlsym(<引擎名>)` →
  `dlsym("ioengine")` → `dlsym("get_ioengine")` C++ 工厂回调（`ioengines.c:140-159`，
  `get_ioengine_t` 定义在 `ioengines.h:132`）。系统 fio 实测接受该语法并尝试 dlopen
  （对不存在路径报 "engine … not loadable"，本次只做了这一无 I/O 探测）。
- ABI 版本：`FIO_IOOPS_VERSION 37`（`ioengines.h:12`）。
- 回调面：`struct ioengine_ops`（`ioengines.h:29-75`）——异步引擎核心是
  `queue()`（返回 `FIO_Q_COMPLETED/FIO_Q_QUEUED/FIO_BUSY`，`ioengines.h:23-27`）、
  `commit()`、`getevents(td, min, max, timespec)`、`event(td, idx)`，另需
  `init/cleanup/open_file/close_file/get_file_size`，可选 `cancel/prep/io_u_init/io_u_free` 等。
- 引擎标志位（`ioengines.h:77-127`）：异步引擎不带 `FIO_SYNCIO`；可用
  `FIO_ASYNCIO_SYNC_TRIM`、`FIO_MEMALIGN` 等。

### 4.2 Sluice 公共接口面 → 引擎映射（全部走已安装头 + 静态库，零改动 Sluice）

| fio 引擎需要 | Sluice 公共接口（头文件:行） | 说明 |
|---|---|---|
| 打开/关闭文件 | fio 自己的 `open_file/close_file`；fd 包 `NativeFileRef(int, FileAccess)`（async_io_context.hpp:33-34） | LIFE-02：fd 仍归 fio，Sluice 只借用；引擎绝不能替 fio close |
| 提交读/写 | `AsyncIoContext::submit_read(ReadOp)/submit_write(WriteOp)` 或 `RequestScope::submit_read/submit_write`（async_io_context.hpp:124-125; request_scope.hpp:51-52） | 满足 fio `queue()` 非阻塞要求（INV-02） |
| 完成观察（getevents） | `RequestScope::take(ticket)` 非破坏轮询（pending 保持不动，request_scope.hpp:56,132-143）+ `AsyncIoContext::poll_progress()` 有界推进（async_io_context.hpp:152）+ `wait_one(ns)` 有界等待（180-182） | bench 自身已是该模式的活例（`src/runner.cpp:386-431`：take 扫描 + 无进展时 `wait_one` 直到 deadline） |
| 等待语义/健康 | `ProgressWaitOutcome::Kind`（169-178）；`deadline_expired` 不取消 I/O | getevents 的 timespec 超时可直接映射 |
| 取消 | `Request::cancel()` / `AsyncIoContext::cancel(RequestId)`（request.hpp:163-168; async_io_context.hpp:131），`CancelDisposition` 明示强度 | ThreadPool 不能中断进行中的 syscall（contract BACKEND-03），引擎必须如实转发 |
| 并发模型 | 每个作业线程一个 context + `RequestScope` 构造即认领该线程为 progress owner（request_scope.hpp:90; THREAD-01） | fio 每个作业单线程调 queue/getevents，恰好是一 owner 一线程，无需任何额外线程 |
| 收尾 | `RequestScope::finish()`（drain/cancel_then_drain）或 `close_admission + request_stop + shutdown`（async_io_context.hpp:221-223） | cleanup 阶段显式排空，禁止留悬空绑定 |
| 构造失败 | `make_uring_context` → `not_supported`/异常；`make_threadpool_context` → 异常（两工厂头 14-20） | 引擎 init 失败必须让 fio 作业失败，**禁止任何 hidden POSIX fallback**（与本仓库硬约束 4 一致） |

**完成观察能力结论：公共 API 没有缺口。** fio 异步引擎所需的"提交非阻塞、非阻塞探测完成、
有界等待完成、取消、显式关闭"在已安装公共面上全部存在（上表）。无需给 Sluice 增加任何
API、线程或 callback；引擎把 `io_u*` → `ScopeTicket` 的映射、`getevents` 的 take+poll+wait
循环、短 I/O → `io_u->resid` 的换算、错误码 → `io_u->error` 的换算全部封装在**本仓库内**
的引擎 .so 里。这与硬约束 2/3（不改 v2 API、异步不伪装同步、不绕过 lifetime/publication/
progress owner/shutdown）一致。

集成注意（记录给后续实现者，非 Sluice 缺陷）：
1. 引擎 .so 是 C++（链 libstdc++/libpthread/liburing 按需），必须自带运行时依赖；fio 是 C 程序，dlopen 拉起 C++ .so 可行（fio 源码注释亦点名 C++ 引擎走 `get_ioengine`，`ioengines.c:148-158`）。
2. `io_u` 缓冲区在事件归还前由 fio 持有，与 LIFE-01 borrow 契约吻合；事件归还必须发生在 `take_result()` 取得 publication 之后（REQ-04）。
3. Sluice 允许短计数（SEM-05）；fio 侧要用 `io_u->resid` 表达，不得静默补齐或重试（否则等于 hidden fallback/改数据）。
4. Sluice `wait_one` 的 park 边界与 fio `getevents` 的 timespec 语义不同（deadline 不取消 I/O），循环需以 min_events 满足或超时退出为准，照抄 `src/runner.cpp:386-431` 的骨架即可。

### 4.3 明确不属于可用面的东西（禁止使用）

- `include/sluice/async/detail/**`、`include/sluice/detail/**`（backend seam、RequestCore、ProgressSource 等）：**不安装、不稳定、内部接口**（BACKEND-01；`xmake/libraries.lua:43-46` 注释明说 backend 类与 seam 留在库 TU 内）。
- 自行实现 Sluice backend / 注册资源 / 第三方插件 ABI：PROD-03 与 BACKEND-01 明确排除。
- `progress_notification_fd()` 一族：为外部事件循环（W-03）设计；fio 引擎用 `poll_progress/wait_one` 即可，不必借用 fd。
- 任何把 ThreadPool 请求伪装成立即完成的同步路径；任何绕过 `RequestScope::finish`/`shutdown` 的提前析构。

---

## 5. 环境事实（全部本次实测）

| 项 | 实测值 | 命令 |
|---|---|---|
| CPU | Intel Xeon E5-2666 v3 @ 2.90GHz，1 socket × 10 core × 2 thread = **20 逻辑核**，1 NUMA node | `lscpu` |
| cpufreq | driver `intel_cpufreq`，governor **`schedutil`**（未钉 performance），min 1200 MHz / max 3500 MHz，采样时 scaling 58% | `cat /sys/devices/system/cpu/cpu0/cpufreq/{scaling_governor,scaling_driver}` |
| 内核 | `7.3.3-200.fc44.x86_64`（Fedora 44） | `uname -r` |
| `/` | `/dev/sda3[/root]` btrfs，`rw,relatime,seclabel,compress=zstd:1,ssd,discard=async,space_cache=v2` | `findmnt /` |
| `/home` | `/dev/sda3[/home]` btrfs，同一物理分区另一子卷，同选项 | `findmnt /home` |
| `/tmp` | tmpfs，`size=32837700k`（≈31.3 GiB），nosuid,nodev | `findmnt /tmp` |
| 其他盘 | sdb（3.6T ntfs，无挂载）、nvme0n1（ntfs，无挂载）——**均不可用于测试写**（硬约束 5） | `lsblk` |
| gcc/g++ | `gcc (GCC) 16.2.1 20261029 (Red Hat 16.2.1-3)` | `gcc --version` |
| Python | `Python 3.14.10` | `python3 --version` |
| xmake | `v3.1.2+HEAD.5d21e9d` | `xmake --version` |
| liburing devel | `liburing-devel-2.13-2.fc44`：`/usr/include/liburing.h` + `/usr/lib64/liburing.so.2.13`（**无 liburing.a**，动态库 only）；`pkg-config --modversion liburing` = 2.13 | `rpm -q`、`pkg-config`、`ls` |
| io_uring 可用性 | `/proc/sys/kernel/io_uring_disabled` = **0**（未禁用） | `cat` |
| fio | `/usr/sbin/fio`，`fio-3.40-3.fc44.x86_64`；`fio --version` = `fio-3.40`；动态依赖仅 `libnuma.so.1, libz.so.1, libm.so.6, libmvec.so.1, libc.so.6`（**未链接 liburing**，尽管 `--enghelp` 列出 io_uring/io_uring_cmd 引擎；运行时该引擎是否可用未验证，见 §8）；接受 `--ioengine=external:<path>` 语法 | `ldd`、`readelf -d`、`fio --enghelp` |

测量相关环境备注（后续阶段风险）：
- governor 是 **schedutil** 而非 performance——正式测量前的定频/预热策略是后续实现者的课题，此处只记录现状。
- `/` 与 `/home` 的 btrfs 开了 **zstd:1 压缩**与 async discard，对 I/O 带宽/延迟测量有直接影响；scratch 文件落在哪个子卷要显式选择。
- 依硬约束 5，一切测试 I/O 只能写 `build/scratch/` 下的临时 regular file；注意 `tools/check.py` 默认 scratch 是 Python `tempfile`（落在 /tmp tmpfs）——后续阶段调用时需显式传 `--scratch-dir`。

---

## 6. 审计中的偏差与风险汇总

1. （偏差，非阻塞）bench 仓库当前分支是 `feat/fio-sluice-engine` 而非任务所述 main；指向与 main 相同提交 `2abda4d`，工作区干净——与硬约束 1 一致。
2. （事实，非阻塞）Sluice checkout 有 3 个未跟踪 evidence 目录；无已跟踪文件改动；本仓库对其只读。
3. （风险）系统 fio 未动态链接 liburing，但其 `--enghelp` 列出 io_uring 引擎；其 io_uring 引擎运行时可用性未验证。本仓库已有 `build/fio/upstream`（fio-3.40 源码克隆），引擎集成与对比所需的上游源码在位。
4. （风险）环境无 `liburing.a`（仅动态）；若后续要用静态链接 Sluice ring SDK 的方式构建引擎 .so，需要改用动态 liburing 链接（`-luring` 走 `/usr/lib64/liburing.so`，Makefile ring profile 即如此，`Makefile:11-12`）。
5. （备注）governor=schedutil、btrfs zstd 压缩、`/tmp` 为 tmpfs：见 §5 备注。
6. （备注）构建系统现状为 Makefile；按用户指令迁移 xmake 属后续实现者工作，本次未动。
7. （备注）公共 API 完成观察能力无缺口（§4.2），不需要也不能通过修改 Sluice 来适配 fio。

## 7. 本次实际执行的命令清单（供复核）

```
git -C /home/jnhu/Source/sluice-workload-bench fetch origin --prune          # exit 0
git -C /home/jnhu/Source/Sluice fetch origin --prune                          # exit 0
git rev-parse HEAD; git status; git log -5 --format='%H %d %s'  （两仓库）
cat SLUICE_REVISION                                                          # 5d94f233967c3aef993eb19ba9e3252c532b0951
make SLUICE_SOURCE=/home/jnhu/Source/Sluice JOBS=8 all                       # exit 0（noring：SDK 安装+二进制链接）
find build/noring/sdk -type f                                                # §4.3 产物清单
nm --defined-only build/noring/sdk/lib/libsluice_{async,core}.a              # 公共符号核对
lscpu; findmnt / /home /tmp; lsblk; uname -r
gcc --version; python3 --version; xmake --version; pkg-config --modversion liburing
rpm -q fio liburing liburing-devel; fio --version; ldd /usr/sbin/fio; readelf -d /usr/sbin/fio
fio --enghelp; fio --enghelp=io_uring
fio --enghelp=external:/tmp/definitely-not-here.so                           # 外部引擎装载路径探测（无 I/O）
cat /proc/sys/kernel/io_uring_disabled                                       # 0
git -C build/fio/upstream log -1 / describe --tags / remote -v               # fio-3.40 克隆核对
```

## 8. 未核实事项（明确声明）

1. `make check` / `tools/check.py`、`tools/run_matrix.py`：**未运行**（硬约束 7：除性能测量阶段外不得运行 benchmark 负载；本审计的核对方式按任务要求为读码）。
2. `PROFILE=ring` 的 SDK 与二进制构建：未运行（本次只建了默认 noring）。
3. 系统 fio 内置 io_uring 引擎的运行时可用性：未验证（需要跑 fio 作业，受硬约束 7 限制）。
4. Sluice ring 后端在本机的实际可用性（内核 7.3.3，io_uring_disabled=0）：未运行负载，未验证。
5. fio 外部引擎对真实 .so 的 dlopen+get_ioengine 全流程：只做了不存在路径的错误路径探测。

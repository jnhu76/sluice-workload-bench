# Phase 2 最小引擎架构设计（fio external ioengine × Sluice v2 公共 API）

- 状态：设计定稿；**实现已交付**（见 `adapters/fio/README.md` 与
  PHASE1-REPORT §C–§F；本文保留设计时表述，落地偏差以
  adapters/fio/README.md §3 为准）。
- 日期：2026-11-19。分支：`feat/fio-sluice-engine`（与 `main` 同基 `2abda4d`）。
- 输入材料（本文全部结论的出处）：
  - `docs/fio-integration/REPOSITORY-REALITY.md`（Phase 0 审计，下称 REALITY）；
  - `docs/fio-integration/FIO-ABI-QUALIFICATION.md`（Phase 1 ABI 资格，下称 ABIQ）；
  - Sluice 只读 checkout `/home/jnhu/Source/Sluice` @ `5d94f23`（`SLUICE_REVISION` 一致）：
    `include/sluice/async/*` 公共头、`include/sluice/{error,file_resource,result}.hpp`、
    `docs/explicit-io-contract.md`（v2-r1，下称 CONTRACT，按 requirement ID 引用）、
    `src/async/request_scope.cpp`（实现核对用，只读）；
  - fio 冻结树 `build/fio/upstream` @ tag `fio-3.40`（`ff930c4`，git-ignored）。
- 本文引用格式：Sluice 头/源与 CONTRACT 为该 checkout 内 `path:line`；fio 为冻结树内
  `path:line`；本仓库文件为仓库相对 `path:line`。凡未执行的检查明确标注"未验证"。

## 0. 设计基线与不可协商项

1. **纯 C++20 插件，无薄 C shim**（ABIQ §5 已裁决：裸含 `fio.h` 在 C++20 下干净通过、
   `extern "C"` 包裹反而非法、`ioengine` 全局变量不 mangle 可被 dlsym）。
2. **零 Sluice 改动**：引擎只消费已安装公共面（REALITY §4.2 结论"公共 API 完成观察能力
   无缺口"）。不实现第三种 backend（CONTRACT BACKEND-01：seam 关闭，`docs/explicit-io-contract.md:1077-1101`），
   不新增 Runtime/Scheduler/polling thread。
3. **单一 wrapper 头纪律**：`fio_cxx.hpp` 先含固定集合的 std 头、再含 `fio.h`/`optgroup.h`，
   是全仓库唯一 include fio 头之处（ABIQ §4.4：`minmax.h:4-18` 宏污染使 fio.h 之后的
   std 头 include 失败）。
4. **异常零逃逸**：任何回调内可能抛异常的 Sluice 调用都被 `try/catch` 翻译为
   `io_u->error`/负返回值；逃逸即 `std::terminate`（C 宿主，ABIQ §5 规则 3）。
5. **不伪装、不绕过**：异步 Request 绝不伪装同步完成（每个 `FIO_Q_QUEUED` 的 io_u 恰好
   经 `getevents`/`event` 交付一次）；不绕过 Request lifetime / borrow / publication /
   progress owner / shutdown 语义（对应 CONTRACT REQ-03..05、LIFE-01..02、PROG-01..04、SHUT-01..04）。
6. **无隐藏 fallback**：backend 构造失败=作业失败；绝不回落 POSIX（CONTRACT INV-02、
   BACKEND-02；本仓库硬约束 4）。
7. **不改请求顺序/偏移/数据**：引擎只做 `io_u` → Sluice 操作描述符的一一映射；短读短写
   如实上报 `resid`，由 fio 核心自行 requeue（`io_u.c` `io_completed` 的短 I/O 重排逻辑，
   见 §6）；不缓存、不预读、不额外拷贝。
8. **测量卫生**：所有 I/O 只写 `build/scratch/` 下临时 regular file；正式测量串行（后续
   `run_fio_matrix.py` 契约，见 §7.1）。

## 1. 目录与文件规划

```
adapters/fio/                     # fio 引擎插件（本设计的主体）
  README.md                       # 实现说明骨架（本阶段产出）
  src/
    fio_cxx.hpp                   # 唯一 include fio 头的 wrapper（§0.3）
    options.hpp                   # 引擎私有选项结构 + fio_option 表（§3）
    errno_map.hpp / errno_map.cpp # IoError → errno 映射表（§6.3）
    state.hpp                     # EngineState 结构（§2）
    engine.cpp                    # ioengine_ops 表 + init/cleanup（§4.1/§4.8）
    files.cpp                     # open_file/close_file/get_file_size（§4.7）
    queue.cpp                     # queue/commit + ddir 映射（§4.3/§4.4）
    events.cpp                    # getevents/event/health 路径（§5/§4.6）
  tests/
    errno_map_test.cpp            # 映射表穷举（含 os_errno 透传、fail-closed 分支）
    state_test.cpp                # 槽位记账/容量不变式（链接 fio 头直接构造假 td）
workloads/fio/                    # fio 作业模板（占位文件，路径由工具替换）
  smoke.fio                       # 冒烟：threadpool、4k×16、build/scratch 下
  threadpool-d16.fio              # 测量骨架：--sluice_backend=threadpool、iodepth=16
  uring-d16.fio                   # 测量骨架：--sluice_backend=uring、iodepth=16
tools/
  build_sdk.sh                    # 现有脚本扩展：pic 变体独立前缀（§7 共同约束 2）
  ensure_fio_source.sh            # 新增：克隆/校验冻结 fio 树并 ./configure（§7 共同约束 3）
  check.py                        # 不变（bench 二进制契约，含 exit 77 语义）
  check_fio_engine.py             # 后续代理实现（§7.1 契约）
  run_fio_matrix.py               # 后续代理实现（§7.1 契约）
  compare_fio.py                  # 后续代理实现（§7.1 契约）
xmake.lua                         # 根构建（替代 Makefile，§7）；Makefile 删除
README.md                         # 更新：构建/检查入口改 xmake、二进制新路径（§7）
```

构建产物布局（全部 git-ignored `build/` 下）：

```
build/linux/x86_64/release/
  sluice-workload-bench           # 既有 bench（noring SDK）
  sluice-workload-bench-ring      # 既有 bench（ring SDK + -luring）
  fio-sluice-noring.so            # 引擎（noring PIC SDK）
  fio-sluice-ring.so              # 引擎（ring PIC SDK + -luring）
build/<profile>/                  # 既有：非 PIC SDK 前缀（bench 二进制用）
  sluice/  sdk/  sdk/.ready
build/<profile>-pic/              # 新增：PIC SDK 独立前缀（引擎 .so 用，§7 共同约束 2）
  sluice/  sdk/  sdk/.ready
build/fio/upstream/              # 冻结 fio 树（现有）
build/scratch/                    # 所有临时 I/O 目标（硬约束 5）
```

（`<profile>` ∈ {noring, ring}。PIC 变体用**独立源树+独立安装前缀**
`build/<profile>-pic/`，不与 `build/<profile>/` 共享 `xmake f` 配置——在同一源树上
翻转 `--cxxflags=-fPIC` 会触发整库重编，交替构建 bench 与引擎时不可取。）
注意 xmake 的默认构建根即 `build/`：`xmake clean -a` 会连带删掉冻结 fio 树与已装
SDK。两者均可恢复（`tools/ensure_fio_source.sh` / `tools/build_sdk.sh`，`.ready`
缺失即自动重建），文档明示即可，不做额外防护。

job 文件一律 `ioengine=external:<绝对路径>`（ABIQ §3.3/§6.3：短名 dlopen 依赖宿主
`CONFIG_DYNAMIC_ENGINES`，未探测；仓库内不得依赖）。

## 2. per-job EngineState

### 2.1 结构与所有权

每个 fio `thread_data`（= 每个 job 线程）持有恰好一个 `EngineState`，挂在
`td->io_ops_data`（`fio.h:316`；ABIQ §3.7），`init` 中 `new`、`cleanup` 中 `delete`。
**进程级全局状态只有 const 的选项表与 ops 表**——没有跨 job 的可变 Request 表、没有
共享 context。

```cpp
// state.hpp（设计草图，非最终代码）
struct EngineState {
    // ---- 配置（init 时定格）----
    Backend       backend;      // threadpool | uring（来自 --sluice_backend）
    std::size_t   capacity;     // = context request_capacity = RequestScope capacity
                                //   ≡ td->o.iodepth（恒等，无用户旋钮，§3/§4.5）

    // ---- Sluice 对象：成员声明序=析构序，scope 先于 context 释放 ----
    std::optional<AsyncIoContext> context;   // make_*_context 产物
    std::optional<RequestScope>   scope;     // 构造即认领本线程为 progress owner
                                             // (request_scope.hpp:42 → claim_owner_)

    // ---- 有界 Request 跟踪：与 scope 槽一一对应的旁路表 ----
    struct Slot {
        io_u*                    u;      // fio 借出的 io_u（FLIGHT 期间）
        enum class Kind : std::uint8_t { none, read, write, sync_data, sync_all };
        Kind                     kind;
        ScopeTicket<std::size_t> data;   // read/write 票根
        ScopeTicket<void>        sync;   // sync 票根（与 kind 二选一）
    };
    std::vector<Slot>    slots;          // 恰好 capacity 个
    std::vector<std::size_t> free_slots; // 空闲槽下标栈（O(1) 分配/回收）

    // ---- completion staging：getevents 填、event 取 ----
    struct Completion { io_u* u; int error; unsigned long long resid; };
    std::vector<Completion> staged;      // 上限 capacity
    std::size_t             staged_cursor;

    // ---- open-file 生命周期引用 ----
    std::unordered_map<int, sluice::FileAccess> files;  // fd → 声明访问模式

    // ---- health/failure 状态 ----
    bool failed = false;                 // 已进入失败结算（§4.6 置位）
};
```

要点：

- **RequestScope 是跟踪与结算的唯一脊柱**。它一次给出四件本引擎必须自己发明的东西：
  有界容量预留（`request_scope.hpp:97` `reserve_slot_`）、progress owner 认领
  （构造函数 → `claim_owner_`，`src/async/request_scope.cpp:38-47`）、非破坏完成探测
  （`take`，pending 不误取，ready 即回收槽，`request_scope.hpp:56-57,132-143`）、
  显式收尾（`finish()`）。旁路表 `slots[]` 只保存 fio 侧的关联（io_u 指针 + 票根），
  不复制任何 Sluice 状态。
- **有界性由构造保证**：`slots`/`staged` 大小 == capacity == `td->o.iodepth`，引擎
  任一时刻持有的 Request 数 ≤ fio 自己的 `cur_depth`（§4.5 论证），无无界增长。
- **单线程不变式**：`init/queue/commit/getevents/event/open_file/close_file/cleanup`
  都在 job 线程上调用，`EngineState` 无锁。合法性来自 THREAD-01 的 owner 纪律
  （progress owner 固定为构造线程）加 §4.1 对 offload/verify_async 的显式拒绝。
- **多 job（第一阶段立场：支持，条件封闭）**。fio 多 job = 多线程、每 td 独立
  `init`（`backend.c:1862` 顺序），天然一 job 一 context 一 owner，无共享可变状态，
  THREAD-01 逐 context 成立。`numjobs>1` 同理（td 克隆）。第一阶段**拒绝**的并发形态
  只有两个，都在 `init` 里 fail-closed（§4.1）：`io_submit_mode=offload`（submitter 换
  线程，RequestScope 槽表非线程安全）与 `verify_async`（verify 线程会调 `queue`）。
  offload 由引擎标志 `FIO_NO_OFFLOAD` 让 fio 在装载期就拒绝（`ioengines.c:57-61`）。

### 2.2 io_u ↔ Request 身份映射

| fio 侧 | Sluice 侧 | 归属 |
| --- | --- | --- |
| `io_u*`（`IO_U_F_FLIGHT` 期间借给引擎） | `ScopeTicket`（RequestId 的轻量票根，`request_scope.hpp:24-38`） | 引擎 `slots[i]` 同时持有两者 |
| `io_u->file->fd` | `NativeFileRef{fd, access}`（`async_io_context.hpp:30-38`，公共外来 fd 借用入口） | fd 所有权始终在 fio（LIFE-02：引擎绝不 close） |
| `io_u->xfer_buf`/`xfer_buflen`/`offset` | `ReadOp/WriteOp` 的 `dst/src`/`len`/`offset`（`async_io_context.hpp:40-51`） | 缓冲区借出到 **publication** 为止（REQ-04：`take_result` 取得结果后才可复用；fio 在 `event()` 交付并 `put_io_u` 前不回收缓冲区，`io_u.c:2224-2231`） |
| `io_u->error`/`io_u->resid` | `RequestObservation<std::size_t>.result`（确认字节数或 IoError） | 引擎在 getevents 内写入，返回前完成（ABIQ §3.6） |

身份唯一性：槽下标 `i` 在 queue→event 全程稳定；`event(td, n)` 返回 `staged[n].u`。
每个被 `FIO_Q_QUEUED` 接受的 io_u 恰好 stage 一次、交付一次（§5.1 返回数一致性行）。

## 3. Backend 选择与引擎选项

引擎私有选项结构（首字段 pad 指针为硬规则：fio 核心把 `td` 写进 `td->eo` 的第一个
指针字段，`init.c:1276`；`off1` 永不为 0，ABIQ §3.8）：

```c
struct sluice_options {
    void*            pad;        // fio 核心写入 td，勿动
    char*            backend;    // --sluice_backend：threadpool(默认) | uring
    unsigned int     workers;    // --sluice_workers：threadpool worker_count，默认 4
};
```

语义（全部只是选择调用哪个**既有公共工厂**，不新增 Sluice 配置面）：

| 选项 | 映射 | 依据 |
| --- | --- | --- |
| `sluice_backend=threadpool` | `make_threadpool_context({request_capacity=capacity, worker_count=workers})`（`threadpool_backend.hpp:9-17`；失败抛异常、无 fallback） | 与 bench 一致（`src/runner.cpp:302-305`） |
| `sluice_backend=uring` | `make_uring_context({request_capacity=capacity, queue_depth=(unsigned)capacity})`（`uring_backend.hpp:10-20`） | queue_depth 对齐 capacity，同 bench（`src/runner.cpp:307-313`） |
| `sluice_workers` | 仅 threadpool 有意义；uring 下给非默认值时 `init` 报参数错（fail closed，不静默忽略） | ERR-01 语义拒绝优先 |

**容量没有用户旋钮：`capacity ≡ td->o.iodepth`（恒等式，非默认值）**。这是刻意的：
§4.5 的"无 BUSY/空槽必有"论证**负载在这个恒等式上**——fio 的 `queue_full` 以
freelist 空+`cur_depth ≤ iodepth` 封顶在途量（`io_u.c:1680-1690`、`io_u.c:1724`
`cur_depth++`），若允许 `capacity < iodepth`，第 `capacity+1` 个 `queue()` 必然遇到
空槽，每个超额 io_u 只能以错误失败；允许 `> iodepth` 则只是闲置槽位。与其留一个
只能取无效值或无效冗余的选项再靠 init 校验兜底，不如不暴露（最小可维护）。
任务要求"容量默认对齐 fio job 的 iodepth"由此升级为"恒等于 iodepth"。

失败路径（init 内，全部 = 作业失败，无任何 fallback）：

- noring SDK 构建的引擎收到 `sluice_backend=uring`：`make_uring_context` 返回
  `not_supported`（SDK 无 liburing profile，`uring_backend.hpp:14-16` 注释）→ `log_err`
  明示"engine was built from the noring SDK profile; rebuild with ring profile"→ init
  返回 1。**注意与 bench 的 exit 77 不同**：exit 77 是 `tools/check.py` 对 bench 二进制
  的契约（`tools/check.py:49-55`）；fio 引擎侧的失败语言是 init 失败/作业 err。
- ring 构造期 ring 建立失败：`make_uring_context` 抛异常 → catch → init 失败（对齐
  bench 对 `UringAsyncBackend: io_uring` 前缀异常的处理，`src/runner.cpp:314-320`）。
- `workers == 0`（threadpool 臂）：init 参数校验失败。

## 4. 回调契约（逐条）

ops 表按 ABIQ §3.2 的 fio-3.40 字段序完整声明；**实现**的回调与**故意不实现**的回调
清单如下（不实现的均留 NULL，fio 核心已处理缺省，ABIQ §3.2 mandatory 规则）：

| 回调 | 决定 | 依据/说明 |
| --- | --- | --- |
| `init` | **实现** | 构造 EngineState（§4.1） |
| `queue` | **实现**（强制） | `check_engine_ops` 要求（`ioengines.c:44-46`） |
| `commit` | **实现**（薄） | §4.4：只补 submit 记账，无冲刷语义 |
| `getevents`/`event` | **实现**（强制，非 FIO_SYNCIO） | `ioengines.c:62-65` |
| `open_file`/`close_file`/`get_file_size` | **实现** | open/close：`td_io_open_file` 断言 open_file 存在（`ioengines.c:528`，ABIQ §3.2）；`get_file_size` 必须补上（见下） |
| `cleanup` | **实现** | 与 init 配对；`close_ioengine` 无条件调用，须容忍半初始化状态（`ioengines.c:238-262` 一带，`io_ops_data` 可能为 NULL） |
| `cancel` | **故意 NULL** | §4.9：借出缓冲区安全优先 |
| `prep`/`io_u_init`/`io_u_free` | NULL | 无 per-io_u 引擎态（跟踪在旁路表）；`engine_data` 不用（`io_u.h:100`） |
| `errdetails`/`setup`/`post_init`/`terminate`/zoned/fdp 家族 | NULL | 无需求 |

**`get_file_size = generic_get_file_size`（直接转交，不自写）**。留 NULL 是错误的：
无回调时 `td_io_get_file_size` 直接 `return 0`（`ioengines.c:659-665`），文件尺寸
不会被计算；`filesetup.c:865-867` 仅当 `real_file_size == -1ULL && td->o.size` 才回退
`size/nr_files`——不带显式 `size=` 的常规作业会拿不到文件大小。所有随树的文件型
引擎都设它（`engines/libaio.c:493`、`engines/io_uring.c:1695`）。符号已实证导出：
`nm -D /usr/sbin/fio | grep -w generic_get_file_size` →
`0000000000031460 T generic_get_file_size`（本阶段复跑）。该符号加入 §6.4 固定集。

引擎 flags：`FIO_NO_OFFLOAD`（装载期拒绝 offload，`ioengines.c:57-61`；io_uring 引擎
同款，`engines/io_uring.c:1682`）。**不设** `FIO_SYNCIO`（异步）、**不设**
`FIO_ASYNCIO_SETS_ISSUE_TIME`（issue_time 由核心在 queue 后打点，
`ioengines.c:458-466`；我们不调 `fio_gettime`/`io_u_queued`）。

### 4.1 init(td)

1. 拒绝封闭集合外的形态：`td->o.verify_async != 0` → `log_err` + 返回 1（verify 线程
   会从非 owner 线程调 `queue`，RequestScope 槽表无锁，THREAD-01 的"他线程可 submit"
   是 **context** 级承诺，`docs/explicit-io-contract.md:1003-1021`，不是 scope 级）。
   （offload 已由 `FIO_NO_OFFLOAD` 在装载期挡掉，双保险不再查。）
2. 解析 `td->eo` → backend/workers；`capacity = td->o.iodepth`（§3 恒等式，无旋钮；
   含 workers==0 等非法组合的参数校验）。
3. 构造 context（工厂调用，try/catch → log_err + 返回 1）。
4. `scope.emplace(*context, capacity, ScopeCleanupPolicy::drain)` —— 本线程成为该
   context 的固定 progress owner（`request_scope.hpp:42`；构造抛异常同样 catch → 失败）。
5. 分配 `slots`/`free_slots`/`staged`（一次 `capacity` 大小）。
6. `td->io_ops_data = state`。

init 失败必须保证已分配资源析构（`unique_ptr`/optional 成员自然满足）；`cleanup` 对
`td->io_ops_data == NULL` 直接返回（err 路径也会走 `close_ioengine`，§4.8）。

### 4.2 借用与身份（queue 的前置事实）

`NativeFileRef{f->fd, files[f->fd]}` 每次提交即时构造（值类型，`async_io_context.hpp:30-38`）。
访问模式在 `open_file` 时用 `fcntl(fd, F_GETFL)` 的 `O_ACCMODE` 推导并登记（§4.7），
保证 SEM-03 的 read/write 合法性检查在**接受点**而不是物理层才失败。

### 4.3 queue(td, io_u)

核心前置：fio 已置 `IO_U_F_FLIGHT`、清零 `error/resid`、断言文件打开
（`ioengines.c:336-363`）。引擎职责：把 io_u 翻译成一个 Sluice 提交，返回
`FIO_Q_QUEUED`（已接受、稍后经 getevents 交付）。

DDIR 映射表（`io_ddir.h:4-13`）：

| `io_u->ddir` | Sluice 提交 | 备注 |
| --- | --- | --- |
| `DDIR_READ`(0) | `scope.submit_read({file, (std::byte*)xfer_buf, xfer_buflen, offset})` | 确认字节数 → `resid` 换算（§6.2） |
| `DDIR_WRITE`(1) | `scope.submit_write({file, (const std::byte*)xfer_buf, xfer_buflen, offset})` | 同上 |
| `DDIR_SYNC`(3) | `scope.submit_sync_all({file})` | 结果 `Request<void>`；`resid=0` |
| `DDIR_DATASYNC`(4) | `scope.submit_sync_data({file})` | 同上 |
| `DDIR_TRIM`(2)/`DDIR_SYNC_FILE_RANGE`(5)/`DDIR_WAIT`(6) | 不支持 | `io_u->error = EINVAL`，返回 `FIO_Q_COMPLETED`（带错的同步完成=fail closed；不设 trim 相关 flags，fio 默认作业不会生成这些 ddir） |

提交路径（read/write 为例）：

```
slot = free_slots.pop()                     // 空槽必有（§4.5 恒等式）；实现仍带显式
                                            // 空栈检查：log_err+terminate（记账 bug 不
                                            // 许以 UB 形式出现）
ticket = scope.submit_read/write(...)       // try/catch 包裹
  ├─ ok      : slots[i] = {io_u, kind, ticket}; return FIO_Q_QUEUED
  └─ 错误(e) : free_slots.push(i)
                io_u->error = errno_of(e)   // §6.3 映射
                log_err(...)                // 仅 invalid_state/backend_error 等
                                          // 无损映射缺失分支
                return FIO_Q_COMPLETED      // 带错完成：请求未被接受，无后续交付
```

**绝不返回 `FIO_Q_BUSY`**，论证见 §4.5；**绝不返回 `FIO_Q_COMPLETED` 表示成功**除非
Sluice 真的在 queue 内同步完成——本设计里唯一返回 COMPLETED 的是"提交被拒（带
error）"与"不支持的 ddir（带 error）"两条失败路径，二者都不是把异步伪装成同步
（请求根本没被接受；INV-02：拒绝不产生后台效果，`docs/explicit-io-contract.md:328-345`）。

fio 不得提前回收借用中的 buffer：由核心记账保证——`event()` 交付 → `io_completed` →
`put_io_u` 之后 io_u 才回 freelist（`io_u.c:2224-2231`、`io_u.c:843-870`），而引擎侧
`take_result` 已在 getevents 内取得 publication（REQ-04 借出到 publication 为止，
`docs/explicit-io-contract.md:656-671`）。两侧边界重合，无窗口。

### 4.4 commit(td)

Sluice 提交无批冲刷语义（INV-02：submit 只做有界记账+安排派发），所以 commit 无实事可做；
但提供它并补一处记账：

```cpp
static int sluice_commit(thread_data* td) {
    io_u_mark_submit(td, td->io_u_queued);   // 提交延迟直方图（io_uring 引擎同款，
    return 0;                                // engines/io_uring.c:868）
}
```

不提供 commit 的引擎若返回 QUEUED，核心在 queue 点同时记 submit+complete
（`ioengines.c:427-431`），直方图失真（`io_u_mark_submit/complete` 是纯统计，
`io_u.c:1123-1134`；in-flight 记账不受影响但纪律上不整洁）。提供 commit 后
`td_io_commit` 的 `io_u_queued → io_u_in_flight` 转移语义（`ioengines.c:490-514`）
照常由核心完成。

### 4.5 容量不足与 FIO_Q_BUSY 的排除（不丢请求、不死锁的论证）

`FIO_Q_BUSY` 的核心义务：核心回滚计数并**依赖 commit+getevents 走出重试**
（`ioengines.c:393-398`、reap 路径 `ioengines.c:299-303`）；若引擎在"永远拿不到空槽"
时返回 BUSY，而此时又没有任何在途完成可以唤醒 getevents，就是重试死锁。本设计使其
不可达：

- `capacity ≡ td->o.iodepth`（§3 恒等式，**无用户旋钮**——这是论证的承重墙，不是
  默认值）。fio 的 `queue_full` 以 io_u freelist 排空为准（`io_u.c:1680-1690`），
  freelist 共 `iodepth` 个单元，每个取出使 `cur_depth++`、`put_io_u` 使 `cur_depth--`
  （`io_u.c:1715-1727,858-861`），故"已接受未交付"的 io_u 数 ≤ `cur_depth ≤ iodepth`
  == capacity。
- 引擎每接受一个 io_u 占一个 scope 槽，交付（`take` ready）即回收槽。因此
  `queue()` 时刻 `free_slots` 非空 **by construction**，scope 级
  `would_block`（`request_scope.cpp` `reserve_slot_` 失败分支返回
  `IoError::Code::would_block`）不可达；`free_slots.pop()` 前仍有显式空栈运行时检查
  （§5.1 同款纪律：log_err + fail closed，不信任 by-construction 到 UB 的程度）。
- 其余提交错误（context 级 admission 关闭、健康失败、`invalid_state`）是**非瞬态**的：
  重试不可能成功。把它们映射为 BUSY 会造成"BUSY→commit(no-op)→getevents(min>0,阻塞)
  →无完成可等"的死锁（见上）。故一律走 §4.3 的"带错 COMPLETED"路径。

结论：queue 的返回值域 = {`FIO_Q_QUEUED`(已接受), `FIO_Q_COMPLETED`(带 error 的拒绝)}。
FIO_Q_BUSY 永不出现，commit 的存在只是记账完整性与对未来核心路径的兼容垫底。

### 4.6 getevents / event / 失败结算

`getevents` 全语义见 §5；`event` 见 §6；健康失败路径（tier 划分）见 §5.5。

### 4.7 open_file / close_file

```cpp
open_file:  r = generic_open_file(td, f);  if (r) return r;
            fl = fcntl(f->fd, F_GETFL);
            access = O_ACCMODE(fl) → {O_RDONLY→read_only, O_WRONLY→write_only,
                                      O_RDWR→read_write}（file_resource.hpp:12-16）
            state->files[f->fd] = access;  return 0;
close_file: state->files.erase(f->fd);     // 先撤登记
            return generic_close_file(td, f);
get_file_size: return generic_get_file_size(td, f);   // 纯转交（§4 回调表论证）
```

- fd 生命周期归 fio：`generic_open_file/close_file`（`filesetup.c:717/654`，宿主已导出，
  ABIQ §1）处理 O_DIRECT/O_CREAT/哈希；引擎只登记借用事实（LIFE-02：外来代码不得
  close 借来的 fd，`docs/explicit-io-contract.md:974-985`）。
- `close_file` 运行时无在途 Request 引用该 fd：fio 的拆除序保证（线程尾
  `close_and_free_files` 在 io_u 清点排空之后，`backend.c` §3.4 序 7；§4.8 再述）。
  防御性：若 `files.count(fd)` 存在且引擎仍有 slot 引用该 fd 的在途 Request（正常不可
  达），`close_file` 记 log_err 并仍转交 generic（fd 是 fio 的，引擎无权扣留）。
- `F_GETFL` 只在 open 路径各一次，不影响 I/O 路径（硬约束 4 的"不改变"针对请求
  顺序/偏移/数据，open 期的一次元数据查询不是数据面动作）。

### 4.8 cleanup(td)

fio 调用序（冻结树核实）：线程尾 `close_and_free_files(td)` → `cleanup_io_u(td)` →
`close_ioengine(td)` → `cleanup` → `dlclose`（`backend.c` §3.4 序 7、`ioengines.c`
close_ioengine）。即 **cleanup 运行时：文件已关、io_u 池已清、且在途 io_u 已被
排空**（正常路径 `do_io` 尾 `io_u_queued_complete(td, cur_depth)` 阻塞排空
（backend.c §"do_io tail"）；错误路径 `cleanup_pending_aio` 的最终
`io_u_queued_complete(td, cur_depth)` 同样阻塞至清（`backend.c:229-257`；无 cancel
回调时第 239-249 行的提前 put 分支被跳过）。且此处 `min == cur_depth == 未交付 io_u 数`
不是死锁：`cur_depth--` 只发生在 `put_io_u`（`io_u.c:858-861`），min 永不超过在途数）。

结算步骤（全部 noexcept 外壳，异常只 log）：

```
cleanup(td):
  state = td->io_ops_data; if (!state) return;     // err 路径半初始化防御
  if (!state->staged.empty())
      log_err("sluice: %zu completions never consumed by fio", ...);  // 只记录，
      // io_u 所有权此时已归 fio 池，不触碰
  1. settle：若 !state->failed:
       r = state->scope->finish()        // ScopeCleanupPolicy::drain 语义；
       //  正常路径在途=0，finish 即刻返回；r 出错则 log_err（作业此刻早已带错）
  2. destroy：按成员序析构 optional<RequestScope> → optional<AsyncIoContext>
       // scope 析构释放 ProgressOwner（request_scope.hpp 成员 owner_），
       // context 析构前置条件随之满足（SHUT-04：无存活公共绑定/宿主注册，
       //   docs/explicit-io-contract.md:1281-1294；finish 已消费全部 Request 绑定）
  3. delete state
```

失败路径（state->failed 已置位，§5.5 tier-1 已完成 shutdown 并把全部在途以真实结果/
映射错误交付）：finish() 在空槽上平凡返回，后续同上。tier-2 不会走到 cleanup（进程已
terminate）。**不靠丢弃指针伪装取消**：任何在途 Request 都经 `take_result`（正常）或
`shutdown` 结算（失败）取得终态后才释放绑定；HANDLE-02 禁止销毁非终态 Request
（`docs/explicit-io-contract.md:726-732`）。

### 4.9 为什么不实现 cancel（借出缓冲区安全）

`cleanup_pending_aio` 对每个 FLIGHT io_u 调 `->cancel`，返回 0 则**立即** `put_io_u`
回收 io_u（`backend.c:239-249`）。对 Sluice：CANCEL-01 的可赢取消只发生在派发前，
运行中的操作不能物理中断（ThreadPool 明确不支持，REALITY §4.2 表内注明；CANCEL-01
`physical_interruption_unsupported` 行，`docs/explicit-io-contract.md:1041-1061`）。
若 cancel 在 Request 未 publication 时返回 0，fio 立即复用 `xfer_buf`，而 Sluice 后端
可能仍在写它——违反 REQ-04 借用边界与 CANCEL-02（"None alone authorizes buffer
reuse"，`docs/explicit-io-contract.md:1063-1074`）。返回非 0（busy）则核心还是走阻塞
drain。故最优解是**不提供 cancel**：fio 的提前退出路径整体退化为阻塞排空
（`io_u_queued_complete(cur_depth)`，t==NULL），借用窗口完整保留，CANCEL-02 的
"scoped adapter 必须结算后才退出"由我们的 getevents 驱动 wait 满足。

## 5. getevents() 全语义设计

签名：`int getevents(td, unsigned min, unsigned max, const timespec* t)`
（`ioengines.h:41`）。核心调用事实（全部读自冻结树）：

- 核心先把 max 夹到 `cur_depth`、再把 max 抬到 ≥ min（`ioengines.c:304-307`）；返回
  `r≥0` 时 `io_u_in_flight -= r` 并记 r 个完成，`r<0` 进 `td_verror`
  （`ioengines.c:313-321`）。
- 调用形态只有两种（`io_u.c:2284-2291`）：`min>0` → `t==NULL`（阻塞至 min）；
  `min==0` → `t=={0,0}`（立即轮询）。`min>0 && commit` 时先调 commit
  （`ioengines.c:299-303`）。
- `min ≤ 未交付在途数` 在最终排空点成立（§4.8 论证）；轮询点 min=0 无约束。

### 5.1 主循环（伪代码）

```
getevents(td, min, max, t):
  S = state
  if (S->staged_cursor != S->staged.size())     // 运行时检查，非 assert()：
      log_err + terminate                       // release(-DNDEBUG) 会编译掉 assert，
                                                // 记账不变式必须真的被检查（§7）
  S->staged.clear()
  budget = (t == NULL) ? UNBOUNDED
         : (t->{0,0} ? ZERO : ns(t))            // 一般有限预算按相对值处理，
                                                // libaio 先例（engines/libaio.c:228-239）
  deadline = now + budget
  loop:
    harvest():                                  // 非破坏 take 扫描
      for slot in slots (跳过空槽), while staged.size() < max:
        obs = scope.take(slot.ticket)           // pending 不动，ready 即回收槽
        if obs.readiness == ready:
          fill (io_u->error/resid)              // §6
          stage {slot.u, error, resid}; release slot → free_slots
        else if obs.readiness == empty:         // 票根在 scope 里解析失败=引擎记账 bug
          log_err("sluice: lost accepted request") + terminate   // fail closed，见下
    if staged.size() >= min or staged.size() == max: return staged.size()
    if budget != UNBOUNDED and now >= deadline: return staged.size()   // 可为 0
    pass = S->context->poll_progress()          // 有界非阻塞推进（owner 身份）
    if pass 是错误 or pass.health_failed: → §5.5 健康失败路径
    if pass.completed > 0 or pass.immediate_work_remains
       or pass.dispatch_retry_remains:  continue        // 还有驱动者欠的活，不许 park
                                                          // (PROG-01:828-863；async_io_context.hpp:68-78)
    // 静默：已接受工作在内核/worker 手里（accepted_work_remains 允许正常 park）
    w = S->context->wait_one(remaining(deadline) 或 无界)   // async_io_context.hpp:180-182
    if w 是错误: → §5.5
    switch w.kind:
      progress / deadline_expired: continue     // deadline 到期不取消 I/O（PROG-04）；
                                                // 回到 harvest 再探一次再判超时
      control_interrupted: acknowledge_progress_control(); continue
                                                // 粘性控制，先退休再等（PROG-04；
                                                // 稳态不应出现——无人调 request_stop，
                                                // terminate 回调为 NULL）
      health_failure: → §5.5
```

`readiness == empty` 的 fail-closed 分支不是装饰：`take` 对"票据有效但 scope 里
找不到 owning 槽"返回 empty（`request_scope.hpp:132-143` `resolve_` 失败 →
`RequestObservation{}`）。那只可能是引擎自己的记账 bug（票根与槽失配）。此时对应
io_u 永远不会完成，而 fio 的阻塞形态（`min>0 → t==NULL`，`io_u.c:2284-2291`）与
错误路径的最终排空（`backend.c:251-254`）都会**无限期死等且无诊断**。所以选择
log_err + `std::terminate`（Tier-2 同款纪律）：把挂死变成带现场的可诊断失败。bench
原版对同一情形是抛 `"lost accepted request"`（`src/runner.cpp:391-394`）；引擎回调
不允许异常逃逸，故翻译为 terminate（§5.3）。

各语义分支对照：

| 场景 | 行为 |
| --- | --- |
| `min=0`（`{0,0}` 轮询） | harvest 一轮 + 至多一次 `poll_progress` 有界推进，立即返回 0..max；绝不 park |
| `min>0, t==NULL`（阻塞） | 循环直至 staged ≥ min；静默时 `wait_one()` 无界 park（非自旋） |
| `min>0, t` 有限 | 同上，但 park 预算=剩余 deadline；到期返回已有数（可为 < min 或 0） |
| 已完成未消费事件 | 下一次 getevents 的首轮 harvest 即取走（票根 ready 状态留存于 scope 槽） |
| 多 Request 同时完成 | 单轮 harvest 全部收走（上限 max） |
| 乱序完成 | 交付顺序=槽表顺序而非完成顺序；fio 不要求顺序，延迟归因用 queue 时打的 issue_time（§4 flags） |
| 返回数与 event 可取数 | `event(td,i)` 对 `i∈[0,r)` 直接返回 `staged[i].u`，r=getevents 返回值；核心恰调 r 次（`io_u.c:2224-2231`）→ 严格一致 |
| 不 busy-spin | 仅在 `poll_progress` 报告驱动者欠活（immediate/dispatch_retry）时连转；否则必经 `wait_one` park（PROG-02 睡眠前置条件由 context 的 owned-driver 等待协议内部建立，`async_io_context.hpp:161-182` 注释；bench 同款消费者先例 `src/runner.cpp:424-431`） |
| 有限超时下不无限阻塞 | deadline 覆盖所有 park 与循环；`wait_one(remaining)` 有界（`async_io_context.hpp:182`） |

### 5.2 为什么 harvest 用 `take` 而不是 `try_result`

`take` = 非破坏探测 + ready 即消费并回收槽（`request_scope.hpp:56-57`，实现
`src/async/request_scope.cpp:245-253`：pending 原样保留、ready 时 `release_slot_`）。
`try_result` 只观察不消费（`request.hpp:134-144`），消费还需要第二次调用。take 一步
到位且把"槽回收"耦合在成功消费上，避免观察/消费两步间的状态漂移。

### 5.3 与 bench 骨架的关系

主循环是 `src/runner.cpp:386-431`（take 扫描 → 无进展 `wait_one(until-now)` →
异常分支）的移植，差异三处，均有依据：(a) 在 park 前显式 `poll_progress()` 并尊重
residue 标志——PROG-01 要求驱动者不得等待"尚未派发"的完成（`docs/explicit-io-contract.md:836-841`），
uring 的 transport/completion 分裂使 `dispatch_retry_remains` 可能出现
（`async_io_context.hpp:73-76` 注释）；(b) 超时来源从 bench 的 20 s 硬上限换成本文
§5.1 的调用参数语义；(c) bench 的两个异常分支（`"lost accepted request"`
`src/runner.cpp:391-394`、健康/控制异常 `src/runner.cpp:427-431`）不允许越过回调边界，
分别翻译为 empty→terminate（§5.1）与 §5.5 的 tier 化健康路径。

### 5.4 多 io_u 短完成与 requeue 的交互

短读短写：`resid = xfer_buflen - confirmed`（§6.2），`error=0`。fio 核心在
`io_completed` 里自行调整 `xfer_buflen/xfer_buf/offset` 并 requeue 剩余部分
（`io_u.c` io_completed 的短 I/O 分支）；引擎**不做**任何补齐或重试（否则等于改
数据/隐藏 fallback，硬约束 4）。

### 5.5 健康失败与 shutdown（分级、fail closed）

触发点：`poll_progress`/`wait_one` 返回 Result 错误，或
`ProgressWaitOutcome::health_failure`（"progress/wait 机器无法安全继续"，
`async_io_context.hpp:165-168` 注释）。

- **Tier 1（可结算）**：本线程是 owner（THREAD-01：只有 owner 驱动 shutdown，
  `docs/explicit-io-contract.md:1204-1227`）→ 直接驱动
  `context->shutdown(ShutdownPolicy::drain)`（阻塞、可能无界，SHUT-03 明示这是
  生命周期操作）。结果：
  - `completed`：物理执行与通知引用已退休（SHUT-03）→ 对每个在途槽执行 `take`
    （execution-closed 后终态 handle 仍可消费，SHUT-01）→ 把**真实结果或映射错误**
    交付为完成（staged）；`state->failed = true`；getevents 返回 staged 数。后续
    queue() 一律走 §4.3 拒绝路径（`io_u->error` 带错 COMPLETED），作业以 err 结束。
    cleanup 走 §4.8 失败分支（finish 平凡、正常析构）。
  - `health_failed` / `health_unresolved` / Result 错误：**Tier 2**。
- **Tier 2（不可结算，fail closed）**：`log_err`（写明"outstanding Sluice requests
  cannot be safely retired; buffers must not return to fio"）后 `std::terminate()`。
  理由：此时在途 Request 的借用（buffer/fd）**不能**被宣布结束（SHUT-03："A report of
  health failure alone does not imply completion or permit File/buffer destruction"），
  把 io_u 交还 fio = 让 fio 复用仍被借用的缓冲区（内存安全事故）；扣住不交 = fio 在
  最终排空点永久阻塞（§4.8 的 min==cur_depth 阻塞等待）。二者都不诚实，唯一不撒谎、
  不损坏数据的出口是进程级失败。这与 Sluice 自己对该状态的立场一致
  （`src/async/request_scope.cpp` settlement 对 health failure 走 fail_fast →
  `std::terminate`，`src/async/fail_fast.cpp`）。我们**不吞掉**它、也不用泄漏指针的方式
  绕过（硬约束 8）。

## 6. event() 与错误映射表

### 6.1 event(td, i)

```cpp
static io_u* sluice_event(thread_data* td, int i) {
    EngineState* S = ...;
    if (S->staged_cursor >= S->staged.size()) {   // 运行时检查（非 assert，§7）
        log_err("sluice: event() called beyond staged completions");
        std::terminate();                        // 见下：返回 nullptr 不是选项
    }
    return S->staged[S->staged_cursor++].u;
}
```

**event() 绝不返回 nullptr**：核心在 `ios_completed` 里先调 `event` 再把返回值**无判空地**
交给 `io_completed`（`io_u.c:2224-2231`），而 `io_completed` 第一行就解引用
`io_u->ddir`（`io_u.c:2093-2100`）——空指针意味着立即段错误，把可诊断的记账 bug
变成崩溃。越界只可能是 getevents/event 契约的引擎侧 bug；fail closed（log_err +
terminate）与 §5.1 empty 分支同款纪律。

`error/resid` 已在 getevents 的 harvest 内写好（ABIQ §3.6：核心要求返回前完成）。

### 6.2 resid 与短读短写

成功（`Result<std::size_t>` 有值）：`confirmed = value`（request.hpp:203-206：终态负载
即 `effect.confirmed_bytes`）；`resid = xfer_buflen - confirmed`；`error = 0`。
`confirmed == len` 是常态；短读（EOF）/短写如实上报，后续交给 fio requeue（§5.4）。
`eof` 语义即"短读"而非错误（CONTRACT SEM-05，`docs/explicit-io-contract.md:459-481`）。
sync 类（`Request<void>`）：`resid = 0`，成败看 Result。

### 6.3 Sluice → Linux errno 映射表（errno_map）

规则一（无损优先）：`IoError.os_errno != 0` 时**原样使用** `os_errno`
（ERR-01："On Linux preserve the native errno when present"，
`docs/explicit-io-contract.md:555-560`；`error.hpp:24-26` 字段）。

规则二（code 归约，`os_errno == 0` 时）：

| IoError::Code | errno | 备注 |
| --- | --- | --- |
| `eof` | 非错误：走 §6.2 resid 路径 | |
| `canceled` | `ECANCELED` | |
| `interrupted` | `EINTR` | |
| `would_block` | `EAGAIN` | 作为操作终态错误如实上报 |
| `no_space` | `ENOSPC` | |
| `permission_denied` | `EACCES` | |
| `not_found` | `ENOENT` | |
| `invalid_argument` | `EINVAL` | |
| `not_supported` | `ENOTSUP` | log_err 记录 code 名 |
| `invalid_state` | `EIO` + `log_err` | 引擎/上下文状态错，无损映射不存在 → fail closed 并记录 |
| `backend_error` | `EIO` + `log_err` | 同上（ERR-01："Errors not given a canonical mapping retain backend_error plus native detail"） |

"fail closed 并记录"= 绝不映射为 0/成功、不猜具体设备错误；用保守 `EIO` 同时经 fio
导出的 `log_err`（`nm -D` 实证导出，本文 §6.4）打一行含 `to_string(code)` 与
`os_errno` 的诊断。映射表穷举进 `tests/errno_map_test.cpp`。

### 6.4 引擎调用的 fio 内部符号（固定最小集）

`generic_open_file`、`generic_close_file`、`generic_get_file_size`、`log_err`、
`io_u_mark_submit`（`generic_get_file_size` 本阶段 `nm -D` 复验：
`0000000000031460 T generic_get_file_size`；其余 ABIQ §1 + 本阶段复验）。不调
`fio_gettime`/`io_u_queued`（§4 flags 决定）。符号集在实现阶段写入
`adapters/fio/README.md` 的清单并在构建后用 `nm -D` 断言（§7 共同约束 4）。

## 7. xmake 构建设计（替代 Makefile/CMake）

用户指令：本仓库构建系统迁移 xmake，不引入新的 Makefile/CMake。实现阶段的根
`xmake.lua` 目标（设计定稿，本阶段不落地）：

| target | kind | 说明 |
| --- | --- | --- |
| `sluice-workload-bench` | binary | `src/*.cpp` + `build/noring/sdk`（非 PIC），无 -luring；等价现 Makefile 默认 profile |
| `sluice-workload-bench-ring` | binary | `build/ring/sdk` + `-luring`（动态，环境无 liburing.a，REALITY §5） |
| `fio-sluice-noring` | shared | `adapters/fio/src/*.cpp` + `build/noring-pic/sdk`；产物 `fio-sluice-noring.so` |
| `fio-sluice-ring` | shared | `build/ring-pic/sdk` + `-luring` |
| `sluice-sdk-*`（4 个） | phony | before-build 钩子跑 `tools/build_sdk.sh`，产物以 `build/<profile>[-pic]/sdk/.ready` 做时间戳契约（沿用现脚本语义，`tools/build_sdk.sh:40`） |
| task `check` / task `check-ring` | task | 见共同约束 5（保留 REQUIRE_URING 语义与两 profile 的 check 能力） |

共同约束：

1. `set_languages("c++20")`；release 默认（`-O3 -DNDEBUG` 对齐现 Makefile CXXFLAGS，
   `Makefile:2`）；目标目录用 xmake 默认 `build/linux/x86_64/release/`（release 模式
   下与要求一致，不另行 set_targetdir 以免跨模式错位）。
2. SDK 就绪仍经 `tools/build_sdk.sh`（支持 `SLUICE_SOURCE` env，`tools/build_sdk.sh:16-20`），
   四个前缀（noring / noring-pic / ring / ring-pic）互不混用；脚本扩展一个 pic 参数，
   pic 变体用**独立源树与独立安装前缀** `build/<profile>-pic/{sluice,sdk}`（不与
   `build/<profile>/` 共享 `xmake f` 配置：在同一源树上翻转 `--cxxflags=-fPIC` 会整库
   重编，交替构建时不可取），配置形如 `xmake f -m release --liburing=y|n
   --cxxflags=-fPIC`（`xmake f --help` 实证 `--cxxflags` 存在）。PIC 只给引擎 .so 用；
   bench 可执行文件继续用非 PIC SDK（避免测量面引入 PIC 开销）。附带说明：xmake 默认
   构建根即 `build/`，`xmake clean -a` 会连带删掉冻结 fio 树与已装 SDK——均可恢复
   （`tools/ensure_fio_source.sh` / `tools/build_sdk.sh`，`.ready` 缺失即重建），已知即
   可。**未验证项**：PIC SDK 实际构建未在本阶段运行（设计阶段不跑构建），列为 §9
   风险 R1。
3. 引擎目标的头路径：`-I build/fio/upstream -include build/fio/upstream/config-host.h`
   （ABIQ §3.9；config-host.h 由树内 `./configure` 生成，`tools/ensure_fio_source.sh`
   负责：浅克隆（或复用既有树）、断言 `git describe --tags --exact-match` == `fio-3.40`
   且 HEAD == `ff930c4653ae3952d6b09ab3ec89671aeabf2cbe`、跑 `./configure`）。禁止把
   fio 头复制进仓库（ABIQ §7 冻结物策略）。
4. 符号可解析与版本可追溯（构建后断言，写进 check_fio_engine.py 前置步骤）：
   `nm -D <engine>.so | grep ' ioengine$'` 存在且未 mangle（ABIQ §4.5 实证模式）；
   undefined 符号 ⊆ §6.4 的 fio 内部集 + libstdc++/liburing；链接用 `g++ -shared -fPIC`
   使 libstdc++ 进 DT_NEEDED（宿主 fio 未链 libstdc++，dlopen 时由系统解析，ABIQ §4.5
   已实证可行）。版本宏：引擎编译进 `-DSLUICE_REVISION=...`（读 `SLUICE_REVISION` 文件）、
   `-DSLUICE_BUILD_PROFILE=<noring|ring>`、`-DFIO_SOURCE_TAG=fio-3.40`；
   `init` 时可 `log_err` 一行版本串供结果对账。
5. check 任务两个，覆盖现 Makefile 的两种形态（`Makefile:29-30`：`make check` 与
   `REQUIRE_URING=1 make PROFILE=ring check`）：
   - `xmake check` = `python3 tools/check.py
     build/linux/x86_64/release/sluice-workload-bench`（noring 臂）；
   - `xmake check-ring` = `python3 tools/check.py
     build/linux/x86_64/release/sluice-workload-bench-ring --require-uring`（ring 臂
     强制 uring 可用，等价 REQUIRE_URING=1；`tools/check.py:30,49-55`）。
   退出码原样透传（**exit 77 = UNAVAILABLE 语义保留**；check.py 本身不改）。注意
   check.py 会跑小型功能负载（120 请求级，`tools/check.py:31-96`），按硬约束 7 只在
   显式调用时执行，不挂在默认构建上。README.md 的构建/检查说明与二进制路径
   （`README.md:26,38,61` 现引 `make PROFILE=ring check`/`REQUIRE_URING=1` 与
   `build/<profile>/sluice-workload-bench` 旧路径）随迁移一并更新到 `xmake` 入口与
   `build/linux/x86_64/release/` 新路径（已列入 §1 文件计划）。

### 7.1 tools 契约（后续代理实现的接口边界）

| 工具 | 契约要点 |
| --- | --- |
| `check_fio_engine.py <engine.so> [--fio PATH] [--scratch-dir build/scratch]` | 断言 `fio --version` == 引擎构建版本（ABIQ §6.1 版本门禁）；用 `ioengine=external:<abs>` 跑 `workloads/fio/smoke.fio`（4k×16 级别）于 scratch 下临时 regular file；断言 err=0/exit 0；退出码 {0,1,77}（77=引擎 init 报不可用，如 noring 引擎配 uring） |
| `run_fio_matrix.py` | 串行逐 case（硬约束 7）；显式 `--scratch-dir build/scratch`（避开 /tmp tmpfs，REALITY §5 备注）；JSONL 到 `results/`：记录 fio 版本、引擎 sha256、SLUICE_REVISION、profile、backend、iodepth、workers、rw、trace 参数；case 集 = workloads/fio/*.fio 的参数化展开 |
| `compare_fio.py` | 读 JSONL，对比 fio 臂（threadpool/uring）与既有 bench 臂的 IOPS/BW/延迟分布；只做汇总不改数 |

## 8. 与硬约束的对照（自查）

| 硬约束 | 落点 |
| --- | --- |
| 1 只在 feat/fio-sluice-engine 开发；Sluice 只读 | 本阶段仅新增本仓库两份文档；对 `/home/jnhu/Source/Sluice` 零写入 |
| 2 不改 Sluice v2 API、不新增 Runtime/polling thread | 引擎纯消费者（§0.2）；线程模型=复用 fio job 线程为 progress owner，零新增线程（ThreadPool worker 是工厂内部既有语义） |
| 3 异步不伪装同步、不绕过 lifetime/borrow/publication/owner/shutdown | §4.3/§4.5/§5.5/§4.8/§4.9 |
| 4 不改顺序/偏移/数据、无缓存/预读/拷贝、无 POSIX fallback | §4.3 一一映射；§5.4 短 I/O 交 fio requeue；§3 失败即作业失败 |
| 5 I/O 只写 build/scratch 下 regular file | §7.1 工具契约；workloads 模板 filename 占位由工具落 scratch |
| 6 xmake 替代 Makefile/CMake | §7；实现阶段删 Makefile |
| 7 正式测量串行、测量外不跑负载 | §7 check 任务显式触发；本阶段未运行任何负载（§9 声明） |
| 8 如实汇报 | §9 未验证清单 |

## 9. 未验证事项与开放风险（如实声明）

- **R1（PIC SDK）**：`build/<profile>-pic/sdk` 的实际构建未运行（设计阶段不跑构建，
  硬约束 7 的精神+任务"只写文档"）。机制已核实可行（`xmake f --cxxflags=-fPIC` 存在），
  但 Sluice 静态库在 `--cxxflags=-fPIC` 下的实际重编与链接进 .so 未实证。实现阶段
  第一个动作：构建 pic SDK 并链接最小 .so，失败则升级。
- **R2（uring dispatch_retry 的运行时行为）**：`poll_progress` 尊重
  `dispatch_retry_remains` 的循环在 uring profile 下未运行验证（需要 ring SDK+负载）。
  设计依据是头注释与 PROG-01 文本（`async_io_context.hpp:71-76`）；若实测 residue
  标志行为与注释不符，以 Sluice 实现为准修正循环。
- **R3（健康失败 Tier 2 = 进程终止）**：矩阵/CI 环境若期待"优雅失败退出码"，Tier 2 的
  `std::terminate`（abort）会显得突兀。这是刻意选择（§5.5 论证），但需要在
  check_fio_engine.py 的文档里写明；若运行所有者要求优雅退出，需重新裁决（不存在既
  诚实又优雅的第三条路：交还缓冲区=UAF，扣住=死锁）。
- **R4（take 的 O(n) 槽解析）**：`RequestScope::resolve_` 线性扫槽
  （`request_scope.hpp:114-129`），每轮 harvest O(n²)（n=capacity）。iodepth=1024 时
  ~5×10^5 次 RequestId 比较每轮，亚毫秒级，接受；若实测成为热点，备选是绕过 scope
  自管 `Request<T>` + 哈希表（代价：自己重做容量预留与 settlement，偏离"最薄"原则，
  仅在有测量证据时启动）。
- **R5（环境）**：ring 引擎 dlopen 需运行时 `liburing.so.2`（本机有，REALITY §5）；
  系统 fio 的内置 io_uring 引擎可用性未验证（REALITY §8.4，对比臂注意）；
  governor=schedutil、btrfs zstd 压缩对正式测量的影响是测量阶段课题（REALITY §5）。
- **R6（Phase 1 遗留）**：`--output-format=json` 矩阵下的引擎行为、跨版本加载未验证
  （ABIQ §6.6）；`td->eo` 短名 dlopen 不依赖（§1）。
- **本阶段执行的检查**：除阅读/`nm -D`/`xmake f --help`/`grep` 类只读命令外，未运行
  任何构建或负载；§7 全部为设计。工具/引擎/作业文件均未创建。

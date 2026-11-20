# fio Adapter → RequestScope → RequestBridge → RequestCore 研究计数器

状态：**benchmark-local / research-only**。本文描述的实现只存在于本仓库的
研究构建目标与独立 Sluice worktree 中，不是 release 产物，也不构成产品
公共 API。

路径前缀：

- `B:` = `/home/jnhu/Source/sluice-workload-bench`
- `S:` = `/home/jnhu/Source/Sluice`（只读，HEAD `5d94f233967c3aef993eb19ba9e3252c532b0951`，本轮未改被跟踪文件）
- `R:` = `/home/jnhu/sluice-userspace-research`（由 `S:` 在 `5d94f233` 创建的独立 worktree；按设计为 dirty，未 commit/push）
- `E:` = `B:results/userspace/instrumentation-smoke-001`

## 1. 设计

计数器分两层，由同一个编译宏 `SLUICE_FIO_RESEARCH_COUNTERS` 门控：

| 层 | 位置 | 计数内容 |
|---|---|---|
| fio Adapter | `B:adapters/fio/src/{events,queue,engine,state}.cpp/.hpp`、新文件 `research_counters.hpp` | getevents/harvest/take/staged/submit 事件计数 |
| Sluice async core | `R:include/sluice/async/request_scope.hpp`、`R:include/sluice/async/detail/request_core.hpp`、`R:src/async/detail/request_core.cpp`、新文件 `research_counters.{hpp,cpp}` | RequestScope `resolve_` 扫描、`take_` 调用、RequestBridge `consume` 调用、RequestCore mutex lock 调用 |

Adapter 计数是每个 `EngineState` 的普通整数（引擎路径单线程，无需原子）。
Core 计数是进程级 `std::atomic<uint64_t>`（relaxed），因为 threadpool/uring
的提交、完成与回收可能跨线程。

### 1.1 Adapter 侧

- `B:adapters/fio/src/state.hpp:15` 在 `fio_cxx.hpp` 之前包含研究头文件；
  `:66-69` 在宏定义时给 `EngineState` 增加 `ResearchCounters research` 与
  `research_workers`。宏未定义时这两个成员不存在。
- `B:adapters/fio/src/research_counters.hpp:10` 是唯一的宏门；`:53-83` 生成
  一行 JSON；`:89-95` 处理 `SLUICE_FIO_RESEARCH_COUNTERS_FILE`。
- `B:adapters/fio/src/events.cpp:91-170` 在 `harvest()` 内计数；`:209-211`
  在 `sluice_getevents()` 入口计数。
- `B:adapters/fio/src/queue.cpp:93-101,120-122` 在 `submit_read/write/sync_*`
  调用前计数；`:78-80` 在 submit rejection 时计数。
- `B:adapters/fio/src/engine.cpp:151-154` 在 init 成功末尾记录 core 基线；
  `:168-174` 在 cleanup 入口、`finish()` 之前输出并写文件。

输出时机是 **cleanup 入口**：计数窗口是 `init 完成 → cleanup 开始`，不包含
`RequestScope::finish()` 的收尾结算。启动/收尾各一次，不是每次 I/O。

### 1.2 Core 侧（独立 worktree + research SDK）

`R:xmake/libraries.lua:52,56,62` 把研究头文件加入安装集、定义
`SLUICE_FIO_RESEARCH_COUNTERS=1`、把 `research_counters.cpp` 加入
`sluice_async`。

- `R:include/sluice/async/request_scope.hpp:122-131`：`resolve_()` 入口
  `scope_resolve_calls++`，循环每个 slot `scope_resolve_slot_iterations++`。
- `R:include/sluice/async/request_scope.hpp:145-149`：`take_()` 入口
  `scope_take_calls++`。
- `R:src/async/detail/request_core.cpp:735-739`：
  `RequestBridge::consume()` 入口 `bridge_consume_calls++`。
- `R:include/sluice/async/detail/request_core.hpp:23-37`：
  `ResearchCountingMutex` 在每次 `lock()` 进入时
  `core_mutex_lock_calls++`，生产构建仍是 `std::mutex`。
  `request_core.cpp` 内 31 处 `std::lock_guard<std::mutex>` 全部改为
  `std::lock_guard<CoreMutex>`（`grep -c 'lock_guard<CoreMutex>'` = 31，
  `lock_guard<std::mutex>` = 0）。

`core_mutex_lock_calls` 统计的是 **lock() 进入次数（尝试获取）**，不是等待
时间、不是竞争次数、也不包含 `RequestScope`（无锁）或 backend 的其他 mutex。

### 1.3 构建目标

`B:xmake.lua:188-244` 定义 research SDK 前缀
`build/ring-pic-research/sdk` 与构建函数；`:347-372` 定义
`fio-sluice-ring-research`。该目标：

- 使用与 `fio-sluice-ring` 相同的 adapter 源文件、`-O3`、PIC 与 ring profile；
- 额外定义 `SLUICE_FIO_RESEARCH_COUNTERS=1`；
- 链接 research SDK（其 `sluice_async`/`sluice_core` 由 `R:` 构建并安装）；
- 不替换、不改动 `fio-sluice-ring`、`fio-sluice-noring` 等 release 目标。

研究 SDK 构建前会校验 `R:` 的 HEAD 等于 `SLUICE_REVISION`，并校验
`R:include/sluice/async/detail/research_counters.hpp` 存在且含宏；不会因
worktree 的 tracked edits 拒绝构建（与生产 SDK 的 dirty 检查不同，这是
研究 worktree 的刻意例外）。`.ready` 戳为
`<revision>-research1`。

## 2. 计数定义

| JSON 字段 | 层 | 增量点 | 定义 | 建议分母 |
|---|---|---|---|---|
| `getevents_calls` | Adapter | `events.cpp:209` | fio 调用 `sluice_getevents` 的次数 | 每次 job |
| `harvest_calls` | Adapter | `events.cpp:92` | `harvest()` 进入次数（含轮询/健康结算路径） | getevents |
| `harvest_slots_examined` | Adapter | `events.cpp:98,168` | harvest 扫描循环体执行次数（含空槽与提前 `max` 停止前的槽） | completed IO |
| `take_calls` | Adapter | `events.cpp:108,137` | 对 `scope->take()` 的调用次数 | completed IO |
| `take_pending` | Adapter | `events.cpp:113,142` | take 返回 `pending` 的次数 | take_calls |
| `take_ready` | Adapter | `events.cpp:124,153` | take 返回 `ready`（完成并回收槽）的次数 | take_calls |
| `take_empty` | Adapter | `events.cpp:119,148` | take 返回 `empty`（票据有效但 scope 无槽）的次数；正常应为 0，非 0 触发 fail-closed | take_calls |
| `staged_completions` | Adapter | `events.cpp:129,158` | 写入 `staged` 的完成数 | completed IO |
| `max_staged` | Adapter | `research_counters.hpp:40-45` | 单次 getevents 内 `staged.size()` 的高水位 | 每次 job |
| `submit_read_calls` | Adapter | `queue.cpp:93-96` | `submit_read` 调用次数（含被拒） | completed IO |
| `submit_write_calls` | Adapter | `queue.cpp:93-98` | `submit_write` 调用次数 | completed IO |
| `submit_sync_calls` | Adapter | `queue.cpp:120-122` | `submit_sync_data/all` 调用次数 | completed IO |
| `submit_rejected_calls` | Adapter | `queue.cpp:78-80` | submit 返回错误并走 rejection 的次数 | submit_* |
| `core.scope_resolve_calls` | Core | `request_scope.hpp:122` | `resolve_` 调用次数 | take_calls |
| `core.scope_resolve_slot_iterations` | Core | `request_scope.hpp:127` | `resolve_` 线性扫描的 slot 迭代次数 | take_calls / completed IO |
| `core.scope_take_calls` | Core | `request_scope.hpp:145` | `take_<>` 调用次数 | take_calls |
| `core.bridge_consume_calls` | Core | `request_core.cpp:738` | `RequestBridge::consume` 调用次数 | take_calls |
| `core.core_mutex_lock_calls` | Core | `request_core.hpp:26` | `RequestCore::mutex_` 的 `lock()` 进入次数 | completed IO |

### 2.1 可验证不变量

正常读路径：

- `take_calls == take_pending + take_ready + take_empty`
- `core.scope_resolve_calls == core.scope_take_calls == take_calls`
- `core.bridge_consume_calls == take_calls`（仅当 `take_empty == 0`；empty 在
  `resolve_` 失败后不会调用 `take_result`）
- `staged_completions == take_ready`
- 只读 job：`submit_read_calls == staged_completions`
- `max_staged <= capacity == iodepth`
- `core.core_mutex_lock_calls` 应接近静态审计的 `9 + take_calls`
  （S: 审计 `9 + T`，T 为本次 getevents 的 take 次数；本轮 D16 实测
  `18.625 ≈ 9 + 9.500 = 18.500`，差 0.125 来自固定路径/初始化尾部）。

任何一个不变量失败，研究运行作废（见 `EXPERIMENT-PROTOCOL.md` §7）。

## 3. 输出格式

前缀固定为 `RESEARCH_COUNTERS `，随后一行 JSON，写 stderr；若设置了
`SLUICE_FIO_RESEARCH_COUNTERS_FILE`，同一行追加到该文件（每个 job 一行）。

D16 smoke 原文（`E:raw-semantics/research.counters`）：

```text
RESEARCH_COUNTERS {"schema":"sluice-fio-research/1","backend":"uring","iodepth":16,"workers":4,"getevents_calls":4082,"harvest_calls":5427,"harvest_slots_examined":55087,"take_calls":55069,"take_pending":50973,"take_ready":4096,"take_empty":0,"staged_completions":4096,"max_staged":15,"submit_read_calls":4096,"submit_write_calls":0,"submit_sync_calls":0,"submit_rejected_calls":0,"core":{"scope_resolve_calls":55069,"scope_resolve_slot_iterations":378690,"scope_take_calls":55069,"bridge_consume_calls":55069,"core_mutex_lock_calls":97741}}
```

`core` 字段是该 `EngineState` 的 init 基线与 cleanup 入口快照相减；但 core
计数器本身是进程级的。多 job/多线程并发时各 job 的窗口重叠，不能把行内
`core` 当作互斥归属；计数实验固定 `thread=1`、`numjobs=1`。

## 4. 构建与运行

### 4.1 一次性准备 worktree

```sh
git -C /home/jnhu/Source/Sluice worktree add \
    /home/jnhu/sluice-userspace-research 5d94f233
```

本轮的 core 改动已保存在 `R:`（未 commit）。若 worktree 被删除，需要按
`E:logs/core-instrumentation-diff.txt` 重放补丁，或把该 worktree 保留为
研究基线。

### 4.2 构建

```sh
# release（不受研究宏影响）
XMAKE_STATS=false xmake build -y fio-sluice-ring

# research：首次会自动构建/安装 research SDK 到 build/ring-pic-research/sdk
XMAKE_STATS=false xmake build -y fio-sluice-ring-research
```

`XMAKE_STATS=false` 只用于避免 xmake 统计脚本在无网络环境里挂起，不影响
编译产物。

研究 SDK 的独立构建命令（xmake hook 内部等价执行）：

```sh
xmake f -P /home/jnhu/sluice-userspace-research -m release \
    --liburing=y --cxxflags=-fPIC -y
xmake build -P /home/jnhu/sluice-userspace-research -j 20 -y sluice_async
xmake install -P /home/jnhu/sluice-userspace-research \
    -o /home/jnhu/Source/sluice-workload-bench/build/ring-pic-research/sdk \
    -y sluice_core sluice_async
```

### 4.3 运行

```sh
SLUICE_FIO_RESEARCH_COUNTERS_FILE=/tmp/counters.jsonl \
fio --name=d16 \
    --ioengine=external:/home/jnhu/Source/sluice-workload-bench/build/linux/x86_64/release/fio-sluice-ring-research.so \
    --filename=/path/to/1g-file --rw=randread --bs=4k \
    --size=1G --loops=4 --iodepth=16 \
    --direct=0 --invalidate=0 --thread=1 --sluice_backend=uring \
    --randseed=81 --output-format=json
```

固定大小 + `loops=4` 使计数器窗口与 fio 的 `read.total_ios` 完全对齐
（本轮 1,048,576 IO）；不要用 `time_based + ramp_time>0` 做分母，因为
计数器包含 ramp，而 fio 的 `total_ios` 不包含。

release 对照只需把 `--ioengine` 换成 `fio-sluice-ring.so`。

## 5. 语义不变证明

### 5.1 生产代码零差异（宏未定义）

用 `git archive HEAD adapters/fio/src` 得到 pristine 源，与工作树中门控宏
未定义的同一 TU，用 release 目标的实际编译 flags（`-O3 -DNDEBUG` 等，见
`E:logs/production-object-identity.txt`）分别编译，比较完整 `.o`：

| TU | pristine sha256 | gated sha256 | cmp |
|---|---|---|---|
| errno_map | `e4040e1b87087ef9c938915b9122f256819ac0ef5377ab59e8f0dfb97e16d4a8` | 同左 | IDENTICAL |
| files | `bee77883828f1d6046d2e1d445de837c11177421d6f4c3d6d487bca744614a3b` | 同左 | IDENTICAL |
| events | `7c2707d6271a8dd61816f3e8a3eedd2cf9345b6e62ebb247c1c4adabb5e8eb0c` | 同左 | IDENTICAL |
| queue | `2dccbc2b2f5f95428714f3b0d6b6b842d1e91ce4563774d433f8fec80cbe5e44` | 同左 | IDENTICAL |
| engine | `d01bb684e53970c6192aa75b1fce4003b28799878a86fcc0634ac7bb18a20816` | 同左 | IDENTICAL |

完整对象逐字节相同，且 gated 对象无任何 `research` 未定义符号。结论：
**`SLUICE_FIO_RESEARCH_COUNTERS` 未定义时，adapter 的五个 TU 与 HEAD 完全
一致**。

注意：整个 `.so` 的 `.text` hash 会变化，原因不是代码，而是 xmake glob 在
`adapters/fio/src/` 新增文件后改变了目标文件链接顺序（release 链接顺序从
旧的 `engine,errno_map,events,files,queue` 变为
`errno_map,files,events,queue,engine`）。逐 TU 对象相同是更强、且不受链接
顺序影响的零差异证据；把五个 gated 对象按旧顺序重新链接，所得 `.text`
sha256 为 `3d3b9ec449f5c982e243f360cfaf75a747b1226884a06a2b736f5db692c49ad6`，
与改动前 baseline 的 `.text` 完全一致（`E:logs/production-object-identity.txt`
末段）。release 与 research 本轮使用相同的链接顺序
（`errno_map,files,events,queue,engine`），因此扰动对照不受该顺序差异影响。

### 5.2 fio job 语义

同一 16 MiB 文件、`randread 4k size=16M iodepth=16 direct=1 randseed=81
uring`，release 与 research 各跑一次（`E:raw-semantics/`）：

| 项 | release | research |
|---|---|---|
| exit code | 0 | 0 |
| `read.io_bytes` | 16777216 | 16777216 |
| `read.total_ios` | 4096 | 4096 |
| `job.error` | 0 | 0 |
| `write_iolog` 中实际 IO 行数 | 4096 | 4096 |
| 按出现顺序归一化的 io_u 序列 sha256 | `4f7b9209…3e184ad8` | 同左 |

`write_iolog` 的时间戳列不同，比较时去掉时间戳，仅比较
`read|write|sync|trim offset length` 序列；`cmp` 结果相同。

错误行为：`--sluice_backend=bogus` 时两者均 exit 1，stderr 均为

```text
fio-sluice: invalid sluice_backend "bogus" (expected threadpool or uring)
fio: io engine sluice init failed.
```

运行期拒绝路径：`--rw=trim`（adapter 不支持的 ddir）两者均 exit 1，引擎行均为
`fio-sluice: ddir 2 is not supported`；research 仍输出一行零计数的
`RESEARCH_COUNTERS`（getevents/harvest/take/staged 全 0），说明拒绝发生在
queue 之前且没有伪造完成。

原文见 `E:raw-semantics/{release,research}.bad.err`、`.bad.exit`、
`.trim.err`、`.trim.exit`。

未运行完整既有测试套件；本轮语义门是上述同 job 对照 + 错误路径，不是
`tools/check_fio_engine.py` 或全部单元测试。

## 6. 扰动量化

D16 扰动 workload：1 GiB 热文件（`direct=0 invalidate=0`），
`randread 4k size=1G loops=4 iodepth=16`（每 rep 1,048,576 IO，无
`time_based`/ramp，计数器与 fio 的分母完全对齐），`taskset -c 0-9`，
`perf stat -e instructions:u,task-clock`。顺序 ABBA：
release r1 → research x1 → research x2 → release r2（`E:raw-perturbation/`）。

| run | engine | IOPS | total_ios | user instructions | instr/IO |
|---|---|---:|---:|---:|---:|
| r1 | release | 361,577.9 | 1,048,576 | 9,294,639,501 | 8,864.06 |
| x1 | research | 271,651.8 | 1,048,576 | 9,865,856,803 | 9,408.81 |
| x2 | research | 278,210.7 | 1,048,576 | 9,875,176,168 | 9,417.70 |
| r2 | release | 367,020.0 | 1,048,576 | 9,298,046,259 | 8,867.31 |

均值与差值（`E:logs/perturbation-summary.txt`）：

- release 均值：364,298.9 IOPS；9,296,342,880 instructions；8,865.68 instr/IO。
- research 均值：274,931.2 IOPS；9,870,516,486 instructions；9,413.26 instr/IO。
- **IOPS：research −24.53%**。
- **每 IO 指令：+547.57（+6.18%）**；两臂完成 IO 数相同，所以总指令也是
  +6.18%，总指令在这里不是独立口径。
- 两次重复的观测极差：IOPS release 1.49%、research 2.39%；instr/IO
  release 0.04%、research 0.09%。

机理解释（计数支持，不是单独因果证明）：x1 中
`scope_resolve_slot_iterations` 95,808,872 + `core_mutex_lock_calls`
19,529,248，合计 **109.995 次 lock 前缀原子/完成 IO**。每 IO 只多约 548
条用户态指令（+6.18%），但 lock 前缀原语的 cycles/instruction 远高于普通
ALU 指令；约 110 次原子/IO 与 −24.53% 的吞吐下降同向且量级相容。
`take_calls`/完成 IO = 9.500，`resolve` 迭代/take = 9.618（capacity 16），
`core_mutex_lock_calls`/完成 IO = 18.625，和静态审计的
`9 + T = 18.500` 相差 0.7%（U3.2 残差 0.125/IO）。

**不得把 research 构建的 IOPS 当作 release 性能。** research 构建只用于
计数；release 性能只用 release `.so`。本轮 release 两次的 IOPS 极差
1.49% 只说明这次短窗口内两 rep 较稳，不构成通用噪声底或精度声明。

## 7. 限制

1. Core 计数器是进程级；`EngineState` 只做窗口差值。`numjobs>1`、
   `thread>1` 或 fio 同进程多 job 时各行会重叠，禁止用于按 job 归因。
   计数实验固定 `thread=1`/`numjobs=1`。
2. 计数窗口止于 cleanup 入口，**不包含** `RequestScope::finish()` 的
   teardown 结算；因此 `core_mutex_lock_calls` 不是整个进程生命周期的锁次数。
3. `core_mutex_lock_calls` 是 `lock()` 进入次数，不是竞争次数、等待时间或
   临界区长度；没有 `try_lock`/futex 统计。
4. `scope_resolve_slot_iterations` 只数扫描迭代，不区分命中、空槽、缓存
   未命中；`scope_resolve_calls` 同时包含 `take_` 与 `wait_for` 的
   `resolve_`，本 adapter 不使用 `wait_for`，所以本轮两者相等。
5. 研究构建的原子计数器显著改变执行时间（本轮 −24.53% IOPS），不能用于
   性能结论；也不能用 research 的 instr/IO 直接外推 release 的每 op 指令。
6. `perf stat -e instructions:u` 只统计用户态；`perf_event_paranoid=2` 下
   无内核计数，无 sudo、未钉 governor/turbo。CPU 频率与机器负载未受控，
   只有 ABBA 顺序与 `taskset 0-9` 缓解。
7. 只构建了 ring 研究目标；noring 研究目标未构建、未验证。threadpool
   backend 的 core 锁竞争形态不同，本轮未测。
8. 研究 worktree 是 dirty worktree（未 commit），依赖其存在；研究 SDK 的
   `.ready` 戳只绑定 revision 与 `research1` 标记，不绑定每个源文件的哈希。
   若在 worktree 内继续改计数器，需要删除
   `build/ring-pic-research/sdk/.ready` 或整个前缀重建。
9. 完整 `.so` 的 `.text` hash 受 xmake glob 链接顺序影响；零差异结论以逐 TU
   完整对象比较为准（§5.1）。
10. 本轮没有放弃 core 计数，因此没有使用 `tickets_taken × capacity` 一类
    INFERRED 估算；所有 core 数字都是运行期计数。

## 8. 证据索引

- `E:manifest.json`：环境、二进制哈希、构建/运行命令、语义与扰动汇总。
- `E:raw-semantics/`：两次 smoke 的 fio JSON、stderr、iolog、counters、exit，
  以及非法 backend / 不支持 ddir 的错误路径原件。
- `E:raw-counts/`：M-COUNT 的 c1/c2（固定大小 1 GiB × loops=4）；两次计数
  逐字段相同，`staged_completions == fio.read.total_ios == 1,048,576`。
- `E:raw-perturbation/`：ABBA 四次 run 的 fio JSON、perf CSV、stderr、counters、exit。
- `E:superseded-ramp/`：pilot 的 `time_based + ramp_time=1` 运行；因计数器
  包含 ramp 而 fio `total_ios` 不包含，未用于 U1–U5，保留以说明分母选择。
- `E:logs/semantics-check.txt`：io_bytes/total_ios/顺序哈希比对。
- `E:logs/counts-summary.txt`：c1/c2 的不变量与 U1–U5 比值。
- `E:logs/perturbation-summary.txt`：IOPS/instructions/instr-per-IO 与计数派生比值。
- `E:logs/production-object-identity.txt`：五个 TU 的 pristine/gated 完整对象哈希。
- `E:logs/bench-instrumentation-diff.txt`、`E:logs/core-instrumentation-diff.txt`：
  本轮 adapter 与 worktree 的完整改动。

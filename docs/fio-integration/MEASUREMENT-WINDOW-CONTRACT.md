# 测量窗口与分母契约（Critical Gate）

状态：**预注册冻结**（正式测量开始前；本文之后不得因结果回看而修改阈值或口径）。

## 1. 问题

fio time_based + ramp_time 的 JSON 统计（IOPS/latency）只覆盖 ramp 后正式窗口；
`perf stat -- fio …` 的计数窗口覆盖整个进程生命周期（启动、文件打开、ramp、
正式窗口、teardown，含引擎线程）。因此

```text
perf instructions / fio JSON window IOPS 分母
```

**不自动成立**。本轮显式选择并实现下列口径，任何 per-op 数字必须携带其口径
标签。

## 2. 本轮口径：Option B（ramp=0 全进程生命周期）为主 + Option C（定长）交叉核验

**仪器语义实测修正（2026-11-19，session-000-smoke，正式测量开始前）**：fio
3.40 JSON 的 `read.io_bytes` 只覆盖 **ramp 后统计窗口**（实测 `io_bytes/bs ==
iops × runtime` 精确成立，ramp 期 I/O 不计入）。因此带 ramp 的 pmu 运行会
造成"PMU 窗口含 ramp 成本 / 分母不含 ramp I/O"的系统性错配（ramp=2s、
runtime=10s 时虚高 ~20%）。**修正后所有插桩模式（pmu/record/trace）一律
`ramp_time=0`**，使 io_bytes 覆盖进程完成的全部 I/O；clean 模式保留
ramp=2s 与 Phase 1 基线口径一致（clean 不做 per-op PMU 归一）。

### Option B — `PROCESS_LIFECYCLE_*`（主口径，ramp=0）

- PMU 计数窗口 = `perf stat` 包裹的整个 fio 进程树（fio 主线程 + 引擎线程；
  `taskset` 继承同一掩码）。
- I/O 分母 = 同一进程完成的全部 I/O 数 = fio JSON `jobs[0].read.io_bytes /
  bs`（ramp=0 时窗口即全运行）。旁证：fio stdout ios 计数。
- 输出标签：`PROCESS_LIFECYCLE_INSTRUCTIONS`、`PROCESS_LIFECYCLE_CYCLES`、
  `PROCESS_LIFECYCLE_CPU_TIME`（task-clock:u；rusage 旁证 user+sys）。
- 该数字是**生命周期摊销值**：含 fio 启动（进程创建、动态链接、job 解析、
  文件 open、引擎 init）与 teardown 的成本，不得表述为纯稳定态 per-op。
  启动/收尾占比由 Option C 界定（下）。

### Option C — `AMORTIZED_ESTIMATE`（交叉核验 cell）

- 同负载、`ramp_time=0`、非 time_based、`number_ios=`（定长）：短窗污染仅剩
  启动+teardown；io 数足够大时摊销误差 → 0。
- 以 Option C 与 Option B 的每 op 差估计 B 的启动/收尾占比，写入
  `window-contract.json`（`per_engine.*.delta_pct_of_C`、
  `startup_share_bound_pct`）。**逐引擎执行**（audit 修正：早期实现只对
  io_uring 计算）。若某引擎 |B−C|/C > 10%，该引擎的 per-op 结论降级为
  lifecycle-only，不外推稳定态；若差值方向与启动摊销相反（C2 更长却
  per-op 更高），按 mode/timing 效应处理而非启动污染，其 per-op 值携带
  该量级的跨模式不确定度。

### 拒绝项（本轮不使用）

- Option A（精确匹配窗口）：需要 fio 内部窗口信号或引擎内标记，二者都会
  改变被测语义（fio Engine 语义不可动、Sluice 不可加探针），`NOT_IMPLEMENTED`。
- fio JSON 正式窗口 IOPS 作 PMU 分母：**禁止**（窗口错配）。
- `fio commit()` 回调次数推断 `io_uring_enter` 次数：**禁止**（该回调只维护
  fio 提交统计，见 `adapters/fio/src/queue.cpp:22`）。enter 计数只能来自
  syscall 级观测（本轮 = strace -c 受控计数；root 可用时 = tracepoint 计数）。

## 3. 作用域标签（随每个数字旅行）

| 标签 | 含义 |
| --- | --- |
| `user` | `:u` 事件 —— 仅用户态执行（本机 paranoid=2 下非 root 唯一可用域） |
| `kernel` | `:k` 事件 —— 本轮非 root `UNAVAILABLE`，不得用 0 冒充 |
| `process` | fio 进程树全部线程（perf/rusage 天然作用域） |
| `io-wq/kworker` | 内核线程 —— 非 root `NOT_MEASURED`，判 `PARTIAL_CPU_ACCOUNTING` |
| `counted` | 精确计数（perf stat 非 multiplex；strace -c 计数） |
| `sampled` | perf record 采样归因 —— 只作函数级归因，不作精确 per-op 计数 |
| `lifecycle` | Option B 分母 |
| `fixed-count` | Option C 分母 |

## 4. multiplex / not-counted / 缩放规则

- perf stat `-x,` 输出的 `time-enabled/time-running < 100%` ⇒ 该事件标记
  `SCALED`，只作参考，不进 per-op 精确表。
- `<not counted>` ⇒ 该能力标记 `UNAVAILABLE`，绝不记 0。
- 主事件组保持 ≤3 硬件事件（Haswell 4 通用计数器），目标 100% running；
  辅助组（branches/branch-misses/cache）单独一组，接受 SCALED 标注。

## 5. 每指标口径速查

| 指标 | 来源 | 窗口 | 分母 |
| --- | --- | --- | --- |
| instructions/op | perf stat `instructions:u` | 进程生命周期 | lifecycle ios |
| cycles/op | perf stat `cycles:u` | 同上 | 同上 |
| CPU time/op | `task-clock:u`（= 进程树 user+sys，非 user-only；与 rusage(u+s) 精确吻合） | 同上 | 同上 |
| ctx switches/op | perf `context-switches` + rusage nvcsw+nivcsw | 同上 | 同上 |
| io_uring_enter/op | strace -c `io_uring_enter` 计数 | straced 进程 | 同 run 的 lifecycle ios |
| futex/op | strace -c `futex` 计数 | 同上 | 同上 |
| 函数热点占比 | perf record `cycles:u` 采样 | 采样窗口 | 采样样本数（`SAMPLED`） |
| IOPS/lat p50/95/99 | fio JSON（正式窗口） | ramp 后窗口 | fio 自身口径 |
| clat 与 lat | 分开取，clat 标注为完成延迟，非端到端 lat | fio 口径 | — |

## 变更记录（透明性声明；审计要求）

- **冻结件**：本会话预注册的冻结快照是 `PROTOCOL.md`（session-001，测量
  开始前生成）中内嵌的本文件副本；其公式记法为 `|B−C|/B`、字段名
  `startup_share_bound`。
- **测量后修订（本文件，16:05）**：① Option C 交叉核验由"仅 io_uring"扩展
  为**逐引擎**（`per_engine`）；② 差值公式字段统一为 `delta_pct_of_C`
  （以 C 为分母）并把 `startup_share_bound` 细化为
  `startup_share_bound_pct`（数值）；③ 明确 sluice/TP 的跨模式差按
  mode/timing 效应解释。**预注册阈值（10%）未变，任何门禁判定未因结果
  回看而调整**；该修订由独立审计发现并在本记录中显式声明。
- 首次正式测量前的冒烟会话发现 fio `io_bytes` 仅覆盖 ramp 后窗口，故
  插桩模式固定 `ramp_time=0` —— 此次修正发生在正式测量开始前（14:29
  冻结，14:31 首 run），不属于事后调整。

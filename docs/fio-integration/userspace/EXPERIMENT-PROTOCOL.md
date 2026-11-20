# 用户态计数实验协议（U1–U5 冻结）

版本：`userspace-protocol/1`，冻结日期 2026-11-20。
适用范围：`B:adapters/fio/src` 的研究计数器、`R:` 的 core 计数 SDK，以及
`fio-sluice-ring` / `fio-sluice-ring-research` 的 D16 对照。

本协议在运行前冻结 U1–U5 的定义、模式、顺序、AB/BA、A/A、分母、证据布局
与停止规则。运行后不得改定义迎合结果；新增比值只能标为 exploratory，不得
叫作 U1–U5。

## 1. U1–U5（冻结定义）

所有比值中的 `completed IO` 在只读 job 上等于 `staged_completions`，并与
fio 的 `read.total_ios` 交叉核对；`capacity` 等于 `iodepth`。

### U1 Adapter harvest 扫描

| ID | 公式 | 回答 |
|---|---|---|
| U1.1 | `harvest_slots_examined / staged_completions` | 每次完成平均扫过多少 slot |
| U1.2 | `take_calls / staged_completions` | 每次完成平均调用多少次 `take` |
| U1.3 | `take_pending / take_calls` | take 中 pending 的比例（非破坏扫描的重复探测） |
| U1.4 | `max_staged / capacity` | 单次 getevents 暂存高水位相对容量 |

### U2 RequestScope resolve 扫描

| ID | 公式 | 回答 |
|---|---|---|
| U2.1 | `core.scope_resolve_slot_iterations / take_calls` | 每次 take 的线性扫描长度 |
| U2.2 | `core.scope_resolve_slot_iterations / staged_completions` | 每次完成分摊的扫描迭代 |
| U2.3 | `core.scope_resolve_calls / core.scope_take_calls` | resolve 调用/take（本 adapter 无 `wait_for`，应为 1） |

### U3 RequestCore 临界区

| ID | 公式 | 回答 |
|---|---|---|
| U3.1 | `core.core_mutex_lock_calls / staged_completions` | 每次完成的 core mutex 获取次数 |
| U3.2 | `core.core_mutex_lock_calls / staged_completions - (9 + take_calls / staged_completions)` | 相对静态审计 `9 + T` 的残差 |
| U3.3 | `core.core_mutex_lock_calls / (9 + take_calls)` | 实测/静态预测比 |

`9` 来自 `S:` 审计的固定生命周期锁（提交 3 + 完成 6）；`T` 是本次 job 的
`take_calls / staged_completions`。U3.2/U3.3 只比较数量级，不把 `9` 当作
无条件常量。

### U4 consume / publication

| ID | 公式 | 回答 |
|---|---|---|
| U4.1 | `core.bridge_consume_calls / take_calls` | 每次 take 是否恰好一次 consume（`take_empty==0` 时应为 1） |
| U4.2 | `take_ready / take_calls` | take 命中 ready 的比例 |
| U4.3 | `take_pending / take_calls` | take 未完成、需下一轮的比例（与 U1.3 同值，保留为独立层口径） |
| U4.4 | `staged_completions / submit_read_calls` | 只读 job 的提交→交付守恒（应为 1） |

### U5 扰动包络

| ID | 公式 | 回答 |
|---|---|---|
| U5.1 | `(research IOPS − release IOPS) / release IOPS` | research 构建的吞吐扰动 |
| U5.2 | `research instr/IO − release instr/IO`，其中 `instr/IO = instructions_user / total_ios` | research 构建的每 IO 用户态指令增量 |
| U5.3 | release A/A 极差、research 两次极差 | 两次重复的观测噪声包络 |
| U5.4 | `(scope_resolve_slot_iterations + core_mutex_lock_calls) / staged_completions` | 研究构建新增的 lock 前缀原子密度（解释变量，不是因果证明） |

## 2. 模式（冻结）

| 模式 | 构建 | 用途 | 禁止 |
|---|---|---|---|
| M-SEM | release + research | 语义等价门：io_bytes、io_u 顺序、exit、错误路径 | 不用来比较性能 |
| M-COUNT | research | U1–U4 计数 | 不把其 IOPS/instr 当 release 性能 |
| M-PERF-REL | release | U5 的 release 臂；release 性能结论 | 不读 RESEARCH_COUNTERS |
| M-PERF-RES | research | 仅 U5 扰动的 research 臂 | 不发布为 release 性能 |
| M-AA | release 两次 / research 两次 | A/A 极差 | 不把两次重复当置信区间 |

同一模式内固定：`fio-3.40`、`taskset -c 0-9`、`thread=1`、`numjobs=1`、
`randseed=81`、`--invalidate=0`（缓冲读热缓存）、串行执行、run 间 `sleep 1`。
M-PERF 的 workload 固定为 `randread 4k size=1G loops=4 iodepth=16
direct=0 invalidate=0`（1,048,576 IO，无 `time_based`/ramp，使计数器窗口与
fio `total_ios` 对齐），文件在 run 前用 `cat file > /dev/null` 预热。

## 3. 顺序（冻结）

每个 session 严格按下列顺序执行，不并行 fio，不插队：

1. **构建与身份**：构建 release 与 research；记录两个 `.so` 的 SHA256、
   Build ID、`.text` size、`SLUICE_REVISION`、`R:` HEAD 与 dirty 状态。
2. **M-SEM**：release → research 各一次同一 smoke job；保存 fio JSON、
   stderr、`write_iolog`、exit code、counters。
3. **M-COUNT**：research `c1` → `c2` 两次，workload 与 M-PERF 相同的固定
   `size=1G loops=4`（只用于 U1–U4，不跑 perf）。
4. **M-PERF（ABBA）**：release `r1` → research `x1` → research `x2` →
   release `r2`；每 run 用 `perf stat -e instructions:u,task-clock` 包裹。
5. **解析与门禁**：先跑 §7 的停止规则；通过后才计算 U1–U5。
6. **落盘**：raw 文件不改写；`manifest.json` 记录哈希与命令。

跨 session 若重复 U5，下一次使用 BAAB（research→release→release→research）
以抵消顺序效应；同一 session 内固定 ABBA。U1–U4 的 M-COUNT 固定 XX
（research 两次），不使用 AB。

## 4. A/A（冻结）

- release A/A  = `r1` vs `r2`，同一 release `.so`。
- research A/A = `x1` vs `x2`，同一 research `.so`。
- 极差定义：`(max − min) / mean × 100%`，逐项报告 IOPS 与 instr/IO。
- A/A 是**两次重复的观测极差**，不是噪声底、不是最小可检测效应、不是
  95% 区间。
- 判读：只有引擎间差值大于 A/A 极差时才讨论方向；否则只报原始值。

## 5. 分母（冻结）

1. 完成侧主分母：`staged_completions`。只读 job 必须满足
   `staged_completions == fio.read.total_ios`。
2. 提交侧分母：`submit_read_calls`（只读 job 必须等于 `staged_completions`）。
3. 每次 take 的分母：`take_calls`。
4. 扫描归一化分母：`capacity == iodepth`。
5. `instr/IO` 的分母是 `fio.read.total_ios`，不是总 instructions、不是 wall
   time、不是 IOPS。
6. 禁止用 `tickets_taken × capacity` 或任何静态上界替代运行期
   `scope_resolve_slot_iterations`；本轮 core 计数已实现，不需要 INFERRED
   估算。

## 6. 证据布局（冻结）

```text
results/userspace/<session-id>/
  manifest.json
  raw-semantics/
    release.{json,err,iolog,exit,counters}
    research.{json,err,iolog,exit,counters}
    {release,research}.bad.{out,err,exit}      # 错误路径：非法 backend
    {release,research}.trim.{out,err,exit}     # 错误路径：不支持的 ddir
  raw-counts/
    c1.{json,err,exit,counters}
    c2.{json,err,exit,counters}
  raw-perturbation/
    r1.{json,perf,err,exit}
    x1.{json,perf,err,exit,counters}
    x2.{json,perf,err,exit,counters}
    r2.{json,perf,err,exit}
  logs/
    semantics-check.txt
    perturbation-summary.txt
    production-object-identity.txt
    bench-instrumentation-diff.txt
    core-instrumentation-diff.txt
```

规则：

- `session-id` 形如 `instrumentation-<purpose>-<NNN>`；本轮为
  `instrumentation-smoke-001`。
- `r*` 只表示 release，`x*` 只表示 research，`c*` 只表示 M-COUNT；不得混用。
- raw 文件是原件，不做二次格式化；汇总只写 `logs/`。
- `manifest.json` 必含：环境、revision、worktree HEAD/dirty、两个 `.so`
  的 SHA256/Build ID/`.text` size、构建命令、语义比对、U5 每 rep 数值、
  A/A 极差、停止规则判定、未控制项。
- 证据目录之外不复制 raw 数字进报告；报告中的数字必须能回指到本布局。

## 7. 停止规则（冻结）

任一条件触发即停止当前 session，不计算 U1–U5 或标记相应单元 INVALID：

| ID | 条件 | 处置 |
|---|---|---|
| S1 | stderr/stdout 出现任一 tripwire（`slot accounting bug`、`lost accepted request`、`previous completion batch…`、`staged completions were never consumed`、`still outstanding`、`queue before engine init`、`was never registered`、`submit threw`、`engine state missing`、`failing closed`、`request failed: code=`） | STOP |
| S2 | 任一 fio exit code ≠ 0（错误路径用例的预期 1 除外） | STOP |
| S3 | release 与 research 的 `io_bytes`、`total_ios` 或归一化 io_u 顺序哈希不同 | STOP |
| S4 | research 的 `RESEARCH_COUNTERS` 缺失、JSON 解析失败、或 stderr 与 counters 文件内容不一致 | STOP |
| S5 | 不变量失败：`take_calls != pending+ready+empty`；`scope_resolve_calls != take_calls`；`scope_take_calls != take_calls`；`bridge_consume_calls != take_calls`（`take_empty==0` 时）；`staged_completions != take_ready`；只读时 `submit_read_calls != staged_completions`；`staged_completions != fio.read.total_ios` | STOP |
| S6 | `max_staged > capacity` | STOP |
| S7 | `take_empty != 0` | STOP（引擎已 fail-closed，计数无意义） |
| S8 | release A/A 或 research A/A 的 IOPS 极差 > 10% | U5 INVALID；若 S1–S7 通过，U1–U4 仍有效但只作描述 |
| S9 | 系统异常（OOM、热降频、明显背景负载、文件缓存被驱逐） | 记录，U5 INVALID |
| S10 | 同一条件重试超过 2 次 | 该 session INVALID，不再重试 |

停止规则只用于有效性，不用于「结果不好看就重跑」。任何重试必须保留前一次
raw 并写进 manifest。

## 8. 判定与报告口径（冻结）

- U1–U4 是**描述性计数**：报告逐 rep 值、均值、比值；不做因果归因，不把
  静态审计的 `9 + T` 当作实测替代。
- U3.2 残差：`|residual| > 0.10 × (9 + T)` 时标 `UNEXPLAINED`；否则标
  `CONSISTENT_WITH_9_PLUS_T`。
- U5 只在 S1–S9 全部通过后判读：
  - 若 `|U5.1|` 大于 release A/A 与 research A/A 极差的较大者，报告为
    **OBSERVED_PERTURBATION**；否则 `WITHIN_TWO_REPEAT_SPREAD`。
  - U5.2 用 `instr/IO` 差值报告，不用总 instructions 差值；固定
    `size × loops` 下两臂完成数应相同，但仍以 `fio.read.total_ios` 归一化，
    防止任何完成数偏差混入。
  - 无论符号与大小，research 构建的 IOPS 一律不得写成 release 性能。
- n=2：只报逐 rep + 均值 + 观测极差；不报 p 值、不报 95% CI、不报
  「最小可检测效应」。
- 未控制项必须随结果声明：无 sudo、governor/turbo 未钉、
  `perf_event_paranoid=2`（仅用户态）、热缓存 `direct=0`、单 SATA SSD/
  btrfs 环境。

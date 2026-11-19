# fio-sluice 跨层成本归因结果（A–K 研究报告）

状态：**正式报告（r2，含独立审计后的降级修正）**。数据源：
`results/attribution/session-001/`（原始回执 + `sha256-manifest.txt` 全树
校验）；复算链：`tools/profile_fio_sluice.py analyze`（`analysis/`
下 `normalized-costs.json`、`window-contract.json`、`analysis.json`、
`runs-index.json`）。环境判词：**`PARTIAL_CPU_ACCOUNTING`**（非 root：
用户态 PMU 可用；kernel cycles / eBPF / io-wq CPU 不可用）。方法与预注册：
COST-ATTRIBUTION-METHOD.md（14:29 冻结，早于首个测量 run 14:31）；窗口与
分母：MEASUREMENT-WINDOW-CONTRACT.md。账本：COST-LEDGER.md。

本版已按独立 Evidence Auditor 与 Adversarial Reviewer 的意见降级了未经
支持的主张；修正记录见 §J。

## A. Repository & Build Reality

- bench `7a8b851`（研究分支自其切出）；SLUICE_REVISION
  `5d94f233967c3aef993eb19ba9e3252c532b0951`（仓内钉住 = 产品 checkout =
  78 个 run 的 engine load 行，analyze 强校验，零 mismatch）。
- fio `fio-3.40` 宿主（stripped，clean/pmu 基线）；诊断 fio 由冻结树
  `ff930c4` 构建（sha256 `a6d5c99b…`，见 binary-manifest.json）。
- 引擎 release stripped：ring `0b03514a…`、noring `21e41cf9…`；诊断双生
  ring-sym `31f5b867…`、noring-sym `94b99551…`。`.text` 段哈希逐字节相等
  （ring `3d3b9ec4…` / noring `9a599ada…`）→ record 归因的代码与计数基线
  相同。
- 环境：E5-2666 v3（Haswell-EP，10C/20T，单 NUMA）/ 内核
  `7.3.3-200.fc44`（#622 时为 7.2.11，全部能力本轮重测）/ btrfs
  compress=zstd:1 SATA SSD 热缓存 / taskset 0-9 / randseed=81。

## B. Instrumentation Capability（capability.json，全部实盘回执）

| 能力 | 状态 | 依据 |
| --- | --- | --- |
| instructions:u / cycles:u / task-clock:u | SUPPORTED | 单事件探测 100% time-running；session 主事件组 100% |
| branches:u / branch-misses:u / L1-dcache-loads:u / LLC-loads:u | SUPPORTED（counted） | aux 回执 |
| perf record 用户态采样（flat） | SUPPORTED | 6+6 个 record run，10K–39K samples，`Total Lost Samples: 0` |
| perf --call-graph dwarf/fp | **UNAVAILABLE** | 任何 callgraph 形式在 paranoid=2 下 "Failure to open any events"（perf 失败仍 exit 0，已按输出文本判定） |
| kernel cycles（cycles:k） | UNAVAILABLE | permission denied（paranoid=2；非 0） |
| context-switches / cpu-migrations（PMU） | **UNAVAILABLE（INVALID_ZERO_UNDER_EXCLUDE_KERNEL）** | perf 强制 exclude_kernel → 恒 0；切换数改用 rusage |
| bpftrace / eBPF / io_uring / sched tracepoints / io-wq 归因 | UNAVAILABLE | 需 root；sudo 需密码，未做权限提升 |
| strace -c（受控诊断） | SUPPORTED（扰动大） | syscall 计数用；不用于时延 |

判词：`PARTIAL_CPU_ACCOUNTING`。缺失项不以 0 冒充（测试守护）。

## C. Workload Identity & A/A

A1（4k randread D1）/ A2（4k D16）/ A3（64k seqread D4）/ A4（4k D4）+
psync D1 参照 + C2（A2 形态，io_size=20G 定长，5.24M ios）。同 fio 进程、
randseed=81、direct=0、invalidate=0、1G 专用文件（+C、psync 写+fsync、
全量预热读）、taskset 0-9。clean 3 reps；pmu 2 reps；record A1–A4 ×3 引擎；
trace A1/A2 ×3 引擎（短窗）。

A/A（clean 3 reps 极差）：native uring 0.6–4.1%；sluice-uring 0.8–5.3%；
TP 0.7–13.5%（A1 13.5% 最大）；psync 0.16%。按预注册 1/3 规则
（极差 > 引擎间差距的 1/3 ⇒ INCONCLUSIVE）：A3/sluice-uring 的 clean IOPS
排名触发该规则（见 analysis.json `comparisons`），其 PMU 指令差不受影响。
**clean 运行按固定顺序（每 cell 内 uring→su→TP）而非随机交错**，TP 的
时间敏感性使 clean IOPS 对照带顺序混杂；PMU 两臂在相邻时间窗内测量且
rep 间稳定（native ~0.1%，sluice ~0.2–0.3%），指令差受顺序影响小。

## D. Measurement Window & Denominator

- 主口径 `PROCESS_LIFECYCLE_*`：ramp=0，perf 窗口 = 整个 fio 进程树，
  分母 = 同一进程完成的全部 I/O（io_bytes/bs）。仪器语义经实测锁定：
  fio 3.40 的 io_bytes 只覆盖 ramp 后窗口（冒烟回执：io_bytes/bs ==
  iops×runtime 精确成立）。
- 交叉核验 C2（io_size=20G，ramp=0，clean×3/pmu×2 per engine，双 rep）：
  native io_uring 的 A2 vs C2 指令/op 差 **1.01%**（A2 2775.4 / C2 2747.7，
  <10% 预注册界）；sluice-uring **7.41%**（A2 8001.1 / C2 8641.2，两 rep
  内 <0.1% 一致）；TP **4.14%**（A2 7878.3 / C2 8218.8）。
- **关键限定**：native 的 1% 级差值验证了启动/收尾占比 ≤~1%；但
  sluice 的跨模式差更大且**方向与启动摊销相反**（C2 运行更长、分摊更小，
  per-op 却更高）→ 这是 mode/timing 效应而非启动污染。故 sluice 的
  per-op 值携带该量级的**跨模式不确定度**（≈7.4%），
  不是 1.01% 的精确窗。判词：`PROCESS_LIFECYCLE_PRIMARY_NATIVE_VERIFIED`。
- clean（ramp=2）的 rusage 与 io_bytes 窗口不匹配（rusage 含 ramp，
  io_bytes 不含）→ clean 行**不计算 rusage per-op**；窗口匹配的 rusage/op
  只来自 clean0 与 pmu 行（`CLEAN_RAMP2_LIFECYCLE_MISMATCH` 标注）。
- `task-clock:u` 的作用域实为**进程树总 CPU（user+sys）**——与 rusage
  user+sys 精确吻合（A2 sluice 2.694 vs 2.695 µs/op）；不含 io-wq/kworker。
  表头已改标 `task_total`。

## E. User-Space Cost Attribution

主表见 COST-LEDGER §1（counted，全 100% time-running）。要点：

- **全链差额（用户态）**：sluice-uring − native = A1 +3,682（+133%）、
  A2 +5,226（+188%）、A3 +3,259（+119%）、A4 +3,262（+117%）instr/op；
  cycles/op +2,435…+3,082；任务 CPU +0.70…+0.98µs/op。
- **层定位（SAMPLED，A2/D16 记录样本）**：libc mutex 29.8%、
  `RequestScope::take` 12.15%（A1 仅 2.44%）、dispatch_one_locked 3.69%、
  harvest 1.72%、handle_one_cqe 1.20%；A1：mutex 31.1%、sluice_queue 2.80%、
  take 2.44%、finalize 2.21%、consume 1.23%。native 对照的 top 符号全部是
  fio 自身机制，无 mutex。
- 归因边界：Δ 是 full-chain 差，**不能**拆成"RequestCore 指令数"；fio
  driver 增量（两臂 async 语义差异）无法单独扣除；Adapter 与 Core 的
  观察成本无法在无函数级计数的前提下独立切开（均入 SAMPLED 列表）。
- 深度信号：D16 的 Δ 比 D1/D4 高 1.5–2.0k instr/op，take 份额 2.4%→12.2%；
  **但 D4（+3,262）低于 D1（+3,682），非单调** → H5 降级
  `LIKELY_BUT_UNPROVEN`（仅 D16 抬升成立；D1 记录基线受 18% 扰动）。
- record 覆盖：A1–A4 ×3 引擎（A3/A4 补测后），A1 记录运行自身 IOPS 低
  18–24%（sluice/TP），A2 接近 clean（−0.7%/+1.7%）→ 采样对照以 A2 为准。

## F. Kernel & Scheduler Attribution

- **提交形状**：native 与 sluice 均 1.0000 enter/op（A1/A2 shape：
  native 65,536/65,536，sluice 65,536 次 flags=0 submit + 1 次
  min_complete=1 wait = 65,537/65,536）。native 的每次 enter 携带
  `IORING_ENTER_GETEVENTS`（提交+收割合并）；sluice 提交 flags=0、收割走
  用户态 peek。**"sluice 每 submit 即 flush" 不构成两臂差异（native 同形）。**
  注：strace 全量追踪把 IOPS 压到 bare 的 3.8–18.3%，触发预注册
  "<50% ⇒ 相关结论只能 INCONCLUSIVE" 规则 → 形状计数本身可用（65,536
  精确闭合），但"H2 无提交形状差异"的**结论**只能在结构证据（源码：
  fio io_uring 引擎逐 io_u submit；Sluice submit_transport_locked 逐 submit
  flush）下单列为 INCONCLUSIVE/LIKELY。
- **ThreadPool 握手**：futex 3.4–4.1/op（strace，受同一规则限制）；
  ctx-switch 0.9–2.5/op（rusage，窗口而异）；mutex/condvar 采样
  40–51%（A1 受扰基线，A2 接近 clean）；任务 CPU 14.5–42.8µs/op
  （窗口而异，pmu/clean0 较低）。跨线程执行机制方向坚实；
  "futex 是主成本"本身未证明（futex syscall 次数 ≠ 阻塞次数）。
- **内核侧**：cycles:k / io-wq / IRQ = UNAVAILABLE。不得声称 whole-system
  CPU/op。sys 时间两臂接近（A1 1.08 vs 1.16µs/op，rusage）——未见 sluice
  引入更多内核时间。
- 文件系统：单 FS（btrfs 热缓存）。#622 的 tmpfs/btrfs io-wq 条件依赖未在
  本轮重测；结论受单 FS 限制。

## G. Completion Causality（#623 K/R/P/O）

- K `NOT_OBSERVABLE`；R 部分（syscall 形态 + SAMPLED reap ~1.2%）；
  P `UNIDENTIFIABLE_WITH_CURRENT_INSTRUMENTATION`；O SAMPLED。
- join/事件丢失：无 eBPF 流；strace -c 对 syscall 计数穷尽；bpftrace
  lost-event 检测器与 JoinIntegrity 已实现并测试，但本轮无逐请求 join
  数据 → **未做任何逐请求时序主张**。`user_data`/fd 复用风险未触发
  （无 join）。

## H. Observer Perturbation

表见 COST-LEDGER §5（由 `render_cost_ledger.py` 从各模式自身 fio JSON
逐 run 生成，clean0=ramp=0 参照）。要点：uring 臂的 pmu/record 扰动在
±3.3% / ±3.9% 内（A1 sluice record −13.6% 例外）；**TP 高度敏感**
（A1 pmu −40.3%、record −35.9%；A3 pmu −33.6%；A2 pmu +11.7% 的反向
漂移同样说明其调度主导的方差）；strace 把 IOPS 压到 bare 的 3.8–18.3%。
TP 对观察者与顺序的强敏感性本身是"成本在跨线程调度而非指令条数"的
旁证（与 §F/§I 的 H1 证据同向，但同样不构成产品结论）。

## I. H1–H6 Hotspot Ledger

见 COST-LEDGER.md §4（r2 判定）：**H1** `LIKELY_BUT_UNPROVEN`（TP 握手
三源互证，futex 计数受 strace 规则限制）；**H2** `INCONCLUSIVE`（结构证据
LIKELY：两臂均 ~1.0 enter/op，均逐 io launch；strace 计数受 <50% 规则
限制）；**H3** `LIKELY_BUT_UNPROVEN`（Core 侧 take 12.15%@D16 等 SAMPLED
份额；adapter/core 观察成本未切开）；**H4** `INCONCLUSIVE`（0.2% 为
10K 样本检测限，非缺失证明）；**H5** `LIKELY_BUT_UNPROVEN`（D16 抬升成立；
take 份额随深度单调 2.44→4.17→12.15%，但指令差 D4<D1 非单调）；
**H6** `INCONCLUSIVE`（4k 与 64k 均无 copy 样本，但采样功效不足）。

## J. Independent Review

两名独立 agent（未参与实现）分别执行了证据审计与对抗复核，全部主要
指控已并入本 r2 报告：

- Evidence Auditor：`sha256sum -c` 原始 run 文件 537/537 通过；6 个会话级
  元数据文件在早期 manifest 后发生漂移（已在 r2 末尾整树重生成并复验）；
  独立复算 6 个数字 5 项精确一致、1 项指出 clean ramp 分母错配（已修正为
  `CLEAN_RAMP2_LIFECYCLE_MISMATCH`，rusage/op 只取 ramp=0 网格）；
  二进制 `.text` 同一性复核通过；无秘密泄漏；perf.data 3.68MiB 全部
  git-ignored。
- Adversarial Reviewer（12 项证伪清单）：REFUTED 4 项（enter 数更高、
  io-wq 会计完整、#622 无条件迁移、减少指令必然提升 IOPS——后者由同
  session 的 A2/A3 TP 反例支持）；INCONCLUSIVE 5 项；NOT_TESTED 3 项。
  其最关键意见——窗口界只对 native 成立、clean rusage 窗口错配、
  perturbation 表为空、H5 非单调、strace <50% 规则、H4/H6 检测限——已
  全部体现在 r2 的 §C–§I 与 COST-LEDGER 中。
- 仍存在争议/未闭合：C2 模式差 7.4% 的机制（NOT_IDENTIFIED）；D1→D4→D16
  非单调性的定量分解；顺序混杂对 clean IOPS 的影响（无逐 run 温度/频率
  回执，已声明）；TP 的 pmu IOPS 反向漂移（+11.7%）原因。

## K. Final Owner Gate

**`ATTRIBUTION_PARTIAL`**——用户态（instructions/cycles/CPU-time/branch/
cache）的成本事实成立且独立复算通过；内核/io-wq/eBPF 明确 `UNAVAILABLE`
（`PARTIAL_CPU_ACCOUNTING`）；至少一项热点（H1 ThreadPool 握手；H5 的
D16 take/harvest 抬升）得到独立归因证据，但按预注册规则与采样功效，
它们停在 `LIKELY_BUT_UNPROVEN`，不构成产品优化准入。

- **工具交付**：`tools/profile_fio_sluice.py` + `tools/attribution/`（probe/
  clean/clean0/pmu/record/trace/crosscheck/analyze，薄编排层）+ 20 项测试
  （覆盖 12 项强制用例）；证据树可整树复算、哈希可独立校验。
- **产品决策**：零产品结论。未触碰 #604 G0、#595（KEEP_IMMEDIATE）、
  #617、#621、#623；未修改 Sluice/fio 语义。热点账本仅作为 #639 的
  事实输入：H1/H5 `LIKELY_BUT_UNPROVEN`，H2/H4/H6 `INCONCLUSIVE`，
  H3 `LIKELY_BUT_UNPROVEN`。任何进入 G2 的提议都需 Owner 另行授权与
  单因素反例；本轮停在 Research Owner Gate。
- **重开条件**：root 会话（kernel cycles/eBPF/io-wq）、C2 模式差机制、
  D4 采样基线重测、tmpfs 对照、真实存储/冷缓存。#622 方法边界继续有效：
  不建新 tracing 引擎；残余缺口均为环境/产品 API 形状问题。

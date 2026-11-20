# fio-sluice 跨层成本归因结果（A–K 研究报告）

状态：**正式报告（r3；§A–§J 为 r2/session-001 主体，§I-R 为
session-002-root 增补并修订判定，§K 为合并最终判词）**。r2 判词仅作历史，
最新判词以 §I-R.4、§K 与 COST-LEDGER §8.4 为准。数据源：
`results/attribution/session-001/`（208 个 run dir；785 条哈希记录，磁盘
786 文件）与 `results/attribution/session-002-root/`（74 个 run dir；
311 条哈希记录，磁盘 312 文件）；两树均有 `sha256-manifest.txt` 全树
校验。复算链：`tools/profile_fio_sluice.py analyze`（`analysis/` 下
`normalized-costs.json`、`window-contract.json`、`analysis.json`、
`runs-index.json`）。环境判词：session-001
**`PARTIAL_CPU_ACCOUNTING`**（非 root：用户态 PMU 可用；kernel cycles /
eBPF / io-wq CPU 不可用）；session-002-root
**`FULL_FOR_MEASURED_SCOPES`**（定义见 §B；capability 子项
`perf_callgraph_user=PARTIAL`、`cpu_migrations` 探针为假阴性、
`io_wq_worker_attribution=UNAVAILABLE`）。方法与预注册：
COST-ATTRIBUTION-METHOD.md（14:29 冻结，早于首个测量 run 14:31）；窗口与
分母：MEASUREMENT-WINDOW-CONTRACT.md。账本：COST-LEDGER.md。

本版已按独立 Evidence Auditor 与 Adversarial Reviewer 的意见降级了未经
支持的主张；修正记录见 §J。**H5 当前判词（r3）为
`HOTSPOT_OBSERVED / CAUSAL_COST_NOT_ISOLATED`；§E/§I 中的
`LIKELY_BUT_UNPROVEN` 是 r2 历史记录，§I-R 与 COST-LEDGER §8.4 为当前
判词。**

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

判词（session-001，非 root）：`PARTIAL_CPU_ACCOUNTING`。缺失项不以 0
冒充（测试守护）。

**session-002-root（Owner 现场授权 sudo，密码仅经 stdin）**：capability
判词 **`FULL_FOR_MEASURED_SCOPES`**——kernel cycles
（cycles:k/instructions:k 100% running）、bpftrace/eBPF、io_uring/sched
tracepoints 均 SUPPORTED；`perf record --call-graph dwarf|fp` 实跑成功
（A2 sluice dwarf 10,178 samples、perf 未报告 lost；kernel fp 10,053
samples、perf 未报告 lost——均非零丢失证明），但 capability 子项
`perf_callgraph_user=PARTIAL`（探针符号解析），
不是“全部 SUPPORTED”。`context-switches` 计数有效；`cpu-migrations` 的
capability 探针记为 UNAVAILABLE（sleep 探针 0 次迁移，属假阴性），而 g3
实跑 raw 回执有 42–87 次、100% time-running，故表中 migr/op 只作旁证。
`io_wq_worker_attribution` 仍为 UNAVAILABLE（worker key 仅系统级 comm
匹配，不归属本 run）。两 session 独立归档，不混合。

> **判词定义（本仓首次出现，后文一律引用）**
> `FULL_FOR_MEASURED_SCOPES`：只在**已测 scope 集合**内成立——`:u` PMU 与
> 进程树 `:k` PMU 的 counted 值、eBPF 聚合计数、perf 调用栈（SAMPLED）。
> 它**不**是 whole-system 全成本，**不**是逐请求因果闭合，**不**表示
> "CPU 账本已完整"。以下 scope 在 root 会话中仍未闭合：
> 1. **外部 io-wq/kworker 与该 fio run 的唯一归属**：`capability.json` 的
>    `io_wq_worker_attribution` 仍判 UNAVAILABLE（该行在 capability.py 中
>    是固定条目）；eBPF worker key 只是系统级 comm 匹配，不归属本 run
>    （§I-R 第 2 条）；进程树 `:k` PMU 也不覆盖其它 task 的内核执行。
>    本轮 punt 回执为每 run 1 次只说明 btrfs+NODATACOW 热缓存路径未进入
>    稳态 io-wq，不等于具备归属能力。
> 2. **IRQ/softirq 等中断侧系统成本**：`tools/attribution/capability.py`
>    未做任何探测、无回执；进程树 `:k` PMU 不含中断上下文。
> 3. **K/R/P/O 逐请求因果关联**：eBPF 只有聚合 per-op 计数，
>    `report.py` 的 BPF 行固定 `join = "NOT_ATTEMPTED (aggregate counts
>    only)"`。
> 因此本判词**不得**被引用为 whole-system full causal attribution，也不得
> 据此声称两臂的"全部 CPU 成本"已经对齐。历史回执
> `results/attribution/session-002-root/capability.json` 与其同 manifest
> 下的 `analysis/analysis.json` 的 `cpu_accounting_verdict` 字面值仍为
> `"FULL"`（不追改、不重算改写），一律按本段定义解读。

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

- 主口径 `PROCESS_LIFECYCLE_*_per_op`：窗口 = `ramp_time=0` 的整个 fio
  进程生命周期，分母 = 同一 run 的 `lifecycle_ios`
  （`read.io_bytes/bs`）；每个 per-op 行都要求 run 自身 job text 的
  `ramp_time=0` 回执，否则 REFUSED。
- 交叉核验 C2（io_size=20G，ramp=0，clean×3/pmu×2 per engine，双 rep）：
  native io_uring 的 A2 vs C2 指令/op 差 **1.01%**（A2 2775.4 / C2 2747.7，
  <10% 预注册界）；sluice-uring **7.41%**（A2 8001.1 / C2 8641.2，两 rep
  内 <0.1% 一致）；TP **4.14%**（A2 7878.3 / C2 8218.8）。
- **关键限定**：native 的 1.01% 只是 **native 臂的总跨模式差上界**，不是
  “启动/收尾 ≤~1%”的证明，**更不是 sluice/TP 的精度**：`AMORTIZED_ESTIMATE`
  只对 native 交叉核验成立。C2 同时改变时间基准，差值不能单独识别启动
  成本：native C2 pmu run 更短（8.542/8.849s vs A2 10s）却 per-op 更低；
  sluice-uring C2（14.139/14.029s）与 TP C2（22.315/22.377s）更长却
  per-op 更高——三引擎方向均与启动摊销相反。故机制统一记为
  `MODE_OR_TIMING_EFFECT_NOT_IDENTIFIED`，**不得解释为启动/收尾成本**；
  三者分别携带 ≈1.0% / ≈7.4% / ≈4.1% 的跨模式不确定度，没有精确的同口径
  per-op 数据，sluice/TP 的 per-op 结论明确降级为 lifecycle-only，不外推
  稳定态。判词 `PROCESS_LIFECYCLE_PRIMARY_NATIVE_VERIFIED` 只表示 native
  的 |A2−C2|/C ≤10% 门禁通过，不表示启动占比或机制已验证。
- clean（ramp=2）的 rusage 与 io_bytes 窗口不匹配（rusage 含 ramp，
  io_bytes 不含）→ clean 行**不计算 rusage per-op**；窗口匹配的 rusage/op
  只来自 clean0 与 pmu 行（`CLEAN_RAMP2_LIFECYCLE_MISMATCH` 标注）。
- `task-clock:u` 的作用域实为**进程树总 CPU（user+sys）**——与 rusage
  user+sys 精确吻合（A2 sluice 2.694 vs 2.695 µs/op）；不含 io-wq/kworker。
  报告表头统一标 `cpu-us/op (task total)`，不再用 `(u)` 暗示 user-only。

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
- 深度信号：D16 的 Δ 比 D1/D4 高 1.5–2.0k instr/op，take 份额
  2.4%→12.2%；**但 D4（+3,262）低于 D1（+3,682），非单调** → r2 据此把
  H5 降为 `LIKELY_BUT_UNPROVEN`（仅 D16 抬升成立；D1 记录基线受 18%
  扰动：343,897 vs 421,080 IOPS，慢 18.33%）；r3 当前判词见 §I-R /
  COST-LEDGER §8.4（`HOTSPOT_OBSERVED / CAUSAL_COST_NOT_ISOLATED`）。
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
  flush）下单列为 INCONCLUSIVE/LIKELY；r3 的非扰动 tracepoint 计数
  （1.0000 enter/op + 1.0000 submit_req/op）已覆盖该限制。
- **ThreadPool 握手**：futex 3.4–4.1/op（r2 strace，受同一规则限制；r3
  eBPF 3.00/6.15 见 §I-R.2）；ctx-switch 0.84–2.00/op（clean0/pmu，
  窗口而异）；mutex/condvar 采样 40–51%（A1 受扰基线，A2 接近 clean）；
  任务 CPU 14.0–40.1µs/op（pmu 逐 rep；主表均值 14.1–37.3µs/op）。
  跨线程执行机制方向坚实；"futex 是主成本"本身未证明（futex syscall
  次数 ≠ 阻塞次数）。
- **内核侧（session-001 非 root 边界）**：cycles:k / io-wq / IRQ =
  UNAVAILABLE。不得声称 whole-system CPU/op。窗口匹配的 clean0 rusage
  显示 sys 时间两臂接近（A1 0.90 vs 1.00µs/op；A1 clean ramp=2 的
  1.08/1.16 属 `CLEAN_RAMP2_LIFECYCLE_MISMATCH`，不得作 per-op）——未见
  sluice 引入更多内核时间。root 会话只解除了 `cycles:k`（进程树内核态）；
  **io-wq/kworker 归属与 IRQ/softirq 仍未闭合**（判词定义见 §B）。
- 文件系统：单 FS（btrfs 热缓存）。#622 的 tmpfs/btrfs io-wq 条件依赖未在
  本轮重测；结论受单 FS 限制。

## G. Completion Causality（#623 K/R/P/O）

- K `NOT_OBSERVABLE`（r2/session-001；root 见 §I-R.2：`io_uring_complete`
  计数 cqe/op=1.000，但仍无逐请求时间戳）；R 部分（syscall 形态 +
  SAMPLED reap ~1.2%）；P `UNIDENTIFIABLE_WITH_CURRENT_INSTRUMENTATION`；
  O SAMPLED。
- join/事件丢失：session-001 无 eBPF 流（session-002-root 见 §I-R.5）；
  strace -c 对 syscall 计数穷尽；bpftrace lost-event 检测器与
  JoinIntegrity 已实现并测试，但本轮无逐请求 join 数据 → **未做任何
  逐请求时序主张**。`user_data`/fd 复用风险未触发（无 join）。root 会话
  虽有 io_uring/sched tracepoint 与 `:k` PMU，但只产出聚合 per-op 计数
  （`join = NOT_ATTEMPTED`）→ **K/R/P/O 的逐请求因果关联仍未闭合**，不随
  判词升级而闭合。

## H. Observer Perturbation

表见 COST-LEDGER §5（由 `render_cost_ledger.py` 从各模式自身 fio JSON
逐 run 生成，clean0=ramp=0 参照）。要点：uring 臂的 pmu/record 扰动在
−2.1%…+3.9% 内（A1 sluice record −13.6% 例外）；**TP 高度敏感**
（A1 pmu −40.3%、record −35.9%；A3 pmu −33.6%；A2 pmu +11.7% 的反向
漂移同样说明其调度主导的方差，A2 record +1.5%）；strace 把 IOPS 压到
bare 的 3.8–18.3%。TP 对观察者与顺序的强敏感性本身是"成本在跨线程
调度而非指令条数"的旁证（与 §F/§I 的 H1 证据同向，但同样不构成产品
结论）。

## I. H1–H6 Hotspot Ledger

见 COST-LEDGER.md §4（**历史 r2 判定**；r3 最新判词见本文件 §I-R.4 与
COST-LEDGER §8.4）：**H1** `LIKELY_BUT_UNPROVEN`（TP 握手三源互证，
futex 计数受 strace 规则限制）；**H2** `INCONCLUSIVE`（结构证据 LIKELY：
两臂均 ~1.0 enter/op，均逐 io launch；strace 计数受 <50% 规则限制）；
**H3** `LIKELY_BUT_UNPROVEN`（Core 侧 take 12.15%@D16 等 SAMPLED 份额；
adapter/core 观察成本未切开）；**H4** `INCONCLUSIVE`（0.2% 为 10K 样本
检测限，非缺失证明）；**H5** `LIKELY_BUT_UNPROVEN`（D16 抬升成立；take
样本份额 2.44→4.17→12.15%，但指令差 D4<D1 非单调；r3 当前判词见
§I-R.4）；**H6** `INCONCLUSIVE`（4k 与 64k 均无 copy 样本，但采样功效
不足）。

## I-R. Root 会话结果（session-002-root；补充数据并修订判定，§I 的 r2
判词已被 §I-R.4 取代）

数据：`results/attribution/session-002-root/`（74 个 run dir；runs-index
74，其中 13 个失败/作废回执保留并排除；`sha256-manifest.txt` 311 条
记录全通过，磁盘共 312 文件；capability `FULL_FOR_MEASURED_SCOPES`，
子项限制见 §B）。

**窗口限定（audit）**：root 会话只有 A1/A2 的 `pmu-root-*` 生命周期计数，
**没有 C2 定长交叉核验**（`analysis/window-contract.json` 的
`per_engine={}`、verdict=`LIFECYCLE_ONLY_NO_STEADY_STATE_EXTRAPOLATION`；
归档 JSON 的 `option_B=absent` 是旧检测器只匹配字面 `pmu` 的缺陷，修复后
代码把 `pmu-root-*` 计为 Option B=present，但本会话仍无 Option C）。
故本节 per-op 数字与 §D 同窗口口径（`PROCESS_LIFECYCLE_*_per_op`：
`ramp_time=0`、分母 = 同 run 的 `lifecycle_ios`），但**没有启动/收尾占比
上界**，只作 lifecycle-only，不得外推稳定态，也不得把 session-001 的
native 1.01% 当作本会话精度。root 行保留原始 event 键，并带 `label` 映射
到同一 `PROCESS_LIFECYCLE_*` 标签；`instr:u`/`cyc:u` 为 `user`，
`instr:k`/`cyc:k` 为 `kernel`，`task-clock` 为 `task_total`（user+sys）。

1. **用户/内核分域（counted）**：A2 native 2,776 u + 2,676 k instr/op、
   1,438 u + 4,270 k cyc/op；sluice-uring 7,993 u + **2,655 k** instr/op、
   4,530 u + **4,608 k** cyc/op。**内核侧两臂几乎等价（+8–9% cycles），
   用户态 +5.2k instr/op 的差距没有内核侧膨胀**——层归属因此确定：
   差距在用户态 Sluice/Adapter。TP：9.4k–15k k-instr、16.9k–25.4k
   k-cycles/op（futex 握手）。
2. **eBPF（comm 聚合；HOST_ISOLATED_AGGREGATE）**：两 uring 臂
   1.0000 enter/op、1.0000 submit_req/op、1.0000 cqe/op（cqe 现按
   `comm=="fio"` 过滤；修复前为全 comm 求和，含 iou-wrk）；**punt：
   native 0，sluice-uring 每 run 1 次**（`@punt[fio]: 1` +
   `@cqe[iou-wrk-*]: 1`，逐 op≈0——不能写作“无 io-wq punt”）；TP futex
   3.00/6.15 per op（TP 的 io_uring 键结构性不适用，不进入 per-op）；
   fio on-CPU dwell 2.1–3.1µs/op（uring）、
   15.4–28.0µs/op（TP），为 comm=fio 全部线程之和（thread=1：job 是
   fio 进程内的线程，TID≠TGID），不是提交 TID 的单线程时间。计数无
   pid/tgid 绑定，隔离仅来自 run 前 `pgrep -x fio` 门禁；io-wq worker
   key 为系统级 comm 匹配，**不归属本 run**。**窗口核验**：
   `bpf_attach_s + wall_s <= bpf_window_s` 是 `EBPF_COUNTED` 的必要条件，
   超窗记 `EBPF_WINDOW_OVERSHOOT`（下界）；accepted run 为重试时不再
   复制第一次 attempt 的计数（`bpf_retry_outside_window=true` →
   `EBPF_UNAVAILABLE`）。本会话旧 run 未持久化 `bpf_window_s`，按 job
   `runtime+ramp+4=14s` 重建；`wall_s≈10.39s` + 1s attach allowance
   通过，标 `window_basis=RECONSTRUCTED_FROM_JOB` /
   `attach_basis=ASSUMED_1S`。
3. **调用栈（A2 sluice，dwarf，10,178 samples，perf report 显示 10K，
   0 lost〔perf 无报告，非零丢失证明〕；root 会话只有 A2 的 dwarf 调用树，
   A1/sluice-uring 无 `record-root` 回执，故调用树本身不能证明深度依赖）**：
   `sluice_getevents` 44.42%（harvest→take→consume 27.63/25.97/15.52%；
   poll→reap→handle_one_cqe 16.36/8.40/7.59%；publish_one 5.48%）、
   `sluice_queue` 24.25%（submit_read 21.81% → dispatch 9.75%）；平坦
   mutex 29.0%。内核栈：syscall 进出 ~10%、filemap 读 ~8%、fget/fput
   ~4.7%、xas_load 3.1%——两臂共享路径。
4. **判定修订（r3，详见 COST-LEDGER §8.4）**：H1 `ATTRIBUTED（限定）`、
   H2 `NOT_EXPOSED`、H3 `ATTRIBUTED（SAMPLED）`、H4/H6 保持
   `INCONCLUSIVE`；**H5** `HOTSPOT_OBSERVED / CAUSAL_COST_NOT_ISOLATED`
   （原 r3 标签 `ATTRIBUTED（D16 限定）` 仅指函数级热点归属：D16 的 take
   样本份额可定位；采样份额是 run 内占比而非 per-op 函数计数，
   D4<D1 非单调未闭合，容量扫描导致的净成本与优化收益均未证明）。
   可优化性判断仍被 #595/#604/#579 门禁阻断，本轮不提出任何优化。
5. **丢失控制**：早期 trace-bpf 尝试在 TP 上触发 bpftrace 丢事件
   （现场记录 206,430/1,452,829；该批回执未归档，当前树不可复算）并被
   重测取代；分析器现对丢事件 run 输出 `EBPF_LOSSY`（CAPTURE_DROPPED；
   计数=下界、dwell 无效），有测试。重试 run 不再复制第一次 attempt 的
   bpftrace 回执；`bpf_retry_outside_window=true` 的 run 记
   `EBPF_UNAVAILABLE`。**“无 Lost N events 报告”≠“零丢失”**：
   attach 前、退出信号后、attach 失败与 dwell 的逐 CPU map 竞争均不在该
   计数器内（6 个回执的 stderr 均含 `Can't delete map element`，即至少
   一次 switch-out 未匹配到 `@start`）。

## J. Independent Review

两名独立 agent（未参与实现）分别执行了证据审计与对抗复核，全部主要
指控已并入本 r2 报告：

- Evidence Auditor：`sha256sum -c` 原始 run 文件 537/537 通过；6 个会话级
  元数据文件在早期 manifest 后发生漂移（已在 r2 末尾整树重生成并复验）；
  独立复算 6 个数字 5 项精确一致、1 项指出 clean ramp 分母错配（已修正为
  `CLEAN_RAMP2_LIFECYCLE_MISMATCH`，rusage/op 只取 ramp=0 网格）；
  二进制 `.text` 同一性复核通过；无秘密泄漏；原始 perf.data 未入 git
  （session-001 合计 8.46MiB / 12 个非空文件；session-002-root 合计
  482.5MiB / 5 个非空文件，最大单文件约 316MiB），仅存在于本地
  `results/`（`.gitignore` 的 `/results/`）；`sha256-manifest.txt` 同时
  收录 `perf.data` 与 `perf.data.sha256` 侧车，缺失本地树时只能重跑
  对应会话（root 数据需 sudo），不能从 git 恢复原始 perf.data。
- Adversarial Reviewer（12 项证伪清单）：REFUTED 4 项（enter 数更高、
  io-wq 会计完整、#622 无条件迁移、减少指令必然提升 IOPS——后者由同
  session 的 A2/A3 TP 反例支持）；INCONCLUSIVE 5 项；NOT_TESTED 3 项。
  其最关键意见——窗口界只对 native 成立、clean rusage 窗口错配、
  perturbation 表为空、H5 非单调、strace <50% 规则、H4/H6 检测限——已
  全部体现在 r2 的 §C–§I 与 COST-LEDGER 中。
- 仍存在争议/未闭合：C2 模式差 7.4% 的机制（`MODE_OR_TIMING_EFFECT_
  NOT_IDENTIFIED`）；D1→D4→D16 非单调性的定量分解（D4<D1，且 D16 两会话/
  口径的份额差 1.70 个百分点与 D1→D4 的 1.73 个百分点同量级，三点
  “单调”趋势不可外推）；顺序混杂对 clean IOPS 的影响（无逐 run 温度/
  频率回执，已声明）；TP 的 pmu IOPS 反向漂移（+11.7%）原因。

## K. Final Owner Gate

**`ATTRIBUTION_VALID`（跨两层会话合并判词）**——session-001（非 root）
提供用户态完整归因与 `PARTIAL_CPU_ACCOUNTING` 边界；session-002-root 在
**已测 scope** 内补齐证据（`FULL_FOR_MEASURED_SCOPES`，定义见 §B）：
kernel/user 分域 counted、非扰动 eBPF 聚合计数（enter/submit/
punt/cqe/futex）、dwarf 调用栈与内核栈。关键热点获得限定范围的热点定位
（H1 握手、H3 观察路径、H5 D16 函数级热点；H2 形状等价由 counted 证据
否定差异）；H5 的样本份额上升不构成容量扫描的因果净成本证据。
数据口径（窗口/分母/丢失）可信。**注意：ATTRIBUTION_VALID 只描述归因
数据，不等于任何优化被批准；H4/H6 仍 INCONCLUSIVE，可优化性未评估。**
未闭合 scope 与 §B 相同：io-wq/kworker 归属、IRQ/softirq、K/R/P/O 逐请求
因果；本判词不构成 whole-system 全成本结论，也不是"CPU 账本完整"的证明。

- **工具交付**：`tools/profile_fio_sluice.py` + `tools/attribution/`（probe/
  clean/clean0/pmu/record/trace/crosscheck/analyze，薄编排层）+ 72 项测试
  （64 项 r3 基线 + 8 项本轮审计修复）
  （其中 12 项强制用例）；证据树可整树复算、哈希可独立校验。
- **产品决策**：零产品结论。未触碰 #604 G0、#595（KEEP_IMMEDIATE）、
  #617、#621、#623；未修改 Sluice/fio 语义。热点账本仅作为 #639 的
  事实输入（r3）：H1 `ATTRIBUTED（限定）`、H2 `NOT_EXPOSED`、H3
  `ATTRIBUTED（SAMPLED）`、H4 `INCONCLUSIVE`、H5
  `HOTSPOT_OBSERVED / CAUSAL_COST_NOT_ISOLATED`、H6 `INCONCLUSIVE`。
  H5 的旧标签 `ATTRIBUTED（D16 限定）` 只表示函数级热点归属，不表示
  扫描净成本或优化收益。任何进入 G2 的提议都需 Owner 另行授权与单因素
  反例；本轮停在 Research Owner Gate。
- **重开条件**：io-wq/kworker 唯一归属与 IRQ/softirq 成本（root 会话已
  解除 kernel cycles/eBPF，但这两项仍未闭合）、C2 模式差机制、
  D4 采样基线重测、tmpfs 对照、真实存储/冷缓存。#622 方法边界继续有效：
  不建新 tracing 引擎；残余缺口均为环境/产品 API 形状问题。

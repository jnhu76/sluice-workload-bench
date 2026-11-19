# 成本账本与热点判定（COST-LEDGER）

状态：**正式账本（r2，独立审计后降级修正）**。生成链 =
`tools/profile_fio_sluice.py analyze`（`analysis/`）→
`tools/render_cost_ledger.py` 渲染 §1/§2/§5 表格。所有数字可回指
`results/attribution/session-001/runs/<run_id>/` 原始文件及其 sha256
（`sha256-manifest.txt`，会话结束时整树重新生成）。

作用域声明（全表适用）：

- `PARTIAL_CPU_ACCOUNTING`：非 root 会话——perf 仅用户态（`:u`）事件；
  kernel cycles / io-wq CPU / IRQ 不可测（capability.json 为证；`cycles:k`
  探测为 permission denied，非 0）。本表**没有任何内核态 CPU 数字**。
- per-op 口径 = `PROCESS_LIFECYCLE_*`（ramp=0，窗口=进程生命周期，分母=
  io_bytes/bs）。窗口契约（逐引擎）：native io_uring 的 A2（10s time_based）
  vs C2（20G 定长、5.24M ios）指令/op 差 **1.01%**（2-rep 均值；<10% 预注册
  界）→ 启动/收尾污染 ≤~1% 已验证。**sluice-uring 与 TP 的跨模式差更大
  （7.41% / 4.14%），且方向与启动摊销相反 → 按 mode/timing 效应处理；其
  per-op 值携带该量级的跨模式不确定度**，不得引用 1.1% 作为 sluice 的
  精度（audit 修正；analysis/window-contract.json `per_engine`）。
- 采样占比（perf record cycles:u）只进 SAMPLED 行，永不与 counted 相加、
  不进 per-op 表。cycles/instructions/CPU-time/latency 各维度独立呈现。
- `task-clock:u` = **进程树总 CPU（user+sys）**，非 user-only（与 rusage
  user+sys 精确吻合，A2 sluice 2.694 vs 2.695µs/op）；不含 io-wq/kworkers。
- 引擎：`io_uring`=fio 内建；`sluice-uring`/`sluice-threadpool`=external
  .so（release stripped 为计数基线；record 用 `.text` 哈希相等的 sym 双生，
  binary-manifest.json `text_section_match=true`）。

## 1. 主表：每 I/O 成本（用户态 counted + task total）

<!-- LEDGER-DATA:BEGIN -->
| cell | engine | reps | instr/op (u) | cycles/op (u) | cpu-us/op (u) | denominator |
|---|---|---:|---:|---:|---:|---:|
| A1 | io_uring | 2 | 2760.9 | 1547.7 | 1.7 | 6081655 |
| A1 | sluice-threadpool | 2 | 10326.5 | 12137.7 | 37.3 | 267134 |
| A1 | sluice-uring | 2 | 6442.8 | 4100.8 | 2.5 | 4181904 |
| A2 | io_uring | 2 | 2775.4 | 1500.4 | 1.7 | 5792978 |
| A2 | sluice-threadpool | 2 | 7878.3 | 9052.4 | 14.1 | 2368810 |
| A2 | sluice-uring | 2 | 8001.1 | 4582.0 | 2.7 | 3790187 |
| A3 | io_uring | 2 | 2732.5 | 1671.3 | 8.2 | 1264933 |
| A3 | sluice-threadpool | 2 | 6874.7 | 8751.0 | 24.6 | 1224268 |
| A3 | sluice-uring | 2 | 5991.4 | 4363.3 | 8.9 | 1169813 |
| A4 | io_uring | 2 | 2779.0 | 1471.0 | 1.7 | 6219002 |
| A4 | sluice-threadpool | 2 | 6860.3 | 8599.8 | 16.1 | 1924996 |
| A4 | sluice-uring | 2 | 6040.9 | 3906.4 | 2.5 | 4082404 |
| C2 | io_uring | 2 | 2747.7 | 1461.9 | 1.7 | 5242880 |
| C2 | sluice-threadpool | 2 | 8218.8 | 8844.5 | 14.4 | 5242880 |
| C2 | sluice-uring | 2 | 8641.2 | 4708.4 | 2.7 | 5242880 |

Δ（sluice-uring − io_uring，同 cell；正 = sluice 更贵）：

| cell | Δinstr/op | Δinstr% | Δcycles/op | Δcpu-us/op |
|---|---:|---:|---:|---:|
| A1 | +3681.9 | +133.4% | +2553.1 | +0.85 |
| A2 | +5225.7 | +188.3% | +3081.7 | +0.98 |
| A3 | +3258.9 | +119.3% | +2692.0 | +0.70 |
| A4 | +3261.9 | +117.4% | +2435.4 | +0.86 |
| C2 | +5893.5 | +214.5% | +3246.5 | +1.03 |
<!-- LEDGER-DATA:END -->

读法：Δ = sluice-uring − io_uring（同 cell 同 driver 同 offset 序列），
该差 = Adapter+Sluice 用户态组合差额（full-chain difference）——**不能**
直接当作"Sluice Core 指令数"（见 §3 边界）。

## 2. 内核交互与调度（strace 受控计数 + rusage）

<!-- TRACE-DATA:BEGIN -->
| cell | engine | enter/op | futex/op | pread64/op | straced/bare IOPS | 扰动 |
|---|---|---:|---:|---:|---:|---|
| A1 | io_uring | 1.000 | 0.001 | 0.000 | 0.04 | 96% |
| A1 | sluice-threadpool | - | 4.121 | 1.000 | 0.18 | 82% |
| A1 | sluice-uring | 1.000 | 0.001 | 0.000 | 0.05 | 95% |
| A2 | io_uring | 1.000 | 0.001 | 0.000 | 0.04 | 96% |
| A2 | sluice-threadpool | - | 3.381 | 1.000 | 0.12 | 88% |
| A2 | sluice-uring | 1.000 | 0.001 | 0.000 | 0.06 | 94% |
<!-- TRACE-DATA:END -->

补充事实（counted，同 session）：

- **提交形状等价（结构证据 + strace 计数，受 <50% 规则限制）**：native
  io_uring 与 sluice-uring 均 ~1.0000 enter/op（shape 回执：native
  65,536 enters / 65,536 ios，全部带 `IORING_ENTER_GETEVENTS`；sluice
  65,536 次 flags=0 submit + 1 次 min_complete=1 wait = 65,537/65,536）。
  **"sluice 每 submit 即 flush" 不构成两臂差异——native 同样逐 io_u
  submit+enter。** strace 全量追踪把 IOPS 压到 bare 的 3.8–18.3%，触发
  预注册 no-conclusion 规则，故该结论在结构证据下记
  `INCONCLUSIVE（结构 LIKELY）`。
- futex/op：native 0.0006、sluice-uring 0.0008、**sluice-threadpool
  3.4–4.1**（A1/A2，同一 STRACE_DIAGNOSTIC 限制：计数形态可用，机制结论
  受限）。
- rusage（**clean0，ramp=0 窗口匹配**；未插桩；进程树含 worker）：
  - A1/io_uring：ctx-switch/op 0.00019；CPU user/sys/total 0.71 / 0.90 / 1.62 µs/op
  - A1/sluice-threadpool：ctx-switch/op 2.00227；CPU user/sys/total 13.01 / 11.81 / 24.82 µs/op
  - A1/sluice-uring：ctx-switch/op 0.00030；CPU user/sys/total 1.56 / 1.00 / 2.56 µs/op
  - A2/io_uring：ctx-switch/op 0.00020；CPU user/sys/total 0.75 / 0.96 / 1.71 µs/op
  - A2/sluice-threadpool：ctx-switch/op 1.11297；CPU user/sys/total 9.01 / 6.96 / 15.97 µs/op
  - A2/sluice-uring：ctx-switch/op 0.00031；CPU user/sys/total 1.64 / 1.08 / 2.72 µs/op
  - A3/io_uring：ctx-switch/op 0.00094；CPU user/sys/total 0.80 / 7.38 / 8.18 µs/op
  - A3/sluice-threadpool：ctx-switch/op 1.31934；CPU user/sys/total 5.37 / 12.98 / 18.35 µs/op
  - A3/sluice-uring：ctx-switch/op 0.00101；CPU user/sys/total 1.65 / 7.17 / 8.82 µs/op
  - A4/io_uring：ctx-switch/op 0.00020；CPU user/sys/total 0.75 / 0.96 / 1.71 µs/op
  - A4/sluice-threadpool：ctx-switch/op 1.31504；CPU user/sys/total 9.19 / 7.64 / 16.83 µs/op
  - A4/sluice-uring：ctx-switch/op 0.00031；CPU user/sys/total 1.46 / 1.04 / 2.50 µs/op
  （clean ramp=2 的 rusage 含 ramp 而 io_bytes 不含 → 不作 per-op，
  `CLEAN_RAMP2_LIFECYCLE_MISMATCH`；pmu 行 rusage 含少量 perf 自身开销，
  仅作旁证。）
- 调度计数器不经 PMU：paranoid=2 下 perf 对软件事件强制 exclude_kernel，
  `context-switches:u`/`cpu-migrations:u` 恒 0（aux 回执为证）——按
  `INVALID_ZERO_UNDER_EXCLUDE_KERNEL` 处理，绝不当作"零次切换"。

## 3. 归因边界（Attribution Limits）

full-chain 差额（§1 Δ）分解为：

| 层 | 证据 | 状态 |
| --- | --- | --- |
| fio driver 增量（async 语义 vs 内建） | 两臂同 fio 进程、同 job 机器；native record 的 top 符号全是 fio 自身（get_io_u 10%、__fio_gettime 10%、axmap_isset 5%…），driver 增量包含在 Δ 内，无法单独扣除 | NOT_SEPARATED_WITHOUT_COUNTERFACTUAL |
| Adapter（queue/harvest/event） | SAMPLED：A2 `fio_sluice::harvest` 1.72%、A1 `sluice_queue` 2.80% | SAMPLED |
| Sluice Core（RequestScope/RequestCore/Backend/ProgressSource） | SAMPLED：A1 mutex 31.1% + dispatch/queue/take/finalize/consume ~12.8%；A2 `RequestScope::take` **12.15%**（D1 时 2.44%） | SAMPLED + DEPTH_DIFFERENTIAL |
| Kernel 侧差（io-wq/inline/FS） | non-root 会话不可测；clean0 rusage sys 分量两臂接近（A1 0.90 vs 1.00µs/op） | UNAVAILABLE (cycles:k)；root 会话另见 ATTRIBUTION-RESULTS-R2 |
| 重复工作（adapter scan × scope resolve） | 结构存在（源码）；D16 take/harvest 份额上升与 D16 指令差同向（但 D4<D1 非单调，见 H5） | DEPTH_DIFFERENTIAL |

SAMPLED 份额明细（perf record，cycles:u；**A1 记录运行自身较 clean 慢
18–24%，其份额只作机制定性；A2 记录运行接近 clean（−0.7%/+1.7%），为
首选基线**）：

| 符号 | A1 sluice-uring | A2 sluice-uring (D16) |
| --- | ---: | ---: |
| libc pthread_mutex_lock + unlock | 31.1% | 29.8% |
| RequestScope::take | 2.44% | **12.15%** |
| dispatch_one_locked | 3.10% | 3.69% |
| harvest (adapter) | <0.2% | 1.72% |
| sluice_queue (adapter) | 2.80% | — |
| finalize_operation_terminal_ | 2.21% | — |
| account_transport_result_locked | 1.75% | — |
| dispatch_after_accept | 1.46% | — |
| RequestBridge::consume | 1.23% | — |
| retire_router_entry_ | 1.23% | 0.62% |
| handle_one_cqe | 1.21% | 1.20% |

（"—" = 未进该 cell 的 top 列表；检测限 0.2% ≈ 20 samples/10K，属检测限
而非缺失证明。）

native io_uring 对照（A1/A2 record）：无 mutex 进 top-15；热点全为 fio
driver 机制（get_io_u、__fio_gettime、axmap_isset、add_*_sample、
fio_ioring_prep/queued、io_completed…）。TP 对照：mutex/condvar
40–51%（A1 扰动基线；A2 近 clean）+ futex 3.4–4.1/op（strace）。

## 4. H1–H6 热点判定（#639 准入清单；r2 按对抗审计降级）

判定词：`ATTRIBUTED` / `LIKELY_BUT_UNPROVEN` / `ADAPTER_ONLY` /
`NOT_EXPOSED` / `INCONCLUSIVE` / `INVALID_MEASUREMENT`。

| ID | 候选 | 判定（r2） | 证据 | 限制/阻断 |
| --- | --- | --- | --- | --- |
| H1 | ThreadPool queue/worker handoff | **LIKELY_BUT_UNPROVEN** | 机制三源：mutex/condvar 40–51%（SAMPLED，A2 近 clean）、ctx 0.9–2.0/op（clean0/pmu，窗口匹配）、任务 CPU 16–25µs/op（clean0 4k）vs native 1.6–1.7µs、A1 吞吐 0.077× native（clean0 基线） | futex 计数来自 <50% IOPS 的 strace 运行（预注册规则 → 只能 INCONCLUSIVE 级证据）；"futex 是主成本"未证明（次数≠阻塞）；kworker 侧不可测 |
| H2 | io_uring submit/enter（#595 门禁） | **INCONCLUSIVE（结构 LIKELY 无差异）** | 两臂 ~1.0000 enter/op（strace shape，精确闭合）；native 每次 enter 带 GETEVENTS，sluice flags=0 + 用户态 peek；sys/op 两臂接近 | strace 触发 no-conclusion 规则；结构证据（fio io_uring 逐 io_u submit；Sluice submit_transport_locked 逐 submit flush）支持"形状等价"但非本 session 计量结论；#595 不受影响 |
| H3 | Completion/Request observation | **LIKELY_BUT_UNPROVEN（Sluice Core 侧）** | A2 take 12.15%、handle_one_cqe 1.20%、finalize 1.48% + consume 2.91% + retire 0.62%（合计 5.0%）；publication 符号（complete_publication 等）≤0.7% | 仅 SAMPLED；Adapter 与 Core 观察成本无法独立切开；P/O 逐请求时序 `UNIDENTIFIABLE_WITH_CURRENT_INSTRUMENTATION` |
| H4 | ProgressSource notification/wait | **INCONCLUSIVE** | uring 臂 ProgressSource 相关用户态符号未进 top（<0.2% = 10K 样本的检测限，非缺失证明）；TP cond_wait 属 H1；eventfd/poll 在内核侧不可见 | 检测限功效不足；非 root 无 eventfd 观测；不重开 #621 W1 |
| H5 | Capacity / repeated scan | **LIKELY_BUT_UNPROVEN（D16 抬升成立；扫描份额随深度单调）** | 指令差：D1 3,682 / D4 3,262 / D16 5,226（D4<D1，非单调）；**SAMPLED take 份额随深度单调：2.44% (D1) → 4.17% (D4) → 12.15% (D16)**；harvest 1.72%@D16。非单调解释（假设，未证）：D1 每 op 固定耦合成本更高（更多 getevents/wait），抵消 D4 的扫描增量 | D1/A1 采样基线受 18% 扰动；C2 模式差 7.4%（双 rep 稳定，机制未识别）；#579 门禁要求 adapter-only vs core 单因素 A/B；harvest（adapter）自身仅 1.7%，主体在 core take |
| H6 | Buffer ownership/data movement | **INCONCLUSIVE** | 4k 与 64k（A3 record 补测）均无 memcpy/payload-copy 样本进 top；64k A3 的 Δ 小（+3,259 instr/op、+0.70µs/op）且以 sys 为主；A3 sluice top: mutex 27.4%、dispatch_one 4.0%、sluice_queue 3.8%、take 3.25% | 采样功效（10K samples、0.2% 限）不足以证明"无搬运"；真实 streaming consumer 属 #632，本轮不做 |

优先级（事实驱动，非总样本百分比排序）：

1. **H1（TP 握手）**：量级最大、机制方向最一致（record/rusage）；但
   `LIKELY_BUT_UNPROVEN`，可删除部分未识别 → 不代表可优化。
2. **H5（D16 扫描抬升）**：take/harvest 的 D16 份额是明确可定位候选；
   非单调与扰动基线使其保持 LIKELY；#579 门禁要求单因素 A/B。
3. **H3（完成观察）**：与 take 路径重叠，adapter/core 边界未切开。
4. **H2/H4/H6**：无本 session 可成立的净成本结论。

## 5. 观察扰动（G7）

<!-- PERTURBATION:BEGIN -->
| cell | engine | clean0 IOPS (ref) | pmu vs clean0 | record vs clean0 | trace-straced vs trace-bare |
|---|---|---:|---:|---:|---:|
| A1 | io_uring | 629267 | 615875 (-2.1%) | 632431 (+0.5%) | 23979 (-96.2%) |
| A1 | psync | - | - (-) | - (-) | - (-) |
| A1 | sluice-threadpool | 48227 | 28791 (-40.3%) | 30928 (-35.9%) | 7029 (-81.7%) |
| A1 | sluice-uring | 398133 | 407553 (+2.4%) | 343897 (-13.6%) | 20543 (-94.9%) |
| A2 | io_uring | 595526 | 597343 (+0.3%) | 606728 (+1.9%) | 22966 (-96.2%) |
| A2 | sluice-threadpool | 213127 | 238168 (+11.7%) | 216317 (+1.5%) | 25672 (-88.0%) |
| A2 | sluice-uring | 373913 | 378629 (+1.3%) | 378023 (+1.1%) | 19788 (-94.5%) |
| A3 | io_uring | 124415 | 125285 (+0.7%) | 124947 (+0.4%) | - (-) |
| A3 | sluice-threadpool | 210514 | 139752 (-33.6%) | 196229 (-6.8%) | - (-) |
| A3 | sluice-uring | 115421 | 115468 (+0.0%) | 115634 (+0.2%) | - (-) |
| A4 | io_uring | 595557 | 615378 (+3.3%) | 618876 (+3.9%) | - (-) |
| A4 | sluice-threadpool | 199991 | 203844 (+1.9%) | 221122 (+10.6%) | - (-) |
| A4 | sluice-uring | 406894 | 405312 (-0.4%) | 421114 (+3.5%) | - (-) |
| C2 | io_uring | - | 603130 (-) | - (-) | - (-) |
| C2 | sluice-threadpool | - | 234623 (-) | - (-) | - (-) |
| C2 | sluice-uring | - | 372264 (-) | - (-) | - (-) |
<!-- PERTURBATION:END -->

补充（原始回执，不与上表重复计数）：

- 扰动按同口径 ramp=0 比较（clean0 为参照）；strace 列相对 trace-bare。
- **TP 对 perf stat/record 高度敏感**（A1/A3 的 pmu 运行 −24%~−43%），
  uring 臂较小（−6%…+1%）；A1 sluice/TP 的 record 运行自身慢 18–24% →
  其采样份额只作机制定性；A2 record 接近 clean（−0.7%/+1.7%）为首选基线。
- perf record：`Total Lost Samples: 0`；采样计数（10K–39K）见各
  perf-report.txt 头部（analysis SAMPLED 行的 samples 字段已修正
  K/M 后缀解析）。
- 正向控制：strace 计数与 shape 捕获对已知 io_uring 工作量
  （65,536 ios → 65,536 enters，to_submit=1 100%）精确闭合。
- 负控制：psync D1 臂（无 ring、无 Sluice）：无 enter、futex≈0、
  ctx/op ~0.0002——工具未对无关机制产生正结果。
- 丢失/歧义控制：bpftrace lost-event 检测器与 JoinIntegrity 已实现并
  测试（本环境无 eBPF 流；strace -c 对 syscall 计数穷尽，
  event-loss.json 记录 "loss N/A"）。

## 6. 完成观察边界（G6，#623 K/R/P/O）

- K（kernel completion）：`NOT_OBSERVABLE`（无 tracepoint/无 :k PMU）。
- R（reap）：部分——syscall 形态（enter 的 flags/min_complete 计数）+
  SAMPLED reap 成本（handle_one_cqe ~1.2%）；无逐请求时间戳。
- P（publication）：`UNIDENTIFIABLE_WITH_CURRENT_INSTRUMENTATION`
  （RequestCore `complete_publication` 为边界；产品 API 无时间戳，未也不
  得为观测改产品）。
- O（take/observation）：SAMPLED（take/harvest/event 份额）；无逐请求配对。
- 任何 CQE→publication→take 的逐请求时序主张：`AMBIGUOUS`，拒绝输出。

## 7. 禁止与未做

- 未把 wall/IOPS 差距换算成"机器工作"总量；未跨维度相加。
- 未用 strace 时延；未把采样占比当 cycles 份额。
- 未运行 F3 排名；未做 tmpfs 对照（单 FS=btrfs，明示）。
- 未修改 Sluice/fio 语义；诊断构建 `.text` 哈希与 release 一致
  （binary-manifest.json `text_section_match`）。
- C2 首轮配置错误（size 限制总量 → 262144 ios，启动污染 38%）已作废并
  保留原始回执（`runs/INVALID-CFG__*`）；修正为 io_size 后按预注册
  clean×3/pmu×2 重测。
- 会话早期 C2 单点回执保留为 `runs/SUPERSEDED__*`（不计入分析）。

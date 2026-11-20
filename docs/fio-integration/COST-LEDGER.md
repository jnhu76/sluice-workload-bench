# 成本账本与热点判定（COST-LEDGER）

状态：**正式账本（r3；§1–§7 为 r2/session-001 主体，§8 为
session-002-root 增补，§8.4 为最新判词）**。r2 判词（§4）仅作历史，
最新以 §8.4 为准。生成链 =
`tools/profile_fio_sluice.py analyze`（`analysis/`）→
`tools/render_cost_ledger.py` 渲染 §1/§2/§5 表格。所有数字可回指
`results/attribution/session-001/runs/<run_id>/` 或
`results/attribution/session-002-root/runs/<run_id>/` 原始文件及其 sha256
（`sha256-manifest.txt`，会话结束时整树重新生成）。

作用域声明（§1–§7 适用；§8 为 root 补充，含内核态数字）：

- `PARTIAL_CPU_ACCOUNTING`：非 root 会话（session-001）——perf 仅用户态
  （`:u`）事件；kernel cycles / io-wq CPU / IRQ 不可测（capability.json
  为证；`cycles:k` 探测为 permission denied，非 0）。§1–§7 **没有任何
  内核态 CPU 数字**；root 分域数字只在 §8。root 的 `:k` 数字只覆盖进程树
  内核态，不含 io-wq/kworker 与 IRQ/softirq。
- per-op 口径 = `PROCESS_LIFECYCLE_*_per_op`（窗口 = `ramp_time=0` 的整个
  fio 进程生命周期；分母 = 同一 run 的
  `lifecycle_ios = read.io_bytes/bs`）。列名：
  `instr/op (u)` = `PROCESS_LIFECYCLE_INSTRUCTIONS_per_op`（scope `user`）、
  `cycles/op (u)` = `PROCESS_LIFECYCLE_CYCLES_per_op`（scope `user`）、
  `cpu-us/op (task total)` = `PROCESS_LIFECYCLE_CPU_TIME_us_per_op`
  （scope `task_total`，见下）。
- 逐引擎窗口契约：native io_uring A2 vs C2 差 **1.01%**（2-rep 均值；<10%
  预注册界）。该值只是 **native 的总跨模式差上界**：C2 pmu run 更短
  （8.5–8.8s vs 10s）且 per-op 更低，方向与启动摊销相反，**不能推出
  启动/收尾 ≤~1%，也不是 sluice/TP 的精度**。sluice-uring 7.41% 与 TP
  4.14% 的跨模式差方向同样相反，机制统一为
  **`MODE_OR_TIMING_EFFECT_NOT_IDENTIFIED`**；**不得把 7.41%/4.14%
  解释为启动/收尾成本**。三者分别携带 ≈1.0% / ≈7.4% / ≈4.1% 的跨模式
  不确定度，仅作 lifecycle 口径使用（analysis/window-contract.json
  `per_engine`）。
- 采样占比（perf record cycles:u）只进 SAMPLED 行，永不与 counted 相加、
  不进 per-op 表。cycles/instructions/CPU-time/latency 各维度独立呈现。
- `task-clock` / `task-clock:u` = **进程树总 CPU（user+sys）**，非
  user-only（与 rusage user+sys 精确吻合，A2 sluice 2.694 vs
  2.695µs/op）；不含 io-wq/kworkers。表头写 `(task total)`。
- 引擎：`io_uring`=fio 内建；`sluice-uring`/`sluice-threadpool`=external
  .so（release stripped 为计数基线；record 用 `.text` 哈希相等的 sym 双生，
  binary-manifest.json `text_section_match=true`）。

## 1. 主表：每 I/O 成本（用户态 counted + task total）

<!-- LEDGER-DATA:BEGIN -->
| cell | engine | reps | instr/op (u) | cycles/op (u) | cpu-us/op (task total) | denominator (per rep) |
|---|---|---:|---:|---:|---:|---:|
| A1 | io_uring | 2 | 2760.9 | 1547.7 | 1.7 | 6081655/6235841 |
| A1 | sluice-threadpool | 2 | 10326.5 | 12137.7 | 37.3 | 267134/308724 |
| A1 | sluice-uring | 2 | 6442.8 | 4100.8 | 2.5 | 4181904/3969151 |
| A2 | io_uring | 2 | 2775.4 | 1500.4 | 1.7 | 5792978/6153889 |
| A2 | sluice-threadpool | 2 | 7878.3 | 9052.4 | 14.1 | 2368810/2395025 |
| A2 | sluice-uring | 2 | 8001.1 | 4582.0 | 2.7 | 3790187/3783152 |
| A3 | io_uring | 2 | 2732.5 | 1671.3 | 8.2 | 1264933/1240890 |
| A3 | sluice-threadpool | 2 | 6874.7 | 8751.0 | 24.6 | 1224268/1571052 |
| A3 | sluice-uring | 2 | 5991.4 | 4363.3 | 8.9 | 1169813/1139661 |
| A4 | io_uring | 2 | 2779.0 | 1471.0 | 1.7 | 6219002/6088563 |
| A4 | sluice-threadpool | 2 | 6860.3 | 8599.8 | 16.1 | 1924996/2152095 |
| A4 | sluice-uring | 2 | 6040.9 | 3906.4 | 2.5 | 4082404/4023833 |
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
直接当作"Sluice Core 指令数"（见 §3 边界）。注意 per-op 是逐 rep 比值的
均值，两个 rep 的 `lifecycle_ios` 不同；`denominator (per rep)` 列同时给出
两者，不能用单一分母复算该行均值。

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
| Kernel 侧差（io-wq/inline/FS） | non-root 会话不可测；clean0 rusage sys 分量两臂接近（A1 0.90 vs 1.00µs/op） | UNAVAILABLE (cycles:k)；root 会话另见本文件 §8 与 ATTRIBUTION-RESULTS.md §I-R |
| 重复工作（adapter scan × scope resolve） | 结构存在（源码）；D16 take/harvest 样本份额上升与 D16 指令差同向（但 D4<D1 非单调，见 H5）；份额是 run 内占比，不是扫描成本的因果证据 | DEPTH_DIFFERENTIAL |

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

## 4. H1–H6 热点判定（#639 准入清单；**历史 r2 判定，已被 §8.4 的 r3 取代**）

判定词：`ATTRIBUTED` / `LIKELY_BUT_UNPROVEN` / `ADAPTER_ONLY` /
`NOT_EXPOSED` / `INCONCLUSIVE` / `INVALID_MEASUREMENT`。

| ID | 候选 | 判定（r2） | 证据 | 限制/阻断 |
| --- | --- | --- | --- | --- |
| H1 | ThreadPool queue/worker handoff | **LIKELY_BUT_UNPROVEN** | 机制三源：mutex/condvar 40–51%（SAMPLED，A2 近 clean）、ctx 0.84–2.00/op（clean0/pmu，窗口匹配）、任务 CPU 16–25µs/op（clean0 4k）vs native 1.6–1.7µs、A1 吞吐 0.077× native（clean0 基线） | futex 计数来自 <50% IOPS 的 strace 运行（预注册规则 → 只能 INCONCLUSIVE 级证据）；"futex 是主成本"未证明（次数≠阻塞）；kworker 侧不可测 |
| H2 | io_uring submit/enter（#595 门禁） | **INCONCLUSIVE（结构 LIKELY 无差异）** | 两臂 ~1.0000 enter/op（strace shape，精确闭合）；native 每次 enter 带 GETEVENTS，sluice flags=0 + 用户态 peek；sys/op 两臂接近 | strace 触发 no-conclusion 规则；结构证据（fio io_uring 逐 io_u submit；Sluice submit_transport_locked 逐 submit flush）支持"形状等价"但非本 session 计量结论；#595 不受影响 |
| H3 | Completion/Request observation | **LIKELY_BUT_UNPROVEN（Sluice Core 侧）** | A2 take 12.15%、handle_one_cqe 1.20%、finalize 1.48% + consume 2.91% + retire 0.62%（合计 5.0%）；publication 符号（complete_publication 等）≤0.7% | 仅 SAMPLED；Adapter 与 Core 观察成本无法独立切开；P/O 逐请求时序 `UNIDENTIFIABLE_WITH_CURRENT_INSTRUMENTATION` |
| H4 | ProgressSource notification/wait | **INCONCLUSIVE** | uring 臂 ProgressSource 相关用户态符号未进 top（<0.2% = 10K 样本的检测限，非缺失证明）；TP cond_wait 属 H1；eventfd/poll 在内核侧不可见 | 检测限功效不足；非 root 无 eventfd 观测；不重开 #621 W1 |
| H5 | Capacity / repeated scan | **LIKELY_BUT_UNPROVEN（历史 r2；当前见 §8.4）** | 指令差：D1 3,682 / D4 3,262 / D16 5,226（D4<D1，非单调）；**SAMPLED take 样本份额（各 run 内占比，非 per-op 函数计数）：2.44% (A1/D1) → 4.17% (A4/D4) → 12.15% (A2/D16)**；D16 的 root 平坦口径为 10.45%（§8.3）；harvest 1.72%@D16。非单调解释（假设，未证）：D1 每 op 固定耦合成本更高（更多 getevents/wait），抵消 D4 的扫描增量 | D1/A1 采样基线受 18% 扰动（只影响份额基线，不能解释 counted 指令差的 D4<D1）；**份额上升只是该函数占本 run 样本的比例更高，不能推出容量扫描的因果净成本**；D1→D4 的份额步长为 1.73 个百分点，与 D16 两会话的平坦符号份额差 1.70 个百分点（12.15 vs 10.45）同量级 → 单调趋势不可外推；C2 模式差 7.4%（双 rep 稳定，机制未识别）；#579 门禁要求 adapter-only vs core 单因素 A/B；harvest（adapter）自身仅 1.7%，主体在 core take |
| H6 | Buffer ownership/data movement | **INCONCLUSIVE** | 4k 与 64k（A3 record 补测）均无 memcpy/payload-copy 样本进 top；64k A3 的 Δ 小（+3,259 instr/op、+0.70µs/op）且以 sys 为主；A3 sluice top: mutex 27.4%、dispatch_one 4.0%、sluice_queue 3.8%、take 3.25% | 采样功效（10K samples、0.2% 限）不足以证明"无搬运"；真实 streaming consumer 属 #632，本轮不做 |

优先级（事实驱动，非总样本百分比排序）：

1. **H1（TP 握手）**：量级最大、机制方向最一致（record/rusage）；但
   `LIKELY_BUT_UNPROVEN`，可删除部分未识别 → 不代表可优化。
2. **H5（D16 take 样本份额抬升）**：take/harvest 的 D16 份额是明确可定位
   候选（历史 r2 判 `LIKELY_BUT_UNPROVEN`）；非单调与扰动基线使容量扫描
   的因果净成本未隔离；#579 门禁要求单因素 A/B。
3. **H3（完成观察）**：与 take 路径重叠，adapter/core 边界未切开。
4. **H2/H4/H6**：无本 session 可成立的净成本结论。

（以上为 r2 优先级。r3 后 H5 的当前判词为
`HOTSPOT_OBSERVED / CAUSAL_COST_NOT_ISOLATED`：D16 的 take 份额是函数级
热点观察，D4<D1 非单调，容量扫描的因果净成本未隔离；见 §8.4。）

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
- **TP 对 perf stat/record 高度敏感**（A1/A3 的 pmu 运行 −33.6%~−40.3%；
  A2/A4 有 +1.5%~+11.7% 的反向漂移），uring 臂较小（约 −2%~+4%，A1
  sluice record −13.6% 例外）；A1 sluice/TP 的 record 运行相对 clean
  （ramp=2）慢 18–24% → 其采样份额只作机制定性；A2 record 接近 clean
  （−0.7%/+1.7%）为首选基线。
- perf record：`Total Lost Samples: 0`（perf 未报告，不是零丢失证明）；
  采样计数（10K–39K）见各 perf-report.txt 头部（analysis SAMPLED 行的
  samples 字段已修正 K/M 后缀解析）。
- 正向控制：strace 计数与 shape 捕获对已知 io_uring 工作量
  （65,536 ios → 65,536 enters，to_submit=1 100%）精确闭合。
- 负控制：psync D1 臂（无 ring、无 Sluice）：无 enter、futex≈0、
  ctx/op ~0.0002——工具未对无关机制产生正结果。
- 丢失/歧义控制：bpftrace lost-event 检测器与 JoinIntegrity 已实现并
  测试（session-001 非 root 无 eBPF 流；session-002-root 见 §8.2；
  strace -c 对 syscall 计数穷尽，event-loss.json 记录 "loss N/A"）。

## 6. 完成观察边界（G6，#623 K/R/P/O）

- K（kernel completion）：`NOT_OBSERVABLE`（r2/session-001；root 以
  `io_uring_complete` tracepoint 计数 cqe/op=1.000，但仍无逐请求时间戳，
  见 §8.2）。root 会话补齐 tracepoint 与 `:k` PMU 后仍只有聚合计数
  （`join = NOT_ATTEMPTED`），逐请求 K→R→P→O 时序未建立，该 verdict 不随
  `FULL_FOR_MEASURED_SCOPES` 改变。
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

## 8. Root 会话补充（session-002-root，`FULL_FOR_MEASURED_SCOPES` CPU accounting）

独立会话（不与 session-001 混合）：E5 同一机器、同一 SLA/负载形状；
Owner 现场授权 sudo（密码仅经 `sudo -S` stdin 传入；未写入仓库/证据树/
git；workflow 草稿的明文已替换为占位符）。capability.json
判词 **`FULL_FOR_MEASURED_SCOPES`**（定义见 ATTRIBUTION-RESULTS.md §B）：
kernel cycles、eBPF、io_uring/sched tracepoints 全部 SUPPORTED；
`--call-graph dwarf/fp` 可用。**该判词只覆盖已测 scope**：本会话
capability.json 的 `io_wq_worker_attribution` 仍为 UNAVAILABLE，IRQ/softirq
未被探测，eBPF 只有聚合计数（逐请求 `join = NOT_ATTEMPTED`）——§8.1–§8.3
的每一行都不含这些成本，不得读成 whole-system CPU 账本。原始回执字段
`cpu_accounting_verdict` 的字面值仍是 `"FULL"`（receipt 已入 sha256
manifest，不追改），按上述定义解读。`perf_callgraph_user` 子项为 PARTIAL，
但 dwarf/fp 实跑成功（§8.3）。

### 8.1 每 I/O 用户/内核分域（counted；核心事件 100% time-running）

| cell | engine | instr:u | instr:k | cyc:u | cyc:k | ctx/op | migr/op |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| A1 | io_uring | 2,762 | 2,676 | 1,474 | 4,074 | 0.0002 | 0 |
| A1 | sluice-uring | 6,434 | 2,681 | 4,285 | 4,447 | 0.0003 | 0 |
| A1 | sluice-threadpool | 10,222 | 15,006 | 12,145 | 25,440 | 2.004 | 0.00015 |
| A2 | io_uring | 2,776 | 2,676 | 1,438 | 4,270 | 0.0002 | 0 |
| A2 | sluice-uring | 7,993 | 2,655 | 4,530 | 4,608 | 0.0003 | 0 |
| A2 | sluice-threadpool | 7,886 | 9,368 | 9,155 | 16,918 | 0.864 | 0 |

注：capability.json 的 `cpu_migrations` 探针记为 UNAVAILABLE（sleep 探针
0 次迁移，属假阴性），但 g3 实跑 raw 回执显示 A1 42–62、A2 52–87 次
迁移、100% time-running；`migr/op` 仅作旁证，不能据此宣称
`FULL_FOR_MEASURED_SCOPES` 覆盖“全部内核计数器”。uring 两臂 g3 的
`cpu-migrations` raw 为 8–10 次/run（per-op <1e-6，在表中按 4 位小数
舍入为 0）；**表中的 0 不得读作“未发生迁移”**。
`io_wq_worker_attribution` 仍 UNAVAILABLE。g3 的 branches /
branch-misses / L1-dcache-loads / LLC-loads 是 multiplexed 事件
（running 49–75%），分析器标 `SCALED` 并排除出精确 per-op 表；§8.1 主表
只列 g1/g2 的核心事件与 ctx/migr。
另：session-002-root 的 `window-contract.json` 为
`LIFECYCLE_ONLY_NO_STEADY_STATE_EXTRAPOLATION`（无 C2、`per_engine` 为
空）；归档 JSON 的 `option_B=absent` 是旧检测器只匹配字面 `pmu` 的缺陷，
修复后 `pmu-root-*` 计为 Option B=present。本表 per-op 仅为 ramp=0
进程生命周期值，不得外推稳定态，也不得套用 session-001 的 1.01% 窗界。
`task-clock` 为 `task_total`（进程树 user+sys）；`:k` 只覆盖该进程树。

**关键事实**：sluice-uring 与 native 的**内核侧成本几乎相同**（instr:k
2,655–2,681 vs 2,676；cyc:k +8–9%）——用户态 +3.7k…+5.2k instr/op 的
全链差距没有伴随内核侧膨胀；差距属于用户态 Sluice/Adapter 路径。
TP 的内核侧为 9.4k–15k instr、16.9k–25.4k cycles/op（futex 握手在位）。
内核侧等价只排除“内核膨胀”，不构成容量扫描因果净成本的证据。

### 8.2 eBPF 计数（comm 聚合，`HOST_ISOLATED_AGGREGATE`；`EBPF_COUNTED`）

| cell | engine | enter/op | submit_req/op | punt(io-wq) raw (per-op) | cqe[fio]/op | futex/op | fio on-CPU dwell |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| A1 | io_uring | 1.000 | 1.000 | 0 (0) | 1.000 | 0.000008 | 2.07 µs/op |
| A1 | sluice-uring | 1.000 | 1.000 | 1 (<1e-6) | 1.000 | 0.000010 | 2.89 µs/op |
| A1 | sluice-threadpool | — | — | n/a | n/a | 3.00 | 28.03 µs/op |
| A2 | io_uring | 1.000 | 1.000 | 0 (0) | 1.000 | 0.000007 | 2.17 µs/op |
| A2 | sluice-uring | 1.000 | 1.000 | 1 (<1e-6) | 1.000 | 0.000008 | 3.14 µs/op |
| A2 | sluice-threadpool | — | — | n/a | n/a | 6.15 | 15.41 µs/op |

<!-- BPF-DATA:BEGIN -->
（root 会话渲染段；`python3 tools/render_cost_ledger.py` 在含 trace-bpf
行的会话上生成 lower-bound/UNAVAILABLE 表。）
<!-- BPF-DATA:END -->

- **作用域**：本表所有计数按 `comm` 聚合、无 pid/tgid 绑定；唯一隔离
  是 run 前的 `pgrep -x fio` 门禁，故证据级别为
  **HOST_ISOLATED_AGGREGATE**，不是逐进程计数。同一 14s 窗口内可见大量
  宿主进程（A1 io_uring 回执：`@futex[node]` 5697、`zcode-cli` 989、
  containerd 82、dockerd 15）；若宿主上有 io_uring 使用者，未过滤的
  `@cqe` 会把它计入。修复后的分析器按 `comm=="fio"` 过滤 `cqe_events`，
  非 fio 计数单列 `cqe_events_non_fio_comms_system_wide`（A1/A2
  sluice-uring 各 1，来自 iou-wrk），且不并入本 run 的 per-op。历史
  `normalized-costs.json` 的 `cqe_events=3,524,078` 是旧的全 comm 求和
  （= 3,524,077 fio + 1 iou-wrk），raw 回执的 fio 计数是 3,524,077。
  TP 不经过 io_uring 路径，其 cqe 与 punt 均为 n/a（0 会是结构性缺失
  而非测得零）；分析器对 TP 行把这些键标为 `not_applicable`，不再把
  缺失的 io_uring map 求和成 0.0 发布。历史
  `analysis/normalized-costs.json` 中 TP 的旧行仍可能显示 0（旧分析器
  产物，未在本轮重生成），一律按本节 n/a 语义解读。
- **窗口核验**：`bpf_attach_s + wall_s <= bpf_window_s` 是 `EBPF_COUNTED`
  的必要条件；超窗记 `EBPF_WINDOW_OVERSHOOT`（计数下界、per-op 偏低）；
  accepted run 为重试时，第一次 attempt 的 bpftrace 回执不复制到重试目录
  （`bpf_retry_outside_window=true` → `EBPF_UNAVAILABLE`）。本会话旧 run
  未持久化 `bpf_window_s`，按 job `runtime+ramp+4=14s` 重建；
  `wall_s≈10.39s` + 1s attach allowance 通过，标
  `window_basis=RECONSTRUCTED_FROM_JOB` / `attach_basis=ASSUMED_1S`。
- `io_uring_enter` 经 `syscalls:sys_enter_io_uring_enter` 计数（内核 7.3.3
  已无 `io_uring:io_uring_enter` tracepoint）——两臂均 **1.0000 enter/op**
  且 `submit_req` 1.0000/op：提交形状等价，非本次 strace 扰动下的结论。
- **punt：native 0，sluice-uring 每 run 1 次**：btrfs + NODATACOW 热缓存
  的稳态无 io-wq punt，但 A1/A2 两个 sluice-uring run 各有
  `@punt[fio]: 1` 与 `@cqe[iou-wrk-*]: 1`（worker dwell 83–104µs），
  逐 op≈0.0000003——不能写作“无 io-wq punt”；#622 的 FS 条件性应按
  “稳态 0、每 run 边界 1 次”表述。io-wq worker 的 dwell/cqe key 仍为
  系统级 comm 匹配（0–0.1ms），**不归属本 run**。
- **TID/TGID**：本 workload 为 `numjobs=1/thread=1`，job 是 fio 进程内的
  线程（TID≠TGID）。探针用 `args->next_pid/prev_pid`（TID）配对
  start/stop 是正确的，但 `@dwell` 按 comm 汇总，得到的是 main+job+
  disk-util 等全部 `comm=fio` 线程之和（与 rusage 进程 CPU 同量级），
  不是提交 TID 的单线程时间。
- TP 的 futex 为 **3.00/op（D1）、6.15/op（D16）**（非扰动计数，替代此前
  strace 值）；fio on-CPU dwell 28.0/15.4 µs/op，与 rusage 任务 CPU 同量级。
- 早期一次 trace-bpf 尝试在 TP 上触发 bpftrace 丢事件（现场记录
  206,430 / 1,452,829；该批回执未归档，当前树不可复算）——已被重测
  取代；分析器现在对任何丢事件 run 输出 `EBPF_LOSSY`
  （CAPTURE_DROPPED，计数只作下界、dwell 无效），并有测试。
- **“无丢失报告”≠“零丢失”**：detect_lost 只匹配 bpftrace 的
  `Lost N events`（perf ring 丢弃）。attach 前窗口、退出信号后的窗口、
  attach 失败（无 `Attached N probes`）以及 dwell 的逐 CPU map 竞争都
  不在该计数器内——6 个回执的 stderr 均含
  `WARNING: Can't delete map element`，即至少一次 switch-out 未匹配到
  `@start`，该段 on-CPU 时间没有进入 dwell。runner 现以
  `Attached N probes` 为启动 fio 前的 attach 屏障，并把 attach 行、窗口
  秒数与 bpftrace 退出码写入 meta。

### 8.3 调用栈归因（perf record dwarf，A2 sluice-uring，10,178 samples，perf report 显示 10K，perf 未报告 lost）

root 会话只有 A2 的 dwarf 调用树（A1/sluice-uring 无 `record-root` 回执），
因此调用树只支持“D16 存在函数级热点”，不能单独证明深度依赖。

平坦份额：mutex lock 15.03% + unlock 13.98%（29.0%）、
`RequestScope::take` 10.45%、dispatch_one_locked 3.48%、get_io_u 3.54%。

调用树（children 份额）：

- `sluice_getevents` **44.42%**
  - `harvest` 27.63% → `RequestScope::take` 25.97% → `RequestBridge::consume` **15.52%**
  - `poll_progress` 16.36% → `poll` 15.48%：`reap_cqes` 8.40% →
    `handle_one_cqe` 7.59%（→ finalize 4.83%、offer_terminal 1.51%、
    release_execution 1.39%）；`publish_one` 5.48%；
    `find_live_router_cookie_` 1.13%
- `sluice_queue` **24.25%** → `RequestScope::submit_read` 21.81% →
  `AsyncIoContext::submit_read` 19.93% → `UringAsyncBackend::submit_read`
  18.02% → `dispatch_after_accept` 9.75%

即：**观察路径（harvest→take→consume）与提交路径（queue→submit→dispatch）
合计约 69% 的用户态热点**；mutex 的调用者分布在 consume（RequestCore 状态
机）、CQE 处理与 submit 路径。内核调用栈（`cycles:k` + fp，10,053
samples）显示每 I/O 的 syscall 进出（~10%）、
`filemap_get_read_batch`/`filemap_read`（~8%）、`fget`/`fput`（~4.7%）、
`xas_load` 3.1%、btrfs 读路径 1.85%——两臂共享的 io_uring 每请求内核路径。
调用树只证明 D16 存在函数级热点，不证明深度依赖或容量扫描的净成本。

### 8.4 判定修订（r3）

判定词：`HOTSPOT_OBSERVED` = 函数级热点可定位（采样份额/调用树）；
`CAUSAL_COST_NOT_ISOLATED` = 该热点的因果净成本未隔离（无函数级计数、
无单因素 A/B）。两者同时成立时不构成可优化性结论。
原 r3 标签 `ATTRIBUTED（D16 限定）` 仅指**函数级热点归属**（D16 的 take
样本份额可定位），不表示容量扫描的净成本或优化收益已被证明，不得作为
可优化性依据引用。

| ID | r2 判定 | r3 判定 | 依据 |
| --- | --- | --- | --- |
| H1 | LIKELY_BUT_UNPROVEN | **ATTRIBUTED（限定）** | eBPF futex 3.0/6.15、PMU ctx 2.0/0.86、fio dwell、内核侧 9.4–15k instr/op；必要/可删除拆分未完成 |
| H2 | INCONCLUSIVE（结构 LIKELY） | **NOT_EXPOSED** | 非扰动 tracepoint：两臂 1.0000 enter/op + 1.0000 submit_req/op；内核侧成本等价 |
| H3 | LIKELY_BUT_UNPROVEN | **ATTRIBUTED（SAMPLED）** | take→consume 25.97%/15.52% 调用树 + counted 深度指令差 |
| H4 | INCONCLUSIVE | INCONCLUSIVE | ProgressSource 自身符号仍低于检测限；eventfd 未单列 |
| H5 | LIKELY_BUT_UNPROVEN | **HOTSPOT_OBSERVED / CAUSAL_COST_NOT_ISOLATED**（原 r3 标签 `ATTRIBUTED（D16 限定）` 仅指函数级热点归属，不表示扫描净成本或优化收益） | 调用树仅支持“D16 存在函数级热点”；counted 深度差仅支持“D16 用户态差额更大”，且为全链差额、不能拆归 take/scan；内核侧等价排除只排除内核膨胀；三者均**不**支持容量扫描的因果净成本：D4<D1 非单调未闭合，采样份额（12.15% / root 10.45%）是 run 内占比而非 per-op 函数计数，函数级热点为 Core take（harvest 1.72%）、扫描机制仍是源码结构假设，且 depth 为多因素打包、#579 要求的单因素 A/B 未做 |
| H6 | INCONCLUSIVE | INCONCLUSIVE | 调用树无 copy 符号，但检测限不足以证明"无搬运" |

§8.4 为最新判定；§4 的 r2 判定仅作历史对照。

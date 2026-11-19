# research(perf): fio-sluice PMU/eBPF cross-layer cost attribution

执行 issue：#3。方法权威：Sluice #622（EXISTING_TOOLS_SUFFICIENT——本 PR 是
薄编排层，不是新 tracing 引擎）。C2O 边界：#623。热点清单：#639。
**不合并、不改 Sluice 产品代码、不动 fio Engine 语义。**

## 固定身份

- bench 基线：`7a8b851`（main）；本分支自其切出
- Sluice SDK：`5d94f233967c3aef993eb19ba9e3252c532b0951`（仓内钉住 = 产品
  checkout = 引擎 load 行，三方一致，analyze 强校验）
- fio：`fio-3.40`（宿主 stripped = clean/pmu 基线；诊断 fio 由冻结树
  `ff930c4` 构建，sha256 见 binary-manifest.json）
- 引擎：release stripped（计数基线）+ `-sym` 诊断双生（perf record 用），
  `.text` 段哈希逐字节相等（binary-manifest.json `text_section_match`）
- 环境：E5-2666 v3 / 内核 7.3.3-200.fc44 / btrfs(compress=zstd:1) SATA SSD
  热缓存 / taskset 0-9 / randseed=81；内核自 #622（7.2.11）已漂移，全部
  能力本轮重新实测
- **两个独立会话**：session-001（非 root，PARTIAL_CPU_ACCOUNTING，208
  run dir / 785 文件哈希），session-002-root（Owner 现场授权的 sudo 会话，
  **FULL**：kernel PMU + eBPF + tracepoints + callgraph，98 run dir /
  311 文件哈希）。密码仅经 `sudo -S` stdin，未落盘、未进 git（已核验）

## 支持与不可用（G1 capability.json，全部实盘回执）

- SUPPORTED：instructions:u、cycles:u、task-clock:u（= 进程树 user+sys）、
  branches/branch-misses/L1/LLC（counted）、perf record :u 平面采样、
  strace 受控计数
- session-002-root（Owner 授权）：以上全部可用，另加 cycles:k/
  instructions:k、bpftrace（io_uring syscall+submit_req+punt+complete+
  futex+sched dwell）、perf record --call-graph dwarf/fp、context-switches/
  cpu-migrations 精确计数
- 非 root 会话 UNAVAILABLE：cycles:k、
  bpftrace/eBPF、io_uring/sched tracepoints、io-wq CPU、block 事件、
  context-switches/cpu-migrations 的 PMU 计数（paranoid=2 强制
  exclude_kernel → 恒 0，按 INVALID 处理；切换数用 rusage）、
  perf --call-graph dwarf/fp（事件打不开；perf 失败仍 exit 0，已按输出
  文本判定）
- 判词：`PARTIAL_CPU_ACCOUNTING`——用户态归因完整，内核侧明示不可测，
  缺项不以 0 冒充（12 项自动化测试之一守护该不变量）

## 实际运行的 workload（G2 预注册冻结）

A1 randread 4k D1 / A2 randread 4k D16 / A3 seqread 64k D4 / A4 randread
4k D4 × {io_uring, sluice-uring, sluice-threadpool} + A1 psync D1 参照 +
C2（A2 形态，io_size=20G 定长 5.24M ios，窗口交叉核验，clean×3/pmu×2）。
F3 不进排名；psync 不进异步排名。

## 关键口径（MEASUREMENT-WINDOW-CONTRACT.md）

实测发现 fio 3.40 JSON `io_bytes` 只覆盖 ramp 后窗口（冒烟会话回执）→
插桩模式一律 `ramp_time=0`，分母 = io_bytes/bs（进程完成全部 I/O）；
标签 `PROCESS_LIFECYCLE_*`；C2 定长运行界定启动/收尾占比。

## 结果摘要（数字以 COST-LEDGER.md 为准，全部可溯原始回执）

- **每 I/O 用户态成本（counted，PROCESS_LIFECYCLE 口径）**：native
  io_uring 2,733–2,779 instr/op / 1,471–1,671 cycles/op；sluice-uring
  5,991–8,001 instr/op / 3,906–4,582 cycles/op。全链差额 +3,259…+5,226
  instr/op（+117%…+188%，time_based cells）；C2 定长 cell +5,894（+215%，
  单模式口径，携带 7.4% 跨模式不确定度）。task total CPU：native
  1.7µs/op（4k）→ sluice 2.5–2.7µs/op；64k cell 8.2→8.9µs/op。
- **ThreadPool**：mutex/condvar 40–51%（SAMPLED）、ctx 0.9–2.5/op
  （rusage）、task CPU 14–37µs/op；A1 吞吐为 native 的 0.06×。
- **提交形状**：两臂均 ~1.0000 enter/op（shape 精确闭合 65,536/65,536；
  sluice 另有一次 wait enter）；native 每次 enter 带 GETEVENTS、sluice
  flags=0 + 用户态 peek — **不存在"sluice 才逐 submit flush"**。
- **深度信号**：RequestScope::take 采样份额 2.44% (D1) → 4.17% (D4) →
  12.15% (D16)；但指令差 D4<D1 非单调，H5 判
  `LIKELY_BUT_UNPROVEN`。
- **窗口**：native A2 vs C2 差 1.01%（启动/收尾 ≤~1% 验证）；sluice
  7.41%（双 rep <0.1% 一致，机制 NOT_IDENTIFIED，按跨模式不确定度处理）。

### H1–H6 r3（root 数据后）

H1 `ATTRIBUTED（限定）`、H2 `NOT_EXPOSED`、H3 `ATTRIBUTED（SAMPLED）`、
H5 `ATTRIBUTED（D16 限定）`、H4/H6 `INCONCLUSIVE`。详见 COST-LEDGER §8.4。

## Observer overhead / event loss

- 扰动按 ramp=0 同口径逐引擎入表：uring 臂 −6%…+1%；**TP 对 perf
  stat/record 高度敏感（−24%…−43%）**；A1 sluice/TP 的 record 运行自身慢
  18–24%（其采样只作机制定性），A2 record 接近 clean
- strace -c：IOPS 降至 bare 的 3.8–18.3% → 触发预注册 <50% 规则，
  enter/futex 计数形态可用，相关 H1/H2 结论降为 INCONCLUSIVE 级
- perf record：SAMPLED 归因（函数占比），与 counted per-op 分表呈现；
  `Total Lost Samples: 0`
- 事件丢失：本环境无 eBPF 流；strace -c 无丢失概念；bpftrace lost-event
  检测器与 AMBIGUOUS 降级路径已实现并有测试

## #639 H1–H6 对照

见 COST-LEDGER.md §4（判定词 + 证据 + 归因边界）。

## 独立 Review

未参与实现的两名独立 agent：Evidence Auditor（首轮 `sha256sum -c`
537/543，发现 manifest 漂移/clean 分母错配/扰动表为空等；修复后复审
**785/785 PASS**，证据包完整性 PASS；残留文档旧数字已全部清理）；
Adversarial Reviewer（12 项证伪清单：4 REFUTED / 5 INCONCLUSIVE /
3 NOT_TESTED；窗口界、clean rusage、扰动表、H5 非单调、strace <50% 规则、
H4/H6 检测限意见已全部吸收）。root 会话后 r3 判词：
**`ATTRIBUTION_VALID`（归因有效；不含任何优化批准）**。

## 原始证据校验

`results/attribution/session-001/`（git-ignored 工作区证据树）：
`sha256-manifest.txt` 全树哈希；Evidence Auditor 独立复算记录
整树 782 文件 `sha256sum -c` 全通过（会话结束后重生成）；
`analysis/runs-index.json` 为从 run 目录重建的权威索引（208 个 run dir，
其中 23 个失败/作废回执保留且排除）。大型 perf.data 仅留本地 +
`perf.data.sha256` 侧车；无 sudo 口令或秘密。

## 未覆盖范围与重开条件

- 内核态/ io-wq / eBPF 归因：需要 root 会话（sudoers 受限 NOPASSWD 或
  Owner 现场）；runner 的 probe/pmu/trace 已支持 root 自动升级
- tmpfs 对照（#622 的 FS 条件依赖）：未运行，单 FS=btrfs 限制明示
- 冷缓存 / O_DIRECT / 写混合 / SQPOLL：不在本轮预注册范围
- 产品优化：本轮零产品结论；#604 G0 未动，#595/#617 门禁未触碰

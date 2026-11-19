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

## 支持与不可用（G1 capability.json，全部实盘回执）

- SUPPORTED：instructions:u、cycles:u、task-clock:u、context-switches、
  cpu-migrations、perf record :u 采样（含 dwarf callgraph）、strace 计数
- UNAVAILABLE（非 root，sudo 需密码；不自行改安全配置）：cycles:k、
  bpftrace/eBPF、io_uring/sched tracepoints、io-wq CPU、block 事件
- 判词：`PARTIAL_CPU_ACCOUNTING`——用户态归因完整，内核侧明示不可测，
  缺项不以 0 冒充（12 项自动化测试之一守护该不变量）

## 实际运行的 workload（G2 预注册冻结）

A1 randread 4k D1 / A2 randread 4k D16 / A3 seqread 64k D4 / A4 randread
4k D4 × {io_uring, sluice-uring, sluice-threadpool} + A1 psync D1 参照 +
C2（A2 形态 number_ios=5M，窗口交叉核验）。F3 不进排名。

## 关键口径（MEASUREMENT-WINDOW-CONTRACT.md）

实测发现 fio 3.40 JSON `io_bytes` 只覆盖 ramp 后窗口（冒烟会话回执）→
插桩模式一律 `ramp_time=0`，分母 = io_bytes/bs（进程完成全部 I/O）；
标签 `PROCESS_LIFECYCLE_*`；C2 定长运行界定启动/收尾占比。

## 结果摘要（数字以 COST-LEDGER.md 为准，全部可溯原始回执）

RESULTS_SUMMARY_PLACEHOLDER

## Observer overhead / event loss

- perf stat ≈0%（pmu-run IOPS vs clean 同数量级，逐引擎见表）
- strace -c：IOPS 显著下降（逐引擎实测百分比入表）——计数用于 syscall
  形态（enter/op、futex/op），不用于时延
- perf record：SAMPLED 归因（函数占比），与 counted per-op 分表呈现
- 事件丢失：本环境无 eBPF 流；strace -c 无丢失概念；bpftrace lost-event
  检测器与 AMBIGUOUS 降级路径已实现并有测试（未见丢失事件可报）

## #639 H1–H6 对照

见 COST-LEDGER.md §4（判定词 + 证据 + 归因边界）。

## 独立 Review

INDEPENDENT_REVIEW_PLACEHOLDER

## 原始证据校验

`results/attribution/session-001/`（git-ignored 工作区证据树）：
`sha256-manifest.txt` 全树哈希；Evidence Auditor 独立复算记录
EVIDENCE_AUDIT_PLACEHOLDER

## 未覆盖范围与重开条件

- 内核态/ io-wq / eBPF 归因：需要 root 会话（sudoers 受限 NOPASSWD 或
  Owner 现场）；runner 的 probe/pmu/trace 已支持 root 自动升级
- tmpfs 对照（#622 的 FS 条件依赖）：未运行，单 FS=btrfs 限制明示
- 冷缓存 / O_DIRECT / 写混合 / SQPOLL：不在本轮预注册范围
- 产品优化：本轮零产品结论；#604 G0 未动，#595/#617 门禁未触碰

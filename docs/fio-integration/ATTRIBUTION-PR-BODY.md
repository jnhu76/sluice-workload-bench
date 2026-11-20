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
  个 run dir / 785 条哈希记录，磁盘 786 文件），session-002-root（Owner
  现场授权的 sudo 会话，**`FULL_FOR_MEASURED_SCOPES`**：kernel PMU +
  eBPF + tracepoints + callgraph，74 个 run dir / 311 条哈希记录，磁盘
  312 文件；`runs-manifest.json` 的 98 条是含重试的条目，唯一 run_id
  63）。密码仅经 `sudo -S` stdin 传入 sudo 会话，未写入仓库/证据树/git；
  workflow 草稿文件曾保存明文密码（gitignored，未进 git、未进证据树），
  已在收口时删除。**判词定义
  见 ATTRIBUTION-RESULTS.md §B**：只表示已测 scope（:u/:k PMU、eBPF
  聚合计数、调用栈）齐备；io-wq/kworker 归属、IRQ/softirq、K/R/P/O 逐
  请求因果仍未闭合，不等于 whole-system 全成本归因。

## 支持与不可用（G1 capability.json，全部实盘回执）

- SUPPORTED：instructions:u、cycles:u、task-clock:u（= 进程树 user+sys，
  非 user-only）、branches/branch-misses/L1/LLC（counted）、perf record
  :u 平面采样、strace 受控计数
- session-002-root（Owner 授权）：以上全部可用，另加 cycles:k/
  instructions:k、bpftrace（io_uring syscall+submit_req+punt+complete+
  futex+sched dwell）、perf record --call-graph dwarf/fp（capability 子项
  `perf_callgraph_user=PARTIAL`，实跑成功：A2 sluice dwarf 10,178
  samples、perf 未报告 lost（非零丢失证明））；context-switches 精确计数；
  cpu-migrations 的
  capability 探针为 UNAVAILABLE（sleep 探针 0 次迁移，假阴性），g3 实跑
  有 42–87 次、100% time-running，故只作旁证；io-wq 归属仍 UNAVAILABLE
- 非 root 会话 UNAVAILABLE：cycles:k、
  bpftrace/eBPF、io_uring/sched tracepoints、io-wq CPU、block 事件、
  context-switches/cpu-migrations 的 PMU 计数（paranoid=2 强制
  exclude_kernel → 恒 0，按 INVALID 处理；切换数用 rusage）、
  perf --call-graph dwarf/fp（事件打不开；perf 失败仍 exit 0，已按输出
  文本判定）
- 判词：`PARTIAL_CPU_ACCOUNTING`——用户态归因完整，内核侧明示不可测，
  缺项不以 0 冒充（70 项自动化测试：64 项 r3 基线 + 6 项本轮审计修复；
  其中 12 项强制用例守护该不变量）

## 实际运行的 workload（G2 预注册冻结）

A1 randread 4k D1 / A2 randread 4k D16 / A3 seqread 64k D4 / A4 randread
4k D4 × {io_uring, sluice-uring, sluice-threadpool} + A1 psync D1 参照 +
C2（A2 形态，io_size=20G 定长 5.24M ios，窗口交叉核验，clean×3/pmu×2）。
F3 不进排名；psync 不进异步排名。

## 关键口径（MEASUREMENT-WINDOW-CONTRACT.md）

实测发现 fio 3.40 JSON `io_bytes` 只覆盖 ramp 后窗口（冒烟会话回执）→
插桩模式一律 `ramp_time=0`，分母 = 同一 run 的 `lifecycle_ios`
（`read.io_bytes/bs`）；标签统一为
`PROCESS_LIFECYCLE_INSTRUCTIONS_per_op` / `_CYCLES_per_op` /
`_CPU_TIME_us_per_op`。C2 定长运行只用于 **session-001** 的交叉核验；
session-002-root 的 window-contract 为
`LIFECYCLE_ONLY_NO_STEADY_STATE_EXTRAPOLATION`（无 C2 交叉核验），其
per-op 不得外推稳定态，也不得套用 session-001 的 1.01% 窗界。

## 结果摘要（数字以 COST-LEDGER.md 为准，全部可溯原始回执）

- **每 I/O 用户态成本（session-001 counted，PROCESS_LIFECYCLE 口径；
  root 分域见 COST-LEDGER §8.1）**：native io_uring 2,732–2,779 instr/op /
  1,471–1,671 cycles/op；sluice-uring 5,991–8,001 instr/op /
  3,906–4,582 cycles/op。全链差额 +3,259…+5,226 instr/op
  （+117%…+188%，time_based cells）；C2 定长 cell +5,894（+215%，
  单模式口径）。task total CPU：native 1.7µs/op（4k）→ sluice
  2.5–2.7µs/op；64k cell 8.2→8.9µs/op。
- **ThreadPool**：mutex/condvar 40–51%（SAMPLED）、ctx 0.84–2.00/op
  （clean0/pmu）、task CPU 14.1–37.3µs/op（pmu 均值）；A1 吞吐为 native
  的 0.077×（clean0 基线）。
- **提交形状**：两臂均 ~1.0000 enter/op（shape 精确闭合 65,536/65,536；
  sluice 另有一次 wait enter）；native 每次 enter 带 GETEVENTS、sluice
  flags=0 + 用户态 peek — **不存在"sluice 才逐 submit flush"**。
- **深度信号**：RequestScope::take 采样份额 D1 2.44% → D4 4.17% →
  D16 12.15%（r2 平坦）/ 25.97% 调用树、10.45% 平坦（r3 root）；
  但指令差 D4<D1 非单调，H5 当前判
  `HOTSPOT_OBSERVED / CAUSAL_COST_NOT_ISOLATED`
  （原 r3 标签 `ATTRIBUTED（D16 限定）` 仅指函数级热点归属，不表示容量
  扫描导致的净成本或优化收益已被证明；采样份额是 run 内占比，不是
  per-op 函数计数）。
- **窗口**：native A2 vs C2 差 1.01%——**仅 native 臂的总跨模式差上界**
  （C2 更短且 per-op 更低，方向与启动摊销相反），**不是"启动/收尾
  ≤~1%"的证明，也不是 sluice/TP 的精度**。sluice-uring 7.41%、TP 4.14%
  的跨模式差方向同样相反，机制
  `MODE_OR_TIMING_EFFECT_NOT_IDENTIFIED`，按各自量级（≈7.4% / ≈4.1%）
  跨模式不确定度处理并降级为 lifecycle-only，不得解释为启动/收尾成本。

### H1–H6 r3（root 数据后）

H1 `ATTRIBUTED（限定）`、H2 `NOT_EXPOSED`、H3 `ATTRIBUTED（SAMPLED）`、
H5 `HOTSPOT_OBSERVED / CAUSAL_COST_NOT_ISOLATED`（原 r3 标签
`ATTRIBUTED（D16 限定）` 仅指函数级热点归属，不表示扫描的净成本或优化
收益）、H4/H6 `INCONCLUSIVE`。详见 COST-LEDGER §8.4。

## Observer overhead / event loss

- 扰动按 ramp=0 同口径逐引擎入表：uring 臂约 −2%…+4%（A1 sluice record
  −13.6% 例外）；**TP 对 perf stat/record 高度敏感（−40%…+12%）**；
  A1 sluice/TP 的 record 运行相对 clean（ramp=2）慢 18–24%（相对 clean0
  为 −13.6%/−35.9%；其采样只作机制定性），A2 record 接近 clean
- strace -c：IOPS 降至 bare 的 3.8–18.3% → 触发预注册 <50% 规则，
  enter/futex 计数形态可用；r2 中受此限制的 H1/H2 结论曾降为
  INCONCLUSIVE 级，r3 的非扰动 eBPF/tracepoint 计数已覆盖该限制（H1
  `ATTRIBUTED（限定）`、H2 `NOT_EXPOSED`）
- perf record：SAMPLED 归因（函数占比），与 counted per-op 分表呈现；
  `Total Lost Samples: 0`（perf 未报告，不是零丢失证明）
- 事件丢失：session-001（非 root）无 eBPF 流；session-002-root 的
  bpftrace 流有丢失检测，早期一次 TP 丢事件（现场记录
  206,430/1,452,829；该批回执未归档，当前树不可复算）被重测取代；
  分析器对丢事件输出 `EBPF_LOSSY`（CAPTURE_DROPPED）；strace -c 无丢失
  概念；相关降级路径已实现并有测试
- eBPF 证据级别为 **HOST_ISOLATED_AGGREGATE**：所有计数按 `comm` 聚合，
  无 pid/tgid 绑定，唯一隔离是 run 前 `pgrep -x fio` 门禁；`@cqe` 现按
  `comm=="fio"` 过滤，非 fio/iou-wrk 计数单列且不并入本 run 的 per-op；
  sluice-uring 的 `@punt[fio]: 1` 是 raw count（不是 punt=0）；TP 无
  io_uring completion，其 cqe 记 n/a。attach 以 `Attached N probes`
  回执为屏障；"无 Lost N events 报告"≠"零丢失"，dwell 的逐 CPU map
  竞争与 attach 前窗口不在该计数器内。
- **窗口核验**：`bpf_attach_s + wall_s <= bpf_window_s` 是 `EBPF_COUNTED`
  的必要条件；超窗记 `EBPF_WINDOW_OVERSHOOT`（计数只作下界）。accepted
  run 是重试时，第一次 attempt 的 bpftrace 回执不再复制到重试目录
  （`bpf_retry_outside_window=true` → `EBPF_UNAVAILABLE`）。旧 run 未
  持久化 `bpf_window_s`，按 job `runtime+ramp+4` 重建；attach 延迟未记录
  时用 1s 保守 allowance，并标 `window_basis=RECONSTRUCTED_FROM_JOB` /
  `attach_basis=ASSUMED_1S`。

## #639 H1–H6 对照

见 COST-LEDGER.md §8.4（r3 判定 + 证据 + 归因边界；§4 为 r2 历史判定）。

## 独立 Review

未参与实现的两名独立 agent：Evidence Auditor（首轮 `sha256sum -c`
537/543，发现 manifest 漂移/clean 分母错配/扰动表为空等；修复后复审
**785/785 PASS**，证据包完整性 PASS）；Adversarial Reviewer（12 项证伪
清单：4 REFUTED / 5 INCONCLUSIVE / 3 NOT_TESTED；窗口界、clean rusage、
扰动表、H5 非单调、strace <50% 规则、H4/H6 检测限意见已全部吸收）。
root 会话后 r3 判词：
**`ATTRIBUTION_VALID`（归因有效；不含任何优化批准）**。

## 原始证据校验

`results/attribution/session-001/` 与 `session-002-root/`（git-ignored
工作区证据树）：`sha256-manifest.txt` 分别 785 / 311 条哈希记录，全部
`sha256sum -c` 通过（session-001 磁盘 786 文件、session-002-root 312
文件，均含 manifest 自身）；`analysis/runs-index.json` 为从 run 目录重建
的权威索引（session-001 208 个 run dir、23 个失败/作废排除；
session-002-root 74 个、13 个排除）。原始 perf.data 未入 git（session-001
8.46MiB / 12 个非空文件；session-002-root 482.5MiB / 5 个非空文件，最大
单文件约 316MiB），仅留本地；manifest 同时收录 perf.data 与 `.sha256`
侧车，缺失本地树时只能重跑对应会话（root 数据需 sudo）复现。无 sudo
口令或秘密。

## Closeout verification（验证对象：代码提交 `5e4fd0e`；后续仅文档计数变更）

- `python3 -m unittest discover -s tools/attribution/tests`: **Ran 70 tests
  ... OK**（64 项 r3 基线 + 6 项审计修复）。
- `analyze --results-dir results/attribution/session-001` 与
  `session-002-root`: **exit 0**；`sha_mismatch_free=True`；
  `session-002-root` 74 runs / 13 排除。
- `render_cost_ledger.py results/attribution/session-001`: 连续两次
  sha256 相同；`git diff --exit-code -- docs/fio-integration/COST-LEDGER.md`
  clean。
- `bash adapters/fio/tests/smoke.sh build/scratch-smoke-pr5`: **PASS**
  （专用 scratch，避免 `build/scratch` 旧产物触发 leftover 守卫）。
- `xmake check-ring`: **PASS**；`run_fio_matrix.py --rehearsal`: **exit 0**。
- root smoke `probe` / `pmu-root` / `record-root` / `trace-bpf` / `analyze`:
  **exit 0**；`cpu_accounting_verdict=FULL_FOR_MEASURED_SCOPES`；
  `callgraph_dwarf=True`；trace-bpf meta `bpf_window_s=9` /
  `wall_s=3.396` / `bpf_attach_s=0.604` /
  `bpf_retry_outside_window=false`；分析行 `EBPF_COUNTED`
  （`window_basis=META`）。

## 未覆盖范围与重开条件

- 内核态/ io-wq / eBPF 归因：root 会话（session-002-root）已完成
  kernel/user 分域、eBPF 聚合计数与 callgraph；io-wq/kworker 归属
  （worker key 仅系统级 comm 匹配）与 IRQ/softirq 成本仍未闭合，仍需
  重开；K/R/P/O 逐请求因果仍 `join = NOT_ATTEMPTED`
- tmpfs 对照（#622 的 FS 条件依赖）：未运行，单 FS=btrfs 限制明示
- 冷缓存 / O_DIRECT / 写混合 / SQPOLL：不在本轮预注册范围
- 产品优化：本轮零产品结论；#604 G0 未动，#595/#617 门禁未触碰

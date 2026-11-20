# 可删除工作台账（Removable Work Ledger）

- 判词归属：**`USERSPACE_COST_PARTIALLY_ATTRIBUTED`**。
- 本台账只记录**候选**及其准入状态。状态词只有四个：**EVIDENCE_READY**（**可申请实验**，不是“已证明可删”：counted 负担 + 可证伪预测 + 语义等价证明**计划** + 单因素设计）、**RESEARCH_ONLY**（机制/范围问题，不是优化候选）、**NOT_ADMITTED**（证据不足或前提不成立，不申请）、**NO_GO**（现有证据不支持继续投入；**不等于数学证伪**）。
- 实验后若证明可删/可降，才使用 `EVIDENCE_PROVEN`（本轮**未使用**）。
- **本轮零产品结论**。任何 EVIDENCE_READY 也不等于可以改 Sluice；#604 仍停在 G0 之前（`docs/fio-integration/PHASE1-REPORT.md` §K）。
- 记号：`E1`…`E5` 同 `USERSPACE_COST-RESEARCH.md` §0.1.4；`S:`=Sluice，`B:`=本仓库。

## 0. 口径与读法

1. **必要成本**（`NECESSARY_COST`）只证明“这些工作在当前设计下存在且不可省”，**不证明成本最小**。
2. **可能可删除**（`POTENTIALLY_REMOVABLE_COST`）：申请 G1/G2 需 **counted 负担 + 可证伪预测 + 语义等价证明计划 + 单因素设计**；声称“已证明可删”另需 **A/B 结果**（本轮无）。C1 满足前者、缺后者。
3. **金额口径**：`cyc/op` 若来自 record-root flat self，标 SAMPLED（n=1）；若由“调用数 × libc pair 价”得出，标 INFERRED；两者都不能与 counted 指令数相加。
4. **不得**把采样份额上升写成净因果成本；H5 的当前判词是 `HOTSPOT_OBSERVED / CAUSAL_COST_NOT_ISOLATED`（`COST-LEDGER.md:414`）。

## 1. 基准数字（A2/D16，除非注明）

| 量 | 值 | 类 | 来源 |
|---|---:|---|---|
| 总 cycles/op | 4,550.59 | SAMPLED → 派生 | `E3` record-root（`16,858,204,391/3,704,623`） |
| 总 instr/op（release） | 7,988.81 | COUNTED | `E2` release rep1/rep2 均值 |
| core lock/op | 18.624953 | COUNTED | `E2` research；`9+T+2P` 闭合 |
| libc lock/unlock 调用/op | 48.256 | COUNTED | `E3` bpf-locks（n=6） |
| core atomics/op | 3.005 | COUNTED（PMU 分解 INFERRED） | `E3` pmu-atomic |
| take/op | 9.499953 | COUNTED | `E2` |
| pending/op | 8.499953 | COUNTED | `E2` |
| harvest/op | 1.062495 | COUNTED | `E2` |
| resolve iters/op | 59.499681 | COUNTED | `E2` |
| 堆分配/op | 0 | COUNTED（静态） | 源码 + 反汇编增长分支 |

## 2. 必要成本（`NECESSARY_COST`）

| 项 | 每 accepted I/O | 为什么必要（当前设计） | 证据 |
|---|---|---|---|
| 提交链 | 1 `sluice_queue` → 1 scope submit → 1 ctx submit（`access_mtx_`）→ 1 虚 backend submit → 3 core 锁 → 1 SQE → 1 `io_uring_submit` | 接受事务与 SQE 写入的线性化；`access_mtx_` 串行接受、`dispatch_mtx_` 保护 SQ/ledger | §A.1；`S:src/async/uring_transport.cpp:130-235` |
| 完成链 | 1 CQE → 1 `handle_one_cqe`（`dispatch_mtx_`）→ 6 core 锁 → 1 publication → 1 `sluice_event` | terminal 不可变、执行引用退休、发布准入、observer 交接 | `S:src/async/uring_completion.cpp:70-198`；`S:src/async/uring_backend.cpp:578-614` |
| 观察链 | 1 `getevents`、1.06 `harvest`、T 次 take/consume、2P 次 core 锁 | `RequestScope`/`RequestCore` 的可见性与消费协议 | `B:adapters/fio/src/events.cpp:247,262`；`S:src/async/detail/request_core.cpp:325` |
| core 锁总量 | `9 + T + 2P` | 见 `REQUEST_CORE_SYNCHRONIZATION_COST.md` §3 | `E2` 计数闭合 |

> 上表与 C6 涉及的五把锁均为 **`SEMANTICALLY_NECESSARY_UNDER_CURRENT_DESIGN`**：删除会破坏 publication / terminal visibility / reclamation / unique consumption / no-lost-wake 中的至少一项。该标签只否定“可删除”，**不**主张“成本最小”。

> 这些是**工作量存在性**的证明；`9` 固定锁的“每次获取是否可合并/去除”仍是重新设计问题（C7）。

## 3. 候选清单

### C1 — pending 重复 take/consume（ready-index / 单次消费）

| 项 | 内容 |
|---|---|
| 状态 | **EVIDENCE_READY** — 仅申请 G1/G2 单因素 A/B，**不是**产品准入 |
| 已满足 / 缺失 | 满足：counted 负担（pending/op、take/op）、可证伪预测、单因素设计、语义等价证明**计划**（四条必须保持的条件，见 §语义门槛）；缺失：完整的语义等价**证明**、A/B 结果、#604 G0。因此**不是** `EVIDENCE_PROVEN` |
| 机制 | harvest 非破坏性（pending 槽不释放，`B:adapters/fio/src/events.cpp:91-171`）；每个 pending take 走 O(C) `resolve_` + 一次 core 锁后才判 pending（`S:src/async/detail/request_core.cpp:325-331`） |
| counted 证据 | pending/op 1 / 2.5 / 8.5 / 32.5；take/op 2 / 3.5 / 9.5 / 33.5；pending/take 0.50 / 0.714 / 0.895 / 0.970（`E2`） |
| SAMPLED 证据 | D16 take self 11.68% = 531.5 cyc/op；consume self 3.06% = 139.3；合计 670.8（`E3`，n=1） |
| 规模（INFERRED） | pending 分摊 self 600.2 + pending core 锁 486.4–505.9（8.5 对 × 57.22–59.52 cyc/对）= **1,086.5–1,106.1 cyc/op（约 1,087–1,106；23.9–24.3%）**。**这是模型估计，不是可省净收益**：pending 与 ready 的平均 self 相同、pending core 锁与全部 libc 锁的平均价相同，两个假设均未验证；新方案还要支付就绪登记/单次消费成本。旧稿的 243–253/843–853 把“对”乘以了“次调用”价，已订正；506 = 8.5 × (1,436.16/24.128) 是同一 pair 口径。净节省由 A/B 测出，core/fio 未拆 |
| 可证伪预测 | take/op → ≈1；core lock/op D16 18.625 → ≈10.125、D64 42.53 → ≈10.03；pending 份额 → ≈0；take+consume self 下降（幅度不可线性外推）；IOPS/CPU 不劣化 |
| 语义门槛 | 唯一消费、回收谓词、fio 完成观察形状（`staged`/`event`）、陈旧 ticket 返回 `empty` 四条必须保持（`REQUEST_SCOPE_LOOKUP_COST.md` §5.1） |
| 阻断 | #579 单因素 A/B 未做；Arm D 未跑；#604 G0 未满足 |
| 升/降级条件 | 升：同 build 单因素实验显示 core 锁/take 计数下降且 IOPS/CPU 不劣化；降：实验显示 pending 的 per-call 成本远低于 ready（则总收益 < 预测）、或语义无法等价 |

### C2 — O(depth) harvest 槽扫描

| 项 | 内容 |
|---|---|
| 状态 | **RESEARCH_ONLY**（并入 C1，不单独准入） |
| 证据 | harvest self 1.88%（`E3`，按 4,550.59 cyc/op 折 **85.6 cyc/op**）；r2 root 为 1.72%（`E4`，`COST-LEDGER.md:175`，该 root 的总 cycles/op 未在本报告复算）；每轮扫 C 个槽（`B:adapters/fio/src/events.cpp:92`）；扫描均值 1.0 / 2.8 / 8.94 / 32.98（`E2`） |
| 判断 | 直接成本小；它是重复 take 的上游触发器。消除 O(C) 扫描的收益主要来自“少调 take”，不是扫描本身 |
| 门槛 | 与 C1 同一实验；不单列 A/B |

### C3 — `RequestScope::resolve_` 线性扫描 → O(1) 槽索引

| 项 | 内容 |
|---|---|
| 状态 | **NOT_ADMITTED** |
| 证据 | 单次 take 58.41（A1，resolve 1.00 iters/take）→ 53.45（`E4`，6.26）/ 55.95（`E3`，6.26）cyc/take；resolve 迭代约 6.3×，单次成本**不升**；D4 全链差 3,260 < D1 3,673。注：8.94 是 adapter 的 harvest slots/harvest，不是 take 的扫描长度 |
| 判断 | 无收益证据；ticket 未用 `slot_` 是事实，但“改成 O(1) 能省多少”没有证据，现有数据反对有显著收益 |
| 升/降级条件 | 只有在 C1 之后仍存在可测的 take self，且能构造“只改索引、不改消费协议”的单因素实验时才复议 |

### C4 — “扫描长度是 D16 增量主因”

| 项 | 内容 |
|---|---|
| 状态 | **NO_GO** |
| 证据 | 增量由 take 调用次数 2→9.5 驱动；单次 take 不随 `resolve` 迭代上升；D4 全链差 < D1 |
| 备注 | 现有证据不支持（**非单因素证伪**；三个 record root 各 n=1、跨会话，不能作因果证伪；见 `USERSPACE-COST-RESEARCH.md` §J.1 与 §D.3） |

### C5 — “harvest 扫描是用户态主成本”

| 项 | 内容 |
|---|---|
| 状态 | **NO_GO** |
| 证据 | harvest self 1.72–1.88%；take 11.68%、consume 3.06%、mutex 31.56% |

### C6 — 删除/替换 core mutex、lock-free consume

| 项 | 内容 |
|---|---|
| 状态 | **NO_GO** |
| 证据 | `SEMANTICALLY_NECESSARY_UNDER_CURRENT_DESIGN`：`RequestCore::mutex_` 是 publication / terminal visibility / reclamation / unique consumption 的唯一串行化点（`S:src/async/detail/request_core.cpp:325,346-365,580-631`）；删除 → double accept/terminal/consume/duplicate free |
| 成本规模 | flat mutex 31.56% = 1,436.2 cyc/op（core 按调用数**分摊估计** 1,108.6，非严格上界；INFERRED），但这是**无竞争快路径**，不是等待（futex 0.001/op、on-CPU 96–98%、HITM 0.0098/op） |
| 若要做 | 需要新协议 + 正确性证明 + 单因素 A/B（`REQUEST_BRIDGE_CONSUME_COST.md` §3.2）；在这些之前不申请 |

### C7 — 合并固定 9 次 core 锁

| 项 | 内容 |
|---|---|
| 状态 | **NOT_ADMITTED** |
| 证据 | 9 = reserve/accept/claim + offer/release/begin/complete/claim/retire observer（`REQUEST_CORE_SYNCHRONIZATION_COST.md` §2.1）；core 锁计数已被 `9+T+2P` 完全解释（残差 ≤2e−6） |
| 规模（INFERRED） | 9 对 × 57.22–59.52 cyc/对 = **515–536 cyc/op**（旧稿 257–268 把 pair 数乘以 per-call 价，少算一倍）；占 core 锁对数 D1 69.2%、D16 48.3%、D64 21.2% |
| 判断 | 没有“哪两次获取保护同一状态且可合并”的证据；这是重新设计问题，不是删除冗余 |
| 升/降级条件 | 必须逐项指出冗余获取点 + 语义等价证明 + 单因素 A/B |

### C8 — ThreadPool 跨线程 handoff / 调度成本

| 项 | 内容 |
|---|---|
| 状态 | **RESEARCH_ONLY** |
| 证据 | D16：TP instr 7,902.23 vs sluice 7,992.81（**−1.1%**），但 cycles 9,046.30 vs 4,617.13（**+95.9%**）、CPU 14.644 vs 2.745 µs（+11.9 µs）；D64：instr −1.4%、cycles +47.2%；consumer：TP instr +1.65%、cycles +156.5%/+175.4%、IOPS −21.6%/−24.3% |
| 判断 | 成本在调度/停顿，不在指令数；机制未识别（futex 唤醒、跨核迁移、cacheline 迁移未分离） |
| 边界 | 对象是 threadpool 调度，不是 Sluice core；consumer 与 fio 非同构；不作为 #604 产品候选 |

### C9 — io_uring 提交批处理

| 项 | 内容 |
|---|---|
| 状态 | **NOT_ADMITTED** |
| 证据 | native/sluice 均 1.0000 enter/op（`E2` trace-bpf、`E4`：native 4,664,725/4,664,725，sluice 3,208,449 enter/3,208,448+1 worker CQE）；trace **NON_EXACT**，只作下界；主依据是 fio 默认 `min=max=1` 与每 SQE 一次 submit 的结构；无单独 enter 成本；#595 KEEP_IMMEDIATE |
| 判断 | 收益前提（enter/op > 1 且 enter 可测）不成立 |

### C10 — research 构建计数器开销

| 项 | 内容 |
|---|---|
| 状态 | **NO_GO（产品候选）** |
| 证据 | instr/op +124.36 / +167.31 / +490.19 / +3,366.62 / +167.94；IOPS −4.63% / −6.27% / −20.59% / −56.81% / −1.82%（`E2/logs/perturbation-summary.txt`，COUNTED） |
| 判断 | 是测量扰动，不是产品成本；只影响“高深度计数对 release 动力学的代表性”（该代表性 `NOT_IDENTIFIED`） |

### C11 — consumer bench 作为 fio adapter 成本代理

| 项 | 内容 |
|---|---|
| 状态 | **NOT_ADMITTED** |
| 证据 | 64 MiB tmpfs 顺序 positional read vs 1 GiB btrfs randread 热缓存；无 fio driver；3 phase（throughput/oracle/instrumented）；每完成 `wait_for`；分母 540,000 = 9×3×20,000（`S:bench/bench_w02_pipeline.cpp:137,526,581,589,735`） |
| 判断 | 非同构；per-op（uring 3,863.66 instr / 2,635.91 cyc）不能当 fio adapter 成本，也不能把 TP−uring 差值映射到 fio A2 |

### C12 — Kernel / io-wq / IRQ 成本

| 项 | 内容 |
|---|---|
| 状态 | **RESEARCH_ONLY**（测量范围扩展，不是优化候选） |
| 证据 | `cycles:k` 只覆盖 fio 线程上下文（A2 native 2,676.2 instr:k / 4,287.9 cyc:k；sluice 2,656.7 / 4,661.9，均为 g2 均值）；io-wq worker 的 CQE 每 run 仅 1 个（`E4` trace-bpf `@punt[fio]=1`、`@cqe[iou-wrk-]=1`），不可归属；IRQ/softirq 未探 |
| 判断 | 不能记 0；需要独立实验（root + tracepoint + IRQ 归属） |

### C13 — `find_live_router_cookie_` 的 O(C) 倒扫

| 项 | 内容 |
|---|---|
| 状态 | **NOT_ADMITTED** |
| 机制 | 每个 CQE 从 router 尾部倒扫，比较 `in_use` 与 cookie（`S:src/async/uring_transport.cpp:551-557`）；router 大小受 capacity 约束 |
| 证据 | `E3` A2/D16 flat self 1.35% = **61.4 cyc/op**（SAMPLED，n=1） |
| 判断 | 与 C3 同类：O(C) 查找，但**没有** counted 扫描长度分布（未插桩）、没有单因素实验、self 只有 1.35%；无收益证据 |
| 若要复议 | 先插桩 router 扫描长度，再按 C1 同一纪律做单因素 A/B |

## 4. 汇总表

| ID | 候选 | 状态 | 主要证据类 | 预计规模 | 下一动作 |
|---|---|---|---|---|---|
| C1 | pending 重复 take/consume | **EVIDENCE_READY** | COUNTED 调用 + SAMPLED self | 1,087–1,106 cyc/op（23.9–24.3%，INFERRED；模型值，非净收益） | 申请 G1/G2 单因素 A/B |
| C2 | O(depth) harvest 扫描 | **RESEARCH_ONLY** | COUNTED + SAMPLED | 85.6 cyc/op self | 并入 C1 |
| C3 | O(1) 槽索引 | **NOT_ADMITTED** | SAMPLED | 未证明 | 不申请 |
| C4 | 扫描长度 = 主因 | **NO_GO** | — | — | 关闭 |
| C5 | harvest = 主成本 | **NO_GO** | — | — | 关闭 |
| C6 | 删 core mutex | **NO_GO** | SAMPLED + 源码语义 | 1,436.2（全部 lock 符号；core 分摊 1,108.6） | 关闭 |
| C7 | 合并 9 次锁 | **NOT_ADMITTED** | COUNTED 计数 | 515–536（INFERRED） | 不申请 |
| C8 | TP handoff | **RESEARCH_ONLY** | COUNTED | cycles +95.9%（D16）；CPU +11.9 µs 是**跨线程聚合**，非单核 per-op | 另立研究 |
| C9 | submit 批处理 | **NOT_ADMITTED** | 结构 COUNTED + NON_EXACT trace | enter/op=1.0000（下界） | 关闭 |
| C10 | research 计数开销 | **NO_GO（产品）** | COUNTED | IOPS −4.6%…−56.8% | 仅作测量说明 |
| C11 | consumer 代理 | **NOT_ADMITTED** | COUNTED（非同构） | — | 关闭 |
| C12 | kernel/io-wq/IRQ | **RESEARCH_ONLY** | NOT_IDENTIFIED | 未归属 | 扩测量范围 |
| C13 | `find_live_router_cookie_` O(C) 倒扫 | **NOT_ADMITTED** | SAMPLED + 静态 | 61.4 cyc/op（1.35%） | 不申请 |

## 5. 未识别成本（`UNIDENTIFIED_COST`）

| # | 缺口 | 为什么不能记 0 | 现有线索 |
|---|---|---|---|
| (a) | 内核/io-wq/IRQ/softirq | `cycles:k` 只覆盖 fio 线程上下文；io-wq CQE 1/run 不可归属；IRQ 未探 | A2 `cyc:k` +374.1（+8.7%，g2 均值）；punt=1 |
| (b) | D16 record-root 的 280.3 cyc/op | 4.43% 未列符号 + 1.03% vdso + 0.70% unknown | 低于 0.2% 阈值的符号 |
| (c) | libc mutex 的 core/fio 拆分 | 31.56% 是全部 lock 符号；core 分摊**估计** 1,108.6（非严格上界，真界 0–1,436.2） | 24.128 对 vs 18.625 对 |
| (d) | D16 全链 +5,219 instr/+3,164 cycles 的因果拆分 | 无 native 同 build record；fio driver 增量未分离 | Sluice 相关 flat self ≈3.1k（把全部 libc mutex 计入 Sluice 的上界）与 Δ≈3.16k 接近是巧合，不是分解 |
| (e) | D64 cycles 余量 ≈2.86–2.92k/op | 无 D64 record/BPF；缓存/queueing/内核/调度未分离 | 公式见 `USERSPACE-COST-RESEARCH.md` §H.13：+5,627.7 − (1,342.7 + 1,367.8–1,422.8) = 2,862.2–2,917.3 |
| (f) | TP 的 cycles/CPU 不随指令解释 | 跨线程 handoff/调度/停顿未识别 | D16 instr −1.1%、cycles +96% |
| (g) | 启动/收尾摊销 | W-C2 7.41%/4.14%/1.01% 是模式差，不是启动成本 | `MODE_OR_TIMING_EFFECT_NOT_IDENTIFIED` |
| (h) | A2 pmu 跨 abort/resume | rep1 04:38、rep2 04:43、ABORT 04:39:52；IOPS 361,971→381,925、CPU 2.818→2.673 µs | instr 计数不受影响 |
| (i) | `E1` 二进制不可复核 | `3ba984…` 已被 `b1e28…` 覆盖 | S2 instr 五 cell 在 0.07% 内复现 |
| (j) | M-SEM 未对当前二进制重跑 | `session-002/manifest.json:2844-2846` `run:false`；smoke 覆盖 `3ba984`+`ece910` | invariants 支持等价，但不替代冻结 S3 |
| (k) | record-root n=1 / D64 无 record | 组件份额只有采样 CI | N=10,195，Lost 0 |
| (l) | U3.3 量纲错误 | 6.50/3.71/1.96/1.27 是总/总 | 正确 per-op 1.1818/1.04/1.0068/1.0007/1.04；含 2P 为 1.000 |
| (m) | achieved depth 是 INFERRED | fio 桶下界加权，不是逐 IO | consumer `max_outstanding` 未记录 |

## 6. Owner 决策点

1. **是否批准 C1（含 C2）进入 G1/G2 单因素 A/B**？批准的前提：语义等价证明、同 build 对照、单因素、counted 判据。
2. **是否先补 G0**（real consumer / 代表性 trace）？当前 fio synthetic 不构成 G0（`PHASE1-REPORT.md` §K）。
3. **是否批准 R3/R4/R5/R6**（native 同 build record、D64 record、M-SEM 重跑、kernel/io-wq 归因）作为“让判词升级”的前置？
4. **是否接受本轮零产品结论**：`USERSPACE_COST_PARTIALLY_ATTRIBUTED` 不授权任何 Sluice 修改。

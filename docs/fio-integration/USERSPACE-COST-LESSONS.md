# 用户态成本研究的可复用经验（Phase B 复盘）

- 状态：**RESEARCH_RECORD / NO_PRODUCT_CHANGE**
- 适用：Sluice fio adapter → `RequestScope` → `RequestBridge` → `RequestCore` 的用户态成本研究
- 证据基线：Phase A PR #5（squash `c66a3a9`）+ Phase B 报告（research 分支 `3bbfe83`）
- 测量 SHA：`5d94f233967c3aef993eb19ba9e3252c532b0951`
- 当前 HEAD：`9c630d1f5ff14da01a09d5c9cada8a2aa5ca2279`（相对测量 SHA 仅 `.claude/skills/*` 文档变化；实验前仍需重核锚点/二进制）

> 本文是**复盘记录**，不是产品决策，也不是优化批准。所有数字保留 `COUNTED / SAMPLED / INFERRED` 标签；模型值不等于已测净收益。Sluice 产品化仍必须走 #604 G0→G1→G2，并尊重 #579/#595/#621 的历史门禁。

## 0. 结论摘要

1. **最值得进入 G1 的候选是 C1**：消除 pending 的重复 `take/consume`（ready-index / 单次消费）。它是本轮唯一 `EVIDENCE_READY`，但不是 `EVIDENCE_PROVEN`。
2. **不要先做 O(1) 槽索引**：单次 `take` 成本没有随扫描长度上升；瓶颈是调用次数。
3. **不要先做 harvest 微优化**：`harvest` self 仅 1.72–1.88%；它是重复 `take` 的上游触发器。
4. **不要做 lock-free 重写**：31.56% 的 libc mutex 是无竞争快路径，不是锁等待；删除 core mutex 会破坏 publication/terminal/reclamation/unique consumption。
5. **D1 与 D16+ 是两个问题**：D1 是固定 9 锁的“截距”，D16/D64 是重复观察的“斜率”。先看真实 workload 的深度分布，再决定优先级。
6. **TP 的瓶颈在 cycles/CPU/handoff，不在指令数**：D16 TP 的 instr 比 sluice-uring 低 1.1%，cycles 却高 95.9%。
7. **本轮没有修改 Sluice、没有新增公共 API、没有创建产品 PR**；#604 G0 仍未满足。

## 1. 证据基线

| 项 | 值 | 标签 |
|---|---|---|
| Phase A 工具 PR | bench #5，squash `c66a3a9` | — |
| Phase A 最终门禁 | 73 tests；hash 785/785 + 311/311；ledger 幂等；smoke/check-ring/matrix/root smoke 通过 | COUNTED |
| Phase B 报告 | `docs/fio-integration/userspace/USERSPACE-COST-RESEARCH.md` @ `3bbfe83` | — |
| Phase B 判词 | `USERSPACE_COST_PARTIALLY_ATTRIBUTED` | — |
| 证据根 | `results/userspace-cost/session-001-baseline`、`session-002-counters`、`session-003-sync-consumer` | 本地、gitignored |
| 对象 | fio 3.40 × Sluice `5d94f23`，单 job 线程、warm-cache、btrfs/SATA | — |
| 不满足 | #604 G0：fio synthetic 不是 real consumer / 代表性 trace | — |

Phase B 的核心限制：D16/D64 增量没有同 build 因果分解；kernel/io-wq/IRQ 与 K/R/P/O 逐请求因果未闭合；record-root 只有 n=1；跨 build/跨会话的点估计不能当因果证据。

## 2. 经验一：先看“调用次数”，再看“单次成本”

### 2.1 COUNTED 事实

| 深度 | take/op | pending/op | pending/take | resolve iters/take | core 锁/op | harvest slots/harvest |
|---|---:|---:|---:|---:|---:|---:|
| D1 | 2.000000 | 1.000000 | 0.500000 | 1.000000 | 13.000000 | 1.0 |
| D4 | 3.499999 | 2.499999 | 0.714286 | 2.142857 | 12.999999 | 2.8 |
| D16 | 9.499953 | 8.499953 | **0.894736** | 6.263156 | 18.624953 | 8.9412 |
| D64 | 33.497575 | 32.497575 | **0.970147** | 22.313371 | 42.528826 | 32.9842 |

D16 每完成 1 个 I/O，约调用 `take` **9.5 次**，其中 **8.5 次得到 pending（89.47%）**。每次 pending 仍要付一次 `resolve_` 线性扫描 + 一次 `RequestCore::mutex_`，然后才在 `published` 判定处返回 pending。

### 2.2 SAMPLED 事实

| 根 | take self | cycles/op | take/op | cyc/take | resolve iters/take | harvest slots/harvest |
|---|---:|---:|---:|---:|---:|---:|
| `E5` A1 record rep1 | 2.44% | 4,787.93 | 2 | **58.41** | 1.00 | 1.0 |
| `E4` A2 record-root rep1 | 10.45% | 4,859.52 | 9.5 | **53.45** | 6.26 | ≈8.94 |
| `E3` A2 record-root rep1 | 11.68% | 4,550.59 | 9.5 | **55.95** | 6.26 | 8.94 |

`resolve_` 迭代/take 从 1.00 增至 6.26（约 6.3×），单次 take 成本没有单调上升；三个 root 各 n=1、跨会话，不能给 run-to-run CI。**8.94 是 adapter 的 harvest slots/harvest，不是 take 的扫描长度**，不能拿来解释单次 take。

### 2.3 推论

- **C3“`resolve_` 线性扫描 → O(1) 槽索引”**：无收益证据。单次 take 不随 resolve 迭代上升；D4 全链差 `<` D1。把 8.94 当 take 扫描长度是层级/单位错误。
- **C4“扫描长度是 D16 增量主因”**：现有证据不支持（弱证伪，非单因素因果证伪）；增量主要随 take 调用次数 `2→9.5` 增长。
- **C5“harvest 扫描是用户态主成本”**：harvest self 仅 1.72–1.88%，直接成本小；它是重复 take 的上游触发器。
- **C2“O(depth) harvest 槽扫描”**：并入 C1，不单独准入；优化扫描本身收益很小。

**可复用规则**：成本 ≈ 调用次数 × 单次成本。如果调用次数随深度增长，先减少调用；不要先优化单次成本。

## 3. 经验二：flat 份额是“CPU 退休周期归属”，不是“阻塞时间”

### 3.1 数字

- D16 record-root 总 cycles/op：`16,858,204,391 / 3,704,623 = 4,550.5857`；10,178 samples，perf 未报告 Lost（不等于零丢失）。
- libc `pthread_mutex_lock/unlock` flat self：**31.56% = 1,436.16 cyc/op**（top-2 30.34% = 1,380.65）。
- 调用数：**48.256 次/op = 24.128 对/op**；单次 **28.6–29.8 cyc/调用**，折合 **57.2–59.5 cyc/对**。
- futex：**0.001/op**；job on-CPU：**96–98%**；HITM：**0.0098/op**。

**单位教训**：把“pair 数”乘以“per-call 价”会少算一倍。旧稿的 `243–253 / 843–853 / [243,853] / 923.8 / 257–268` 已撤回；正确口径是 pair 数 × `57.2–59.5`。复算脚本新增 `calls/op == 2 × pairs/op` 检查（`lock_units_ok=True`），并在无 `bpf_locks` 行时 fail closed。

### 3.2 推论

- 31.56% 是**无竞争快路径 + 调用/PLT** 被 48.256 次调用乘出来的，不是锁等待。
- **C6“删 core mutex / lock-free consume”是 NO_GO**：`RequestCore::mutex_` 承担 publication、terminal visibility、reclamation、unique consumption 的唯一串行化；删除会 double accept/terminal/consume/duplicate free。
- **29% 不是 29% 锁等待**；不能据此提 lock-free 重写。

**可复用规则**：看到 flat mutex 高，先看 futex、off-CPU、on-CPU、HITM、runqueue；如果都没有，优先考虑“调用次数太多”，而不是“锁竞争”。

## 4. 经验三：D1 的固定 9 锁是“截距”，C1 之后才是下一个天花板

### 4.1 数字

| 深度 | sluice cycles/op | native cycles/op | 倍数 |
|---|---:|---:|---:|
| D1 | 4,129.33 | 1,439.82 | **2.87×** |
| D16 | 4,617.13 | 1,453.55 | **3.18×** |
| D64 | 10,244.86 | 1,418.56 | **7.22×** |

- C1 主要打 D16/D64 的“斜率”；D1/D4 的“截距”仍在。
- 一个 I/O 的生命周期被切成 `reserve/accept/claim + offer/release/begin/complete/claim/retire` **9 个固定 core 锁节点**（外加 `access_mtx_`/`dispatch_mtx_`）。
- core 锁计数闭合为 **`9 + T + 2P`**（`T = take/op`，`P = progress pass/op`），五 cell 残差 ≤2e−6（复算最差 7.1e−15）。
- **C7“合并 9 次固定锁”当前是 NOT_ADMITTED**：9 次是当前语义边界；合并属于重新设计，需要逐项指出冗余获取点 + 语义等价证明 + 单因素 A/B。

### 4.2 推论

- 如果产品目标主要是 D1–D4，C1 可能不够；真正的架构问题是状态机边界与临界区粒度。
- 如果目标在 D16+，C1 是第一步；C1 通过后再评估 C7。
- **不要用 D16 的优化去回答 D1 的成本。**

## 5. 经验四：ThreadPool 的瓶颈不是指令数，而是 cycles/CPU/handoff

### 5.1 数字

- D16：TP instr `7,902.23` vs sluice-uring `7,992.81`（**−1.1%**）；cycles `9,046.30` vs `4,617.13`（**+95.9%**）；CPU `14.644` vs `2.745 µs`（**+11.9 µs**）。
- consumer 臂：TP instr 约 +1.65%，cycles **+156.5%/+175.4%**，IOPS **−21.6%/−24.3%**。
- 这说明瓶颈在跨线程 handoff/调度/停顿，不是多跑了指令。

### 5.2 推论

- 查 C8 时不要只看 `perf stat` 指令数；要看 `topdown`、`LLC-load-misses`、`cycle_activity.stalls`、runqueue/唤醒延迟、cache-line bouncing。
- C8 仍是 `RESEARCH_ONLY`：本轮没有把 handoff 成本逐项拆开；不要把它混入 C1。

**可复用规则**：指令数不涨而 cycles 涨，优先查微架构停顿和跨核交接，不要查指令路径。

## 6. C1：唯一的 `EVIDENCE_READY`

### 6.1 机制

`harvest` 非破坏性扫描所有非空槽；对每个槽调用 `scope->take(ticket)`。pending 槽不释放，下一轮继续被扫描；pending 仍要付 `resolve_` + core mutex 才返回 pending。

C1 的方向：**ready-index / 单次消费**——让 `harvest` 只对“已就绪”的槽调用 `take`，pending 不再进入完整 `take/consume` 路径。

### 6.2 模型（INFERRED，不是净收益）

- pending 分摊 take+consume self：`670.8 × 0.894736 = 600.2 cyc/op`
- pending 对应 core 锁：`8.499953 对 × 57.22–59.52 cyc/对 = 486.4–505.9 cyc/op`
- 合计：**1,086.5–1,106.1 cyc/op**，占 D16 record 总量 **23.9–24.3%**

**这不是可省净收益**：它假设 pending 与 ready 的平均 self 相同、pending core 锁与全部 libc 锁平均价相同；新方案还要支付就绪登记/单次消费成本。净节省必须由同 build A/B 测出。

其他派生值（同为 `INFERRED`，不是测量）：

| 项 | 值 | 说明 |
|---|---:|---|
| C7 固定 9 对 core 锁 | **515.0–535.7 cyc/op** | `9 × 57.2–59.5`；不是“可删锁”的证据 |
| D64−D16 cycles 余量 | **2,862.2–2,917.3 cyc/op** | `5,627.73 − (1,342.7 + 1,367.8–1,422.8)`；模型残差，不是测量 |

### 6.3 可证伪预测与成功判据

| 指标 | 当前 D16 | 目标 |
|---|---:|---:|
| take/op | 9.5 | **≤1.85** |
| core 锁/op | 18.625 | **≤11.0** |
| D64 core 锁/op | 42.53 | **≤13.3** |
| pending/op | 8.5 | ≈0 |

不伤害：D16/D64 clean0 IOPS 不低于同 build A/A 极差（Phase B S2 实测最大 **2.196%**）；CPU/op 不升；p99 不劣化；语义门通过。

### 6.4 决策规则

| 结果 | 判定 |
|---|---|
| 机制达标 + 不伤害 + 语义通过 | `EVIDENCE_PROVEN`，提交 G2 提案给 Owner |
| 机制达标但 IOPS 中性 | 记 **“机制成立、吞吐中性”**；产品价值看 CPU/op 与 headroom |
| 机制不达标 | `NO_GO` 或 `INCONCLUSIVE` |
| 语义无法保持 | `SEMANTIC_NO_GO`，停止 |
| 测量无效/样本不足 | `INCONCLUSIVE`，不晋级 |

**禁止**：把 1,086.5–1,106.1 写成“已省 24%”；把采样份额当净因果；把 D16 结论外推到 D1–D4 或产品 API。

## 7. 不建议现在做的清单

| ID | 候选 | 状态 | 理由 |
|---|---|---|---|
| C3 | `resolve_` → O(1) 槽索引 | `NOT_ADMITTED` | 单次 take 不随扫描长度上升；无收益证据 |
| C4 | “扫描长度是 D16 增量主因” | `NO_GO` | 现有证据不支持；增量随 take 调用次数增长 |
| C5 | “harvest 扫描是用户态主成本” | `NO_GO` | harvest self 1.72–1.88% |
| C6 | 删 core mutex / lock-free consume | `NO_GO` | publication/terminal/reclamation/unique consumption 的必需串行化 |
| C7 | 合并固定 9 次 core 锁 | `NOT_ADMITTED` | 9 次是当前语义边界；需重新设计 + 语义证明 |
| C9 | io_uring 提交批处理 | `NOT_ADMITTED` | 两臂均 1.0000 enter/op；前提不成立 |
| C10 | research 计数器作为产品候选 | `NO_GO` | +124–+3,367 instr/op、IOPS −4.6%…−56.8%，是测量扰动 |
| C11 | consumer bench 作 fio adapter 代理 | `NOT_ADMITTED` | 64MiB tmpfs vs 1GiB btrfs、无 fio driver、每完成 wait_for；非同构 |
| C12 | kernel/io-wq/IRQ | `RESEARCH_ONLY` | 未归属；cycles:k 仅 fio 线程上下文；IRQ 未测 |
| C13 | `find_live_router_cookie_` 倒扫 | `NOT_ADMITTED` | self 1.35%、无扫描长度计数、无 A/B |
| C8 | ThreadPool handoff | `RESEARCH_ONLY` | 机制未识别；需 topdown/LLC/stalls 独立实验 |

## 8. 建议路线：G0 → G1 → G2

| 阶段 | 做什么 | 产出/门槛 |
|---|---|---|
| **G0** | 真实消费者/代表性 trace 准入；记录真实 iodepth 与 `getevents` 分布 | 决定目标是 D1–D4 还是 D16+；#604 G0 未满足前不能产品化 |
| **G1** | C1 单因素 A/B（ready-index/单次消费），保持 v2 语义 | 机制：take/op、core 锁/op 达预测降幅 ≥90%；不伤害：IOPS/CPU/p99 不劣化 |
| **G2** | 只有 C1 通过后，才评估 C7 的“合并临界区/减少状态转换”重构 | 语义等价证明 + 单因素 A/B；不是锁微优化 |
| 并行研究 | C8 TP handoff（topdown/LLC）；C12 kernel/io-wq/IRQ（仅当真实负载有 worker 参与） | 独立研究，不混入 C1 |

### 决策树

- 真实深度主要 **D1–D4** → 先回答固定 9 锁状态机的“截距”问题；C1 的收益较小，不应作为第一优先。
- 真实深度 **D16+** → C1 是第一个单因素 A/B；D16 + D64 都要跑。
- 真实负载有 **TP** → C8 独立立项；不要把 TP 的 cycles 异常混入 C1。
- 真实负载有 **io-wq/IRQ** → C12 独立扩测量；当前 cycles:k 只覆盖 fio 线程上下文。

## 9. 方法学检查单

1. **counted / sampled 分开**：调用次数/锁次数用 counted；函数占比只用 sampled，不能把采样份额当 per-op 函数计数。
2. **单因素**：一次只改一个机制；跨 root、跨会话、n=1 的点估计不能当因果证据。
3. **机制与性能分开**：机制用 counter build；性能必须用 release build；两者不能直接相减。
4. **不要加总 inclusive callgraph 百分比**：`getevents 49% → harvest 30% → take 28% → consume 17%` 是嵌套路径，不能相加。
5. **分母与窗口**：同一 run 的 `io_bytes/bs`；`ramp=0`；`task-clock:u` 是进程树 user+sys；PMU 的 time-running/scaling 要保留。
6. **证据完整性**：`analyze` 后重生成 SHA 清单；bpftrace 窗口必须覆盖 `attach + fio wall`；retry 不能把第一次 attempt 的计数复制到重试目录；lock 单位检查 `calls/op == 2 × pairs/op`。
7. **A/A 先行**：同 build A/A IOPS 极差 <10% 才继续；否则 `INCONCLUSIVE`。
8. **交错与样本**：ABBA/ABCCBA；至少 2 reps/arm；D16/D64 优先；记录真实 I/O 数、窗口、CPU scope。
9. **语义门**：唯一消费、回收谓词、陈旧 ticket `empty`、fio `staged/event` 形状、publication memory ordering；任何一条失败即停。
10. **边界声明**：fio synthetic 不是 real consumer；单 job 线程结论不外推到多 submitter/产品 API。

## 10. 开放问题

- 真实 workload 的 depth 与 `getevents` 分布是什么？这决定 C1 与 C7 的优先级。
- ThreadPool 是否在真实产品路径中？若在，C8 的 handoff 成本需要独立实验。
- D64 是否真实存在？D64 clean0 IOPS 已比 D16 低 39.3%，可能不是产品目标。
- 固定 9 锁的“截距”是否值得重构？需要状态机级设计与语义证明，不是微优化。
- kernel/io-wq/IRQ 与 K/R/P/O 逐请求因果仍未闭合；需要 root + tracepoint + IRQ 归因。
- #604 G0 的真实消费者/代表性 trace 从哪里来？

## 11. 链接

- Phase A 工具 PR：https://github.com/jnhu76/sluice-workload-bench/pull/5（merged `c66a3a9`）
- Phase A 工具 issue：https://github.com/jnhu76/sluice-workload-bench/issues/3（closed completed）
- Phase A 研究记录：https://github.com/jnhu76/sluice-workload-bench/issues/6
- Phase B 研究记录：https://github.com/jnhu76/sluice-workload-bench/issues/7
- Phase B 报告：https://github.com/jnhu76/sluice-workload-bench/blob/research/fio-userspace-cost-decomposition/docs/fio-integration/userspace/USERSPACE-COST-RESEARCH.md
- 可删除台账：https://github.com/jnhu76/sluice-workload-bench/blob/research/fio-userspace-cost-decomposition/docs/fio-integration/userspace/REMOVABLE-WORK-LEDGER.md
- C1 实验提案：https://github.com/jnhu76/Sluice/issues/642
- 热点台账：https://github.com/jnhu76/Sluice/issues/639
- 产品准入：https://github.com/jnhu76/Sluice/issues/604
- 历史门禁：https://github.com/jnhu76/Sluice/issues/579、https://github.com/jnhu76/Sluice/issues/595、https://github.com/jnhu76/Sluice/issues/621、https://github.com/jnhu76/Sluice/issues/623
- 方法论：https://github.com/jnhu76/Sluice/issues/622

## 12. 维护规则

- 本文只追加**新证据**，不改写历史判词；若新实验推翻旧结论，新增“修订”小节并保留旧值及撤回原因。
- 任何产品化结论必须回指 #604 G0/G1/G2 与 #642 的 A/B；本文不是准入凭据。
- 新数字必须带 `COUNTED / SAMPLED / INFERRED` 标签；跨 build/跨会话的点估计不得写成因果。
- 若 Sluice SHA 变化，先重跑 source audit 与 M-SEM，再引用本文的锚点。

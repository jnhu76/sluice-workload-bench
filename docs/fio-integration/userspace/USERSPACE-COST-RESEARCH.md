# 用户态成本研究（Phase B 最终报告）

- 判词：**`USERSPACE_COST_PARTIALLY_ATTRIBUTED`**
- 对象：Sluice `5d94f233967c3aef993eb19ba9e3252c532b0951`（产品代码）× fio-3.40 adapter
- 证据根（下文的 `E1`…`E5` 记号一律指这些目录，路径相对本仓库根）：
  - `E1` = `results/userspace-cost/session-001-baseline`（120 个 measured fio run + 6 prep；clean0 与 pmu-root g1/g2/g3）
  - `E2` = `results/userspace-cost/session-002-counters`（release/research ABBA + 5 个 trace-bpf；research 计数器）
  - `E3` = `results/userspace-cost/session-003-sync-consumer`（A2 record-root、BPF lock/sched、pmu-atomic/hitm/memorder、consumer bench）
  - `E4` = `results/attribution/session-002-root`（r2 root 会话：clean0/pmu/record/trace）
  - `E5` = `results/attribution/session-001`（r2 首个 root 会话：A1 record 等）
- 代码锚点记号：`S:` = `/home/jnhu/Source/Sluice/...`，`B:` = 本仓库 `...`，`D:` = release 反汇编地址（来自源码审计附录，见 `USERSPACE-COST-SOURCE-AUDIT.md` 的证据项 E1–E18）。
- 分类标签：**COUNTED**（fio 计数、100% time-running 的硬件计数、运行期计数器、eBPF 调用计数）、**SAMPLED**（`perf record` 样本份额或 `time-running < 100%` 的 PMU 事件）、**INFERRED**（由计数恒等式或分桶下界推出的派生量）、**NOT_IDENTIFIED**（本轮仪器覆盖不到、或无法归属）。
- **状态：Final for the current Phase B evidence set。归因不完整**（见 §K.1）；R1–R6 不在本轮范围，升级到 `USERSPACE_COST_ATTRIBUTED` 需要它们。本报告**不是** whole-system attribution。

## 0.1 判据、门禁与术语（自包含）

> 原始协议没有定义判词门槛；以下是本报告为使判词可复核而**显式化**的判据。它是事后定义，不是预注册。

### 0.1.1 判词判据

| 判词 | 定义（全部条件同时成立） | 本轮 |
|---|---|---|
| `MEASUREMENT_INVALID` | 任一 session 的 `sha256sum -c` 失败、invariants 失败、或分母恒等式（`io_bytes/bs==total_ios`）不成立 | 否（全部通过，§0.2） |
| `USERSPACE_INCONCLUSIVE` | 核心 per-op 总量或计数**不可复算/自相矛盾**（锁闭合不成立、flat 组加总明显偏离 100%、同一 run 的 instr 与 fio 计数冲突） | 否（闭合残差 ≤2e−6，组加总 100.00%） |
| `USERSPACE_COST_PARTIALLY_ATTRIBUTED` | ① 全部 per-op 总量与计数可复算；② 至少一层（scope / bridge / adapter / sync）有 counted + SAMPLED 归属；③ 但 {D16/D64 因果分解、同 build 对照、kernel/io-wq/IRQ、同二进制语义门、≥2 record roots 或 run-to-run CI} 中**至少一项缺失** | **是**（②=scope/bridge/sync；③=五项全缺） |
| `USERSPACE_COST_ATTRIBUTED` | 在 PARTIALLY 之上还满足：(a) D16/D64 增量有**同 build** 因果分解；(b) kernel/io-wq/IRQ 有界或归属；(c) M-SEM 在**当前二进制**重跑；(d) record ≥2 roots 或 run-to-run CI | 否（(a)–(d) 全缺） |
| `SEMANTIC_NO_GO` | 语义门失败，或候选**无法**在保持语义下实现 | 否（无失败；C6 只是候选级 NO_GO） |

**升级到 `ATTRIBUTED` 的最小证据**：R3（native 同 build record 分区）+ R4（D64 record/BPF）+ R5（同二进制 M-SEM）+ 至少两个独立 record roots（或 run-to-run CI）。四项缺一不可；仅靠再多的 SAMPLED 份额不能升级。

### 0.1.2 #604 门禁（G0–G4）

锚点：`docs/fio-integration/PHASE1-REPORT.md` §K。本报告内的定义：

| Gate | 准入条件 | 本轮位置 |
|---|---|---|
| **G0** Workload admission | real consumer 或可辩护的代表性 trace；冻结 oracle/依赖/预算/baseline | **未进入**：fio 参数化合成负载不构成 G0（§K） |
| **G1** Cost attribution | 实际瓶颈 + 必要/可避免工作 + 竞争性解释 | **部分供料**：本报告即 G1 方法演示，对象是合成负载 |
| **G2** Mechanism trial | 隔离 A/B + 因果反事实 | **未进入**：R1 待批 |
| G3 Transferability、G4 Product promotion | 端到端/邻近 regime/硬件敏感性；产品化决策 | 未进入 |

### 0.1.3 候选标签语义（本报告唯一准入语言）

| 标签 | 含义 | 需要什么 |
|---|---|---|
| `EVIDENCE_READY` | **可申请实验**（不是“已证明可删”） | counted 负担 + 可证伪预测 + 语义等价证明**计划** + 单因素设计；四项缺一不出此标签 |
| `EVIDENCE_PROVEN` | 实验后证明可删/可降 | 上述四项 + A/B 结果；**本轮未使用** |
| `RESEARCH_ONLY` | 机制/范围问题，不是优化候选 | 独立研究问题 |
| `NOT_ADMITTED` | 证据不足或前提不成立，不申请 | 需更多证据才能复议 |
| `NO_GO` | 现有证据不支持继续投入 | **不等于数学证伪**；可以是弱证伪、多源不一致或语义上不可做 |

> 因此 §J 的“REFUTED”一律按“现有证据不支持（非单因素证伪）”理解；唯一接近单因素的是 C6 的语义删除矩阵（源码级）。

### 0.1.4 记号表

| 记号 | 含义 | 锚点 |
|---|---|---|
| S1 / S2 / S3 | baseline / counters / sync-consumer 会话 | `E1` / `E2` / `E3` |
| S8 | `E2` 停止规则中的 A/A < 10% 门 | `E2/manifest.json` `stop_rules` |
| U5.1 / U5.2 | release vs research 的 IOPS / instr 扰动 | `E2/logs/perturbation-summary.txt` |
| U3.2 | core 锁残差检查（对 `9+T`） | `E2/logs/counts-summary.txt` |
| U3.3 | core lock 比值检查（本报告指出其量纲错误） | §F.2 |
| H1–H6 | COST-LEDGER 的热点台账；**H5** = capacity / repeated scan | `COST-LEDGER.md:170-177,414` |
| M-SEM | 语义等价门 | `results/userspace/instrumentation-smoke-001/manifest.json` |
| K / R / P / O | kernel completion / reap / publication / observation 四个未闭合通道 | `COST-LEDGER.md:236-252` |
| W1–W5 | 工作负载标签：W1=A1、W2=A4、W3=A2、W4=A5、W5=A3 | §B.0 |
| W-C2 | COST-LEDGER 的固定 20G/5.24M ios 窗口检查（**不是**本报告候选） | `COST-LEDGER.md:26-34` |
| C1–C13 | 优化/研究候选 | `REMOVABLE-WORK-LEDGER.md` |

---

## 0.2 本轮实际执行过的校验（只读）

| 校验 | 命令 | 结果 |
|---|---|---|
| 哈希清单 | `sha256sum -c sha256-manifest.txt --quiet`（在 `E1`/`E2`/`E3`/`E2-pass1-invalidated` 各自目录） | 四者均 `exit=0`；`E1` 936/936、`E2` 340/340、`E3` 179/179；`session-002-counters-pass1-invalidated` 清单 338 条而磁盘 339 文件（`INVALIDATED.json` 在清单生成后写入，未被哈希） |
| 工具单测 | `python3 -I -m unittest discover -s tools/attribution/tests` | `Ran 64 tests ... OK` |
| 报告数字复算 | `python3 -I tools/attribution/recompute_userspace_report.py`（只读；解析 `E1`/`E2`/`E3` 的 raw，不跑 fio/perf） | `identities: closure_ok=True flat_sum_ok=True flat_cycles_ok=True lock_units_ok=True`（exit 0）；S1 15 行 per-op、五 cell 锁闭合、record-root 八组（4,550.59 cyc/op）逐项复现；输出即 §B/§D/§F 的表 |
| lock 单位检查 | 同上脚本的 `table_lock_units` | `calls/op == 2 × pairs/op` 三 run 残差 ≤5.28e−4（阈值 1e−3，BPF attach/detach 边界）；per-call 29.76、per-pair 59.52（top-2 57.22）cyc；C1 模型 1,086.5–1,106.1、C7 515.0–535.7 |
| 计数器单点复核 | `python3 -I -c`（读 `E2/runs/*/counters.json`） | A2 research rep1：`25,521,449,756/3,010,089=8,478.6363` instr/op；`56,062,766/3,010,089=18.624953` core lock/op；`179,099,335/3,010,089=59.499681` resolve iters/op；与 `E2/logs/counts-summary.txt` 一致 |
| 锁闭合复算 | `python3 -I -c`（用 counts 的 harvest/getevents 代入 `9+T+2P`） | 五 cell 残差 ≤ 2e−6（§F.2） |
| record-root 复算 | `python3 -I -c`（`E3` 的 `perf-report-flat.txt` 的 Event count ÷ fio `total_ios`） | `16,858,204,391/3,704,623=4,550.5857` cycles/op；八组 flat 分组加总 = 100.00%（§B.3） |
| `.text` 同一性 | `objcopy -O binary --only-section=.text` + `sha256sum` + `readelf -n` | `fio-sluice-ring.so` 与 `fio-sluice-ring-sym.so` 的 `.text` 均为 `cfe811b7…`（release Build ID `0fea7122…`、sym `297ff32e…`）；research 为 `c85a7f…`/`c81afce2…`。record-root 的符号归因因此适用于 release 代码 |
| 二进制在场性 | `sha256sum` 遍历 `build/**/*.so*` | 当前 release `b1e28d69…`、sym `ca5fb7d7…`、research `ca4a6c45…`；**`E1` 回执里的 release `3ba98405…` 已不在 `build/` 中**（见 §H.6） |

> 本轮**没有重跑**任何 Phase B 测量（无 sudo、无 perf、无 fio）。上面的“复算”全部是对已归档 raw 的重新解析；未运行的项目在 §G 明确列出。

---

## 0.3 裁决员六问：速答

**Q1 每阶段实际机器工作（MEASURED_COST）**
提交侧每 accepted I/O：`sluice_queue` 1 → `RequestScope::submit_*` 1 → `AsyncIoContext::submit_*` 1（`access_mtx_` 1 对）→ 虚 `backend_->submit_*` 1 → `RequestCore::reserve/accept/claim_execution`（3 次 core 锁）→ `io_uring_get_sqe`+prep（liburing 头内 inline）→ 1 次 `io_uring_submit`。完成侧：1 CQE → `handle_one_cqe`（`dispatch_mtx_`）→ `offer_terminal`/`release_execution`/`begin_publication`/`complete_publication`/`claim_observer_delivery`/`retire_observer_delivery`（6 次 core 锁）→ 1 次 publication → 1 次 `sluice_event`。观察侧：1 次 `getevents`、harvest≈1.06/op（D16）、take/consume ≈ `K+k`、每 progress pass 2 次 core 锁。A2/D16 的 **SAMPLED** flat self 八组见 §B.3（Scope 531.5、Adapter 241.6、Bridge 139.3、libc lock 1,436.2、publication 63.3、other Sluice 689.9、fio driver 1,168.6、未识别 280.3 cyc/op；合计 4,550.6）。**COUNTED 的结构性每 op 次数（所有深度）**：1 queue、1 SQE、1 enter、1 CQE、1 getevents、1 `sluice_event`、1 条 64 位 `div`、0 堆分配。**随深度变化的每 op 次数（D16 实测；D1/D4/D64 见 §D.1）**：harvest 1.06、take/consume 9.5、core 锁 18.625、libc lock/unlock 48.256（BPF，release+sym；core 计数来自 research）、core atomics 3.005。

**Q2 DEPTH_SCALING（COUNTED）**
native 基本平坦（2,757.7→2,787.3 instr/op；1,418.6→1,453.6 cyc/op）。sluice-uring：6,430.3→6,038.0→7,992.8→25,256.6 instr/op（D4/D1=0.939、D16/D1=1.243、D64/D1=3.928），4,129.3→3,710.3→4,617.1→10,244.9 cyc/op（D64/D1=2.481）。TP：10,031.1→6,866.9→7,902.2→24,900.4 instr/op（D4/D1=0.685、D64/D1=2.482），12,091.1→8,692.7→9,046.3→15,079.5 cyc/op（D64/D1=1.247）。随深度增长的是**调用次数**：take/op 2→3.5→9.5→33.5（≈C/2+1.5）、pending/op 1→2.5→8.5→32.5（≈C/2+0.5）、resolve iters/op 2→7.5→59.5→747.4、harvest 扫描均值 1.0→2.8→8.94→32.98、core 锁/op 13→13→18.625→42.53（全部 **COUNTED**）。单次 take 的成本没有随 `resolve` 迭代数上升（**SAMPLED**，n=1/root，见 §D.3）。D1→D4 的下降是事实（D1 6,430.3 → D4 6,038.0 instr/op）；一种解释（**未单独验证**）是 D1 每完成约 1 getevents+2 harvest+2 take 的固定开销占主导，D4 的 harvest/op 已降到 1.25。D64 对 sluice-uring 已过拐点：clean0 IOPS 230.4k vs D16 379.6k（−39.3%）。A3（64k seqread）不属于 4k 深度序列。

**Q3 NECESSARY_COST（`SEMANTICALLY_NECESSARY_UNDER_CURRENT_DESIGN`）**
5 把 `std::mutex` + 1 个无锁 `RequestScope`，逐锁职责与删除后果见 §F.1（成本分类见 §F.3）；计数修正为 **core 锁 = 9 + T + 2P**（P = 每 op 的 progress pass 数），五 cell 与实测计数完全闭合（§F.2）。原 U3.2 的 A1 残差 +2.0 不是测量异常，而是漏了 2P。**必要 ≠ 最小**：锁的存在是当前设计的正确性前提；每次获取能否合并/去除，需要重新设计 + 单因素 A/B（§F.5）。“无稳态竞争”只在 fio 单 job 线程下测得（§F.3 scope）。

**Q4 POTENTIALLY_REMOVABLE_COST**
仅 C1（pending 重复 take/consume → ready-index/单次消费）为 **EVIDENCE_READY**，且只申请 G1/G2 单因素 A/B；C2（O(depth) harvest 槽扫描）并入 C1；C3/C7/C9/C11/C13 **NOT_ADMITTED**；C4/C5/C6/C10 **NO_GO**；C8/C12 **RESEARCH_ONLY**。逐项见 `REMOVABLE-WORK-LEDGER.md` 与 §I。

**Q5 UNIDENTIFIED_COST**
内核/io-wq/IRQ/softirq 未归属（`cycles:k` 只覆盖 fio 线程上下文；io-wq 的 CQE 每 run 仅 1 个、不可归属；IRQ 未探）；D16 未识别 280.3 cyc/op（4.43% 低于阈值符号 + 1.03% vdso + 0.70% unknown）；libc mutex 1,436.2 cyc/op 的 core/fio 拆分（core 按调用数分摊**估计** 1,108.6；不是严格上界，真界 0–1,436.2）；D16 全链 +5,219 instr/+3,164 cycles 的因果拆分；D64 的 cycles 余量约 2.86–2.92k/op（公式见 §H.13；锁按 pair 计价订正）；TP 的“指令不涨而 cycles 涨”；启动/收尾的 `MODE_OR_TIMING_EFFECT_NOT_IDENTIFIED`；A2 pmu 跨 abort/resume；`E1` 二进制不可复核；M-SEM 未对当前二进制重跑；record n=1；U3.3 量纲错误；achieved depth 为 INFERRED。逐条见 §H。

**Q6 #604 准入建议**
只有 C1 值得申请**下一阶段研究准入**（G1/G2 单因素 A/B），不是产品准入。**#604 G0 仍未满足**：fio synthetic 不是 real consumer / 代表性 trace（`docs/fio-integration/PHASE1-REPORT.md` §K：「G0 Workload admission … 未进入」）。C2 并入 C1 同一实验；C8/C12 是独立机制研究；C3/C7/C9/C11/C13 不申请；C4/C5/C6/C10 为 NO_GO。**本轮零产品结论**；判词不代表可以修改 Sluice。

---

## A. Source Reality（源码现实）

### A.1 提交与完成两条链（静态，COUNTED 结构）

| 阶段 | 调用链 | 每 accepted I/O | 证据 |
|---|---|---|---|
| queue | fio `td_io_queue` → `ops->queue`=`sluice_queue` | 1 | `B:adapters/fio/src/queue.cpp:35-62`；`USERSPACE-COST-SOURCE-AUDIT.md` 表 1 S1 |
| scope submit | `sluice_queue` → `RequestScope::submit_read/write/sync_*` | 1 | `B:adapters/fio/src/queue.cpp:90-121`；`S:include/sluice/async/request_scope.hpp:56-57` |
| ctx submit | `RequestScope::submit_*` → `AsyncIoContext::submit_*`（`access_mtx_` 全程持有） | 1 + 1 对 `access_mtx_` | `S:src/async/request_scope.cpp:152,178,202,226`；`S:src/async/async_io_context.cpp:249-269` |
| backend submit | 虚调用 `backend_->submit_*` → `submit_request<Op>` 模板（已内联） | 1（虚调用） | `D:0x4c6e`；`S:src/async/async_io_context.cpp:259` |
| core 接受 | `reserve` → `accept` → `dispatch_after_accept` → `dispatch_one_locked` → `claim_execution` | 3 次 core 锁 | `S:src/async/uring_backend.cpp:447,514,527`；`S:src/async/uring_transport.cpp:83,152`；`S:src/async/detail/request_core.cpp:166,187,368` |
| 提交 | `io_uring_get_sqe`/`prep_read`/`sqe_set_data64`（liburing 头内 `static inline`）+ `io_uring_submit` | 1 SQE + 1 enter | `S:src/async/uring_transport.cpp:141-189,218-235`；`D:0x13bab` |
| CQE | 内核写 CQ ring；`io_uring_peek_batch_cqe` 用户态 peek（0 syscall） | 1 CQE | `S:src/async/uring_completion.cpp:200-229`；`S:src/async/uring_backend.cpp:636` |
| 完成处理 | `handle_one_cqe`（`dispatch_mtx_`）→ `finalize_operation_terminal_` → `offer_terminal`/`release_execution`/`retire_router_entry_` | 1 + 2 次 core 锁 + 1 次 `dispatch_mtx_` | `S:src/async/uring_completion.cpp:70-95,99-198` |
| 发布 | 同一次 `poll` 内 `publish_one` → `begin_publication`/`complete_publication`/`claim_observer_delivery`/`retire_observer_delivery` | 4 次 core 锁 + 1 次 publication | `S:src/async/uring_backend.cpp:578-614,636-657` |
| 观察 | `getevents` → `harvest` → `take`（→ `RequestBridge::consume`）→ `sluice_event` | 1 getevents、≈1.06 harvest、T 次 take/consume | `B:adapters/fio/src/events.cpp:247,262,298`；`S:src/async/request_scope.cpp:237-244`；`S:include/sluice/async/request.hpp:146-159` |

### A.2 源码审计确定的五个结构性事实（静态，非计时）

1. **只有 5 把 `std::mutex`**：`RequestCore::mutex_`、`UringAsyncBackend::dispatch_mtx_`、`ThreadPoolBackend::work_mtx_`、`AsyncIoContext::access_mtx_`、`ProgressSource::mtx_`（`S:include/sluice/async/detail/request_core.hpp:295`、`uring_backend.hpp:320`、`threadpool_backend.hpp:247`、`async_io_context.hpp:336`、`progress_source.hpp:491`）。不存在 `ThreadPoolBackend::queue_mtx_`；线程池的 `dispatch_` 与 `publication_pending_` 同由 `work_mtx_` 保护（`S:include/sluice/async/detail/threadpool_backend.hpp:129-133,240-251`）。
2. **两处 O(C) 线性扫描都在 scope/后端层，不在 `RequestCore`**：`RequestScope::resolve_` 从 index 0 扫到 `capacity_` 并逐字段比对 `RequestId`（`S:include/sluice/async/request_scope.hpp:114-129`；`D:0x9320` 起的步长 0x40 循环）；`UringAsyncBackend::find_live_router_cookie_` 对每个 CQE 倒扫 router（`S:src/async/uring_transport.cpp:551-557`）。**`RequestCore` 的查找是 O(1)**：`resolve_internal_` 直接用 `id.slot.value` 索引并校验 context/phase/generation（`S:src/async/detail/request_core.cpp:121-145`），`resolve_public_` 再加 `binding_live` 检查（`:147-160`）。把 take 的全部 self 成本叫“core 查表”是错的。
3. **关键内联已经发生**：`RequestCore::consume_public_result`、`RequestScope::resolve_/reserve_slot_/release_slot_`、`deliver_event` 在 release 中全部内联（独立符号零调用点或仅冷路径调用）；`ReferenceReadySink::on_ready` 被去虚化成“比较 vtable 槽后跳过”（`D:0x10b93-0x10ba4`）。这解释了 `record-root` 里为什么没有 `consume_public_result` 的独立行，而 `take`/`consume` 有。
4. **每 accepted I/O 稳态零堆分配、1 条 64 位 `div`**（`unordered_map<fd,…>` 桶取模，`B:adapters/fio/src/queue.cpp:56`；`D:0x3fc3`）、3 次 `lock` 前缀 RMW（`live_cookies_++/--`、`submit_flushes_++`）。
5. **提交不做跨 io_u 批处理**：`dispatch_after_accept` 在 drain 完 dispatch 队列后无条件 `submit_transport_locked()` → `io_uring_submit`（`S:src/async/uring_transport.cpp:87,218-235`）；fio 默认 `iodepth_batch_complete_min=max=1`，因此 `getevents`/`commit`/`event` 在稳态≈1 次/完成。

### A.3 边界（必须与 §B 的数字一起读）

- 以上是**调用图与次数**，不是时间。单次 mutex、单次 O(C) 扫描、单条 `div` 的周期成本**没有**静态推导。
- 每次 `getevents` 的 harvest 轮数 A **无静态上界**（稳态典型 2：`harvest` 在 `poll_progress` 之前，`B:adapters/fio/src/events.cpp:247,262`）。本报告的 count 值来自运行期计数器，不是这个静态模型。
- 内核侧落点（内联执行 vs io-wq）、eventfd 合并、IRQ/softirq 在仓库内不可静态推导 → `NOT_IDENTIFIED`。

---

## B. PMU Cost Map（S1：5 cell × 3 引擎）

### B.0 工作负载与 run 设计矩阵

**工作负载（取自各 run 的 `fio-job.fio`；COUNTED 配置）**

| 标签 / cell | rw | bs | 请求 iodepth | numjobs / thread | direct / invalidate | size | runtime / ramp | batch |
|---|---|---|---:|---|---|---|---|---|
| W1 / A1 | randread | 4k | 1 | 1 / 1 | 0 / 0 | 1G | 10s / 0 | 默认 min=max=1 |
| W2 / A4 | randread | 4k | 4 | 1 / 1 | 0 / 0 | 1G | 10s / 0 | 默认 |
| W3 / A2 | randread | 4k | 16 | 1 / 1 | 0 / 0 | 1G | 10s / 0 | 默认 |
| W4 / A5 | randread | 4k | 64 | 1 / 1 | 0 / 0 | 1G | 10s / 0 | 默认 |
| W5 / A3 | read（顺序） | 64k | 4 | 1 / 1 | 0 / 0 | 1G | 10s / 0 | 默认 |

所有 cell 另有 `randseed=81`、`group_reporting=1`、`buffer_pattern=0x5a`，进程 `taskset -c 0-9`；文件为 `build/scratch/attribution/{A1..A5}.bin`（1 GiB，sha256 `518c5131…`）。`iodepth_batch_complete_*` 未被覆盖 → fio 默认 `min=max=1`（`options.c:2299-2325`、`init.c:768-771`）。**W-C2 是另一套固定 `io_size=20G/5.24M ios` 的窗口检查，不属于 W1–W5。**

**run 设计（`E1`：120 measured + 6 prep）**

| 窗口 | 组成 | run 数 | 用途 |
|---|---|---:|---|
| clean0 | 5 cell × 3 引擎 × 2 rep | 30 | IOPS 基线、A/A |
| pmu-root-g1 | 同上，每 rep 拆成一个独立 fio 窗口 | 30 | `instructions:u`、`instructions:k`、`task-clock` |
| pmu-root-g2 | 同上 | 30 | `cycles:u`、`cycles:k` |
| pmu-root-g3 | 同上 | 30 | branches / branch-misses / L1-dcache / LLC（multiplexed，**SAMPLED**，不进入 §B.1）+ cs / migrations |
| prep | A1/A2/A4 各 write + warm（A3/A5 复用上一会话/冒烟的文件，见 `E1` 回执） | 6 | 文件准备 |

因此 120 measured = 30 clean0 + 90 pmu group run（30 个 pmu rep × g1/g2/g3）。`E2` = 5 cell × (release×2 + research×2) ABBA = 20 measured + 5 trace-bpf + 10 prep；`E3` 的构成见 §G.1。

### B.1 per-op 总表（**COUNTED**，`E1`）

分母一律为同一 run 的 `read.total_ios`（`io_bytes/bs` 恒等式 120/120 通过）；`instr/op` 取 pmu-root-g1、`cycles/op` 取 g2、`cpu-us/op` 为 `task-clock`（进程树 user+sys）；`clean0 IOPS` 与 `pmu IOPS` 的口径见下。

| cell / 深度 | 引擎 | instr/op (u) | cycles/op (u) | cpu µs/op | clean0 IOPS | pmu IOPS |
|---|---|---|---|---|---|---|
| A1 / D1（INFERRED 1.000） | io_uring | 2,757.73 | 1,439.82 | 1.595 | 625,123.3 | 640,469.2 |
| A1 / D1 | sluice-uring | 6,430.28 | 4,129.33 | 2.444 | 391,315.8 | 417,810.8 |
| A1 / D1 | sluice-threadpool | 10,031.14 | 12,091.07 | 31.016 | 38,618.7 | 34,771.3 |
| A4 / D4（3.995） | io_uring | 2,777.59 | 1,442.88 | 1.644 | 579,163.4 | 621,165.0 |
| A4 / D4 | sluice-uring | 6,038.03 | 3,710.30 | 2.452 | 419,886.9 | 416,464.2 |
| A4 / D4 | sluice-threadpool | 6,866.92 | 8,692.70 | 16.148 | 189,705.5 | 206,368.2 |
| A2 / D16（15.951） | io_uring | 2,773.65 | 1,453.55 | 1.685 | 606,999.2 | 605,776.8 |
| A2 / D16 | sluice-uring | 7,992.81 | 4,617.13 | 2.745 | 379,601.0 | 371,948.1 |
| A2 / D16 | sluice-threadpool | 7,902.23 | 9,046.30 | 14.644 | 218,538.0 | 231,276.4 |
| A5 / D64（63.681） | io_uring | 2,787.33 | 1,418.56 | 1.736 | 578,453.3 | 587,759.6 |
| A5 / D64 | sluice-uring | 25,256.55 | 10,244.86 | 4.402 | 230,446.5 | 231,914.6 |
| A5 / D64 | sluice-threadpool | 24,900.41 | 15,079.48 | 20.366 | 155,889.8 | 158,346.7 |
| A3 / 64k seq D4 | io_uring | 2,758.12 | 1,663.03 | 8.197 | 124,786.9 | 124,751.3 |
| A3 / 64k seq D4 | sluice-uring | 5,983.65 | 4,357.99 | 8.809 | 117,007.9 | 116,090.3 |
| A3 / 64k seq D4 | sluice-threadpool | 6,873.39 | 8,870.87 | 19.690 | 207,974.4 | 178,814.0 |

深度是 fio `iodepth_level` 桶下界加权（**INFERRED**），不是逐 IO 深度；`>=64` 记 64。

**窗口语义（与上表一一对应）**：`instr/op` ← pmu-root-g1；`cycles/op` ← pmu-root-g2；`cpu µs/op` ← g1 的 `task-clock`；`clean0 IOPS` = clean0 两 rep 均值；**`pmu IOPS` = g1 两 rep 均值**（g2/g3 不进本表：A1 native g1 640,469 vs g2 633,856 vs g3 636,036，差约 1.0–1.6%）。g3 的 branches/L1/LLC 是 multiplexed SAMPLED，未进入任何 counted per-op。`task-clock` 的定义见 §B.4。

> 复算注：本表三引擎 × 五 cell 的 instr/cycles/CPU/IOPS 与本报告执行过的 `python3 -I` 重新解析逐项一致（脚本只读 `perf-stats.json`、`denominator.json`；`task-clock` 由 msec 换算 µs 后除以 `total_ios`）。`A2__sluice-uring__pmu-root-g2__rep1/rep2` 的原始 cycles/op 为 **4,577.14 / 4,657.12**（均值 4,617.13），不是证据审计文中写的 4,732.9/4,539.7；以 raw 为准（§H.5）。

### B.2 引擎差（**COUNTED** 派生，同 run 同分母；`E1`）

| cell | sluice−native instr/op（%） | sluice−native cycles/op（%） | sluice−native CPU µs | TP−sluice instr/op（%） | TP−sluice cycles/op（%） |
|---|---|---|---|---|---|
| A1 | +3,672.55（+133.2%） | +2,689.51（+186.8%） | +0.849 | +3,600.86（+56.0%） | +7,961.74（+192.8%） |
| A4 | +3,260.44（+117.4%） | +2,267.42（+157.1%） | +0.808 | +828.89（+13.7%） | +4,982.40（+134.3%） |
| A2 | +5,219.16（+188.2%） | +3,163.58（+217.6%） | +1.060 | −90.58（−1.1%） | +4,429.17（+95.9%） |
| A5 | +22,469.22（+806.1%） | +8,826.30（+622.2%） | +2.666 | −356.14（−1.4%） | +4,834.62（+47.2%） |
| A3 | +3,225.53（+116.9%） | +2,694.96（+162.1%） | +0.612 | +889.74（+14.9%） | +4,512.88（+103.5%） |

### B.3 A2/D16 的 `record-root` flat self 八组（**SAMPLED**，`E3`，n=1）

总 cycles/op = `16,858,204,391 / 3,704,623 = 4,550.59`；`Samples: 10K`，`Lost 0`。八组是对 flat 表的**互斥分组**（组内符号逐一列出，可复算加总 = 100.00%）。

| 组 | 成员（flat self 符号） | 份额 | cyc/op | 类 |
|---|---|---|---|---|
| Scope | `RequestScope::take` | 11.68% | 531.5 | SAMPLED |
| Adapter | `sluice_queue` 2.88 + `harvest` 1.88 + `sluice_getevents` 0.55 | 5.31% | 241.6 | SAMPLED |
| Bridge | `RequestBridge::consume` | 3.06% | 139.3 | SAMPLED |
| libc lock | `pthread_mutex_lock` 16.03 + `__GI___pthread_mutex_unlock_usercnt` 14.31 + `pthread_mutex_unlock@@GLIBC` 0.69 + `pthread_mutex_unlock@plt` 0.33 + `pthread_mutex_lock@plt` 0.20 | 31.56%（top-2 30.34%） | 1,436.2（top-2 1,380.6） | SAMPLED |
| publication | `publish_one` 0.93 + `begin_publication` 0.23 + `complete_publication` 0.23 | 1.39% | 63.3 | SAMPLED |
| other Sluice | `dispatch_one_locked` 3.30、`account_transport_result_locked` 1.50、`dispatch_after_accept` 1.46、`finalize_operation_terminal_` 1.41、`find_live_router_cookie_` 1.35、`handle_one_cqe` 1.35、`submit_read` 1.26、`RequestScope::submit_read` 0.87、`try_reclaim_` 0.65、`retire_router_entry_` 0.59、`accept` 0.33、`release_execution` 0.31、`reserve` 0.28、`AsyncIoContext::submit_read` 0.28、`claim_execution` 0.22 | 15.16% | 689.9 | SAMPLED |
| fio driver | `get_io_u` 2.89、`__fio_gettime` 2.87、`do_io` 2.22、`axmap_isset` 2.02、`td_io_queue` 1.87、`io_completed` 1.43、`add_lat_sample` 1.37、`get_next_rand_offset` 0.93、`add_clat_sample` 0.90、`get_next_offset` 0.80、`account_io_completion` 0.74、`io_u_mark_depth` 0.74、`__get_next_rand_offset` 0.72、`put_file` 0.63、`put_io_u` 0.58、`io_u_queued_complete` 0.58、`__get_io_u` 0.54、`io_queue_event` 0.52、`calibrate_cpu_clock` 0.51、`axmap_set_nr` 0.51、`td_io_prep` 0.45、`unlock_file` 0.36、`td_io_getevents` 0.36、`ntime_since` 0.27、`utime_since` 0.23、`lock_file` 0.22、`axmap_find_first_free` 0.21、`axmap_next_free` 0.21 | 25.68% | 1,168.6 | SAMPLED |
| 未识别 | 低于 0.2% 阈值的未列符号 4.43 + `[vdso]` 1.03 + `[unknown]` 0.70 | 6.16% | 280.3 | SAMPLED（该桶只是样本归属；内核/io-wq 仍是 NOT_IDENTIFIED） |

合计：`531.5+241.6+139.3+1,436.2+63.3+689.9+1,168.6+280.3 = 4,550.7`（表内为 1 位小数四舍五入；按 2 位小数复算为 `531.51+241.64+139.25+1,436.16+63.25+689.87+1,168.59+280.32 = 4,550.59`，即脚本输出）。

**同一 flat self 的 stage 分组（SAMPLED，本报告定义）**——回答“每阶段多少钱”，但 libc mutex 与 fio driver 无法分阶段：

| 阶段 | 归属符号（flat self） | 份额 | cyc/op |
|---|---|---:|---:|
| submit | `sluice_queue` + `RequestScope::submit_read` + `AsyncIoContext::submit_read` + `UringAsyncBackend::submit_read` + `dispatch_after_accept` + `dispatch_one_locked` + `account_transport_result_locked` + `reserve` + `accept` + `claim_execution` | 12.38% | 563.4 |
| completion / publication | `handle_one_cqe` + `find_live_router_cookie_` + `finalize_operation_terminal_` + `release_execution` + `begin_publication` + `complete_publication` + `publish_one` + `retire_router_entry_` + `try_reclaim_` | 7.05% | 320.8 |
| observation | `take` + `consume` + `harvest` + `sluice_getevents` | 17.17% | 781.3 |
| libc mutex（跨全部阶段） | 全部 `pthread_mutex*`（含 PLT） | 31.56% | 1,436.2 |
| fio driver | `object=fio` | 25.68% | 1,168.6 |
| 未识别 | 低于阈值 + vdso + unknown | 6.16% | 280.3 |

阶段归属按符号名做的一次划分；`try_reclaim_` 在 consume 内、`release_execution` 在完成链内，因此这是**近似**（同一符号可能跨阶段）。六组加总 = 100.00%。

**不得相加**：`perf-report.txt` 的 children 份额是路径总量且彼此嵌套——`sluice_getevents` 49.02% → `harvest` 30.50% → `take` 28.47% → `consume` 16.78%（其中 lock 7.29% + unlock 5.42%）；`sluice_queue` 21.34% → `submit_read` 17.58% → `dispatch_after_accept` 9.58%；`poll_progress` 17.97% → `reap_cqes` 9.43% → `handle_one_cqe` 8.59%；`publish_one` 6.48%。这四组互相重叠，也不能与上表 flat self 相加。

### B.4 内核侧（**COUNTED**，`E1` g1/g2 的 `:k`）

| cell | native instr:k | sluice instr:k | native cyc:k（g2 均值） | sluice cyc:k（g2 均值） | sluice−native cyc:k |
|---|---|---|---|---|---|
| A2/D16 | 2,676.2 | 2,656.7 | 4,287.9 | 4,661.9 | +374.1（+8.7%） |
| A3/64k | 3,879.9 | 3,870.1 | 26,107.3 | 26,036.6 | −70.7（−0.3%） |

A2 的 g2 原始 rep 值：native 4,273.8 / 4,301.9，sluice 4,695.2 / 4,628.7（本报告复算）。两臂的 `instr:k` 差在 −0.7%…−0.8% 内，`cyc:k` 差 +8.7%（均值）。

- `:k` 只覆盖 fio 进程树的内核态，**不含** io-wq/kworker；因此“内核侧几乎相同”只排除“fio 上下文内的内核膨胀”，**不能**推出“所有 kernel work 相同”。A2 trace 里 native `@enter=4,664,725`、sluice `@enter=3,208,449` + `@cqe[iou-wrk-]=1` + `@punt=1`（`E4` trace-bpf），enter/op 都是 1.0000，但 punt/worker CQE 与 enter flags（native 带 `IORING_ENTER_GETEVENTS`、sluice flags=0 另走 wait）不同 → 因此 `COST-LEDGER.md` 的 1.0000 enter/op 结论只能当“提交形状”，不能当“内核工作量等价”。
- TP 的 `task-clock/runtime` 为 3.22–3.52（`E1`），是多线程聚合，**不能**当作单核 per-op CPU（但可作为聚合 CPU 成本，见下）。

**`task-clock/op` 的定义**：进程树所有线程的 CPU 时间（user+sys）÷ `total_ios`。对 native/sluice-uring（单线程）它≈该线程的 per-op CPU；对 TP 它是 3.22–3.52 个线程的**聚合** CPU。因此它可以用于“CPU 成本”比较（§B.1 的列），但**不是**单线程 per-op CPU，也不是延迟；C8 的 +11.9 µs 是聚合增量。

### B.5 本轮 PMU 数字的三个限制

1. **窗口**：A1/A2 为 `time_based runtime=10s ramp=0`；**W-C2** 为固定 20G/5.24M ios。跨模式差 native 1.01%、sluice 7.41%、TP 4.14%，统一标 `MODE_OR_TIMING_EFFECT_NOT_IDENTIFIED`，**不得**当启动/收尾成本（`COST-LEDGER.md:26-34`）。
2. **跨构建**：`E1` 的 release `.so` 为 `3ba98405…`（`.text` `36a56c45…`），`E2/E3` 为 `b1e28d69…`（`.text` `cfe811b7…`）。S2 release 与 S1 的 instr/op 在 **0.016%/0.026%/0.050%/0.001%/0.074%** 内复现（A1/A4/A2/A5/A3），指令深度表有跨 build 旁证；**cycles/组件/TP 的跨 build 同一性未建立**。
3. **噪声**：`E1` A/A 为 n=2 观测极差（最大 clean0 IOPS 12.55% @A4 TP、10.41% @A4 io_uring；PMU IOPS 18.41% @A1 TP；cycles 8.76% @A1 sluice；CPU 20.17% @A1 TP），不是 CI。A4 clean0 的 10.41% 若被当作 S8 的 10% 门会触发，但 `E1` 的协议本身没有 10% 阈值。

### B.6 证据依赖映射（`E1` 二进制不可复核时哪些结论降级）

| 结论 | 证据源 | 若 `3ba984…` 不可复核 |
|---|---|---|
| S1 绝对 per-op（instr/cycles/CPU/IOPS）、深度倍率、A/A | `E1` only | instr/op 有 S2 的 ≤0.074% 旁证；**cycles/CPU/IOPS 降级为不可独立复核**（描述性数字可用，不能作跨 build 结论） |
| core 锁计数、take/pending、闭合 | `E2` | 不受影响 |
| flat 八组、mutex/futex/HITM、consumer | `E3` | 不受影响（sym `.text` 与当前 release 一致） |
| 内核 `:k` 与 TP 的 per-op | `E1` only | **不可独立复核** |
| record 扰动表 | `E3`/`E2` | 不受影响 |
| 语义门 | smoke-001（`3ba984`+`ece910`） | 未覆盖当前 `b1e28`+`ca4a6c` → 不成立 |

---

## C. Adapter Cost（fio adapter 自身）

### C.1 每 op 的 adapter 工作量（COUNTED 调用数 + 源码）

| 项 | 每 op | 类 | 证据 |
|---|---|---|---|
| `sluice_queue` 调用 | 1 | COUNTED | `B:adapters/fio/src/queue.cpp:35`；`E2` `submit_read/op=1.000000` 五 cell 全部 |
| fd→access 哈希查找（含 1 条 64 位 `div`） | 1（命中 1 节点） | COUNTED（静态）/周期成本 NOT_IDENTIFIED | `B:adapters/fio/src/queue.cpp:56`；`D:0x3fc3` |
| harvest 槽扫描 | C = 1/4/16/64 次/轮 | COUNTED | `B:adapters/fio/src/events.cpp:92` |
| 每次 harvest 的 `take` | 构造上 ≤ C（受 `staged.size()<max` 截断） | COUNTED | `B:adapters/fio/src/events.cpp:92,100,114` |
| `getevents` | 1.000000 / 0.999999 / 0.999995 / 0.999938 / 0.999998 | COUNTED（`E2`） | `E2/logs/counts-summary.txt` |
| harvest/op | 2.0 / 1.25 / 1.0625 / 1.0156 / 1.25 | COUNTED（`E2`） | 同上 |
| adapter self（A2/D16） | 5.31% = 241.6 cyc/op；其中 `harvest` 1.88% = 85.6、`sluice_queue` 2.88% = 131.1、`sluice_getevents` 0.55% = 25.0 | SAMPLED | `E3` record-root flat（n=1） |

### C.2 关键边界：adapter 的“children”不是 adapter 成本

`harvest` 在调用树里 30.50% 是 **children** 份额，其中 28.47pp 是 `RequestScope::take`；adapter 自己的扫描 self 只有 **1.88%**（85.6 cyc/op）。同理 `sluice_queue` 21.34% 的 children 里 17.58pp 是 `AsyncIoContext::submit_read`、15.90pp 是 `UringAsyncBackend::submit_read`。因此：

- “adapter scan 是用户态主成本” **NO_GO**（C5）：harvest self 1.72–1.88%，主成本在 `take`/`consume`/mutex 快路径。
- adapter 的槽扫描是**重复 take 的上游触发器**，不是主要直接成本 → C2 并入 C1（§I）。

### C.3 与深度有关但与 adapter 无关的两项

- 提交侧每 op 恰好 1 个 SQE、1 次 enter（native 与 sluice 均 1.0000 enter/op；trace 是 **NON_EXACT**、只作下界；主依据是 fio 默认 `min=max=1` + 每 SQE 一次 submit 的结构），**没有**单独可测的 enter 成本 → C9 **NOT_ADMITTED**（#595 KEEP_IMMEDIATE 的收益前提不成立）。
- research 计数构建的扰动（**COUNTED**，`E2/logs/perturbation-summary.txt`）：instr/op 增量 +124.36/+167.31/+490.19/+3,366.62/+167.94（+1.93%/+2.77%/+6.14%/+13.33%/+2.80%），IOPS −4.63%/−6.27%/−20.59%/−56.81%/−1.82%。这是**测量开销**，不是产品成本 → C10 **NO_GO（产品候选）**。高深度计数（D64 的 33.5 take/op）来自被扰动构建：计数比率 COUNTED，但对 release 动力学的代表性 `NOT_IDENTIFIED`。

详见 `ADAPTER_SCAN_COST.md`。

---

## D. RequestScope Cost（查找与重复 take）

### D.1 计数（**COUNTED**，`E2` research；两 rep 一致到 ≤1e−6）

| cell | 容量 C | take/op | pending/op | pending/take | resolve iters/op | resolve iters/take | 扫描均值 slots/harvest |
|---|---|---|---|---|---|---|---|
| A1/D1 | 1 | 2.000000 | 1.000000 | 0.500000 | 2.000000 | 1.000000 | 1.0 |
| A4/D4 | 4 | 3.499999 | 2.499999 | 0.714286 | 7.499997 | 2.142857 | 2.8 |
| A2/D16 | 16 | 9.499953 | 8.499953 | 0.894736 | 59.499681 | 6.263156 | 8.9412 |
| A5/D64 | 64 | 33.497575 | 32.497575 | 0.970147 | 747.443814 | 22.313371 | 32.9842 |
| A3/64k | 4 | 3.499995 | 2.499995 | 0.714285 | 7.499991 | 2.142858 | 2.8 |

`scan_hist` 的形状是 **1..C−1 近似均匀 + C 桶约为单桶 2 倍**（`E2/runs/*/counters.json`；weighted-sum 不变量通过），均值落在 `C(C+3)/(2(C+1))`：C=1→1.0、4→2.8、16→8.941、64→32.984。这是**本轮 5 cell × 2 rep 的描述性观测恒等式**，不是通用定律。

### D.2 pending 的重复付费（结构性，**COUNTED** 调用 + 源码）

`harvest` 对每个非空槽调 `take`（`B:adapters/fio/src/events.cpp:100,114`）；`take_` 先 `resolve_` 线性扫描（`S:include/sluice/async/request_scope.hpp:114-129,131-141`），再 `Request::take_result()` → `RequestBridge::consume` → `RequestCore::consume_public_result`（`S:include/sluice/async/request.hpp:146-159`；`S:src/async/detail/request_core.cpp:323-344`，锁在 `:325`）。**pending 判定发生在 core 锁之后**（`request_core.cpp:326-331`）：pending 的 ticket 照样付一次 `resolve_` 扫描 + 一次 core 锁。D16 时 pending 占 take 的 **89.47%**（8.499953/9.499953），即每 op 有 **8.5 次**“白跑”的 take/consume。

### D.3 单次 take 的成本不随扫描长度上升（**SAMPLED**，n=1/root）

| 根 | take self | cycles/op | take/op | cyc/take | resolve iters/take | harvest slots/harvest |
|---|---|---|---|---|---|---|
| `E5` A1 record rep1 | 2.44% | 4,787.93 | 2 | **58.41** | 1.00 | 1.0 |
| `E4` A2 record-root rep1 | 10.45% | 4,859.52 | 9.5 | **53.45** | 6.26 | ≈8.94 |
| `E3` A2 record-root rep1 | 11.68% | 4,550.59 | 9.5 | **55.95** | 6.26 | 8.94 |

`resolve_` 的平均迭代数/take 从 1.00 增至 6.26（约 6.3×）；8.94 是**上一层 adapter 的 harvest slots/harvest**（`E2` §D.1），不是 `take` 的扫描长度，不能用来解释单次 take。单次 take 成本 58.41→53.45/−8.5%（或 55.95/−4.2%），**没有单调上升**。裁决材料给出的 57.45 cyc/take（`12.15% × 4,492.11 / 9.499953`）对应的 12.15% 份额来自 r2（`COST-LEDGER.md:175`），本报告在归档 flat 中**未能复现**该三元组；无论用 53.45 还是 55.95，结论方向不变且更强。

**因此**：
- C3“resolve_ 线性扫描 → O(1) 槽索引” **NOT_ADMITTED**：单次 take 成本未随 resolve 迭代数上升（1.00→6.26 iters/take；跨 root、n=1，非单因素），且 D4 全链差（3,260）< D1（3,673）与“resolve 迭代越多增量越大”**不一致**（旁证，不单独承担证伪）。
- C4“扫描长度是 D16 增量主因” **NO_GO**（现有证据不支持，**不是**单因素证伪）：增量主要随 **take 调用次数 2→9.5** 增长；三个 record root 来自不同会话，不能给 cyc/take 的 CI。
- C1“pending 重复 take/consume” **EVIDENCE_READY**（只申请 G1/G2 单因素 A/B）：counted 调用负担 + 可证伪的预测（§I）。

**H5 边界（必须随本节引用）**：采样份额是**各 run 内的占比**，不是 per-op 函数计数，也不是净因果成本；H5 的当前判词是 `HOTSPOT_OBSERVED / CAUSAL_COST_NOT_ISOLATED`（`docs/fio-integration/COST-LEDGER.md:414`），其“函数级热点”只指 core `take`，不指容量扫描。本轮新增的“单次 take 不随扫描长度上升”只否定扫描长度假设，**不构成**净成本分解。

### D.4 该层的正确性约束（为什么不能“顺手删掉 pending take”）

`take_result()` 只有在 `consume` 返回 `ready` 时才清空 `core_/backend_/id_` 并释放句柄（`S:include/sluice/async/request.hpp:153-158`）；返回 `pending` 时句柄保持绑定。`consume_public_result` 在同一 core 锁内完成 `resolve_public_` → `published` 判定 → outcome/metadata 拷贝 → `binding_live=false` → `try_reclaim_`（`S:src/async/detail/request_core.cpp:325-341`）。任何“跳过 pending take”的改法必须保持：① 每次 ready 恰好一次消费；② `published && !binding_live && refs==0 && control==0 && !inflight && observer retired` 的回收谓词不被提前满足（`:580-590`）；③ 陈旧 ticket 仍返回 `empty` 而不是误判 ready。这三条是 G2 语义等价证明的必答项。

详见 `REQUEST_SCOPE_LOOKUP_COST.md`。

---

## E. RequestBridge Cost（consume）

| 项 | 值 | 类 | 证据 |
|---|---|---|---|
| 每 op `consume` 调用 | T = 2 / 3.5 / 9.5 / 33.5（D1/D4/D16/D64） | COUNTED | `E2`；`bridge_consume_calls == take_calls == scope_take_calls` 不变量全过 |
| 每次 consume 的 core 锁 | 1 对 | COUNTED（调用）+ 源码 | `S:src/async/detail/request_core.cpp:325` |
| consume self（A2/D16） | 3.06% = 139.3 cyc/op | SAMPLED（`E3`，n=1） | flat |
| consume 子树内的 lock/unlock | 12.71pp（7.29+5.42）= 578.4 cyc/op | SAMPLED | `perf-report.txt` children；是 mutex 31.56% 的子集，**不得**再叠加 |
| core 查表复杂度 | O(1)（按 slot 索引 + context/generation 校验） | COUNTED（静态） | `S:src/async/detail/request_core.cpp:121-160` |
| publication 后回收 | `try_reclaim_`→`release_slot_`（清 borrow/outcome/metadata、generation+1、回 free list） | COUNTED（静态） | `S:src/async/detail/request_core.cpp:580-631` |

语义（`SEMANTICALLY_NECESSARY_UNDER_CURRENT_DESIGN`）：`consume_public_result` 是 **publication 可见性 + 唯一消费 + 绑定释放 + 回收** 的同一线性化点。删除锁的可能后果（源码审计 §5.5 删除影响矩阵）：

- double consume：两个线程同时看到 `published` 并各自拷贝 outcome、各自 `binding_live=false`；
- duplicate free：两边都满足 `reclaimable_`，`free_slots_` 同一 index 入队两次；
- 与 `offer_terminal` 竞争时 outcome 撕裂（写者与读者无 happens-before）。

因此 C6“删除/替换 core mutex、lock-free consume” **NO_GO**：30.34%/31.56% 是**无竞争快路径的 cycles 归属**，不是等待时间（§F.4）。

详见 `REQUEST_BRIDGE_CONSUME_COST.md`。

---

## F. Synchronization Cost（同步成本）

### F.1 五把锁的职责（**COUNTED** 静态；获取点计数来自锁图审计）

| 锁 | 产品获取点 | 保护的状态 | 必需性 | 删除后果 |
|---|---|---|---|---|
| `RequestCore::mutex_` | 28 | 全部 slot 字段、generation、refs、published/binding/observer、free list | publication / terminal visibility / reclamation / unique consumption 四者都必需 | double accept / double terminal / double consume / duplicate free |
| `UringAsyncBackend::dispatch_mtx_` | 14 | `dispatch_`、`publication_pending_`、`delivery_`、`router_`、`ledger`、SQ | 后端发布交接、router/SQ、cancel-vs-dispatch | 丢/重排 publication；错配 CQE；execution_ref 永不释放 |
| `ThreadPoolBackend::work_mtx_`（+`work_cv_`） | 15 | `dispatch_`、`publication_pending_`、`active_workers_`、cv 谓词 | submit→worker 与 worker→owner 交接、prepared op happens-before | worker 用错误 fd/buffer；丢唤醒；错误 quiescence |
| `AsyncIoContext::access_mtx_` | 34 | `backend_`/`core_` 生命周期、`drive_active_`、`execution_closed_` | 接受事务串行化、单一 drive 许可、dtor 与 API 互斥 | 双 poll、move/destroy 竞态、接受事务失去线性化 |
| `ProgressSource::mtx_` | 13 | epoch、control、`notification_fd_`、`notification_pending_` | no-lost-wake / 控制粘性 | 丢唤醒、控制代际竞态 → 活性错误 |

**本表五把锁在当前设计下均标 `SEMANTICALLY_NECESSARY_UNDER_CURRENT_DESIGN`**：这只说明“删除会破坏正确性/活性”，不说明“每次获取的成本最小”。**“产品获取点”是静态调用点计数（28 个 `lock_guard` 位置），不是每 op 执行次数。** 稳态每 accepted I/O 只用其中 **9 个固定点** + **1 个 consume 点**（计 T）+ **2 个 pass 点**（计 2P）；其余 **16 个**是冷路径/错误路径/observer API：`occupancy`、`collect_outstanding`、`rollback`、`close_admission`、`admission_open`、`note_health_failure`、`lookup`、`cancel`、`discard_public_result`、`observe_public_result`、`acquire_execution`（全仓无调用者）、`acquire_control`、`release_control`、`register_observer`（adapter 不用）、`cancel_observer`、`observer_retirement_acquired`。把 28 当成每 op 固定锁数会把固定部分高估约 **3 倍**（28 vs 9）。

`RequestScope` **无锁**，其单线程安全性来自 adapter 的单 job 线程不变式（`FIO_NO_OFFLOAD` + 拒绝 `verify_async`，`B:adapters/fio/src/engine.cpp:71-73,187`），不是类自身保证（源码审计 F8）。

### F.2 core 锁计数闭合：`9 + T + 2P`（**COUNTED** 计数 + **INFERRED** P）

每 progress pass 有 2 次 core 锁：
1. `UringAsyncBackend::poll_progress` 末尾 `pass.accepted_work_remains = outstanding() != 0;`（`S:src/async/uring_backend.cpp:326`）→ `outstanding()`（`:706-708`）→ `core_->outstanding_summary()`（`S:src/async/detail/request_core.cpp:93-96`，锁在 `:94`）。
2. `AsyncIoContext::run_progress_pass_` 的 `core_->health_failed()`（`S:src/async/async_io_context.cpp:433`）→ `S:src/async/detail/request_core.cpp:246-249`（锁在 `:247`）。
   （`close_admission_on_progress_exhaustion_` 只在 `progress_->exhausted()` 为真时才取 core 锁，`S:src/async/async_io_context.cpp:449-453`，稳态不计。）

取 `P = harvest/op − getevents/op`（**INFERRED** 恒等式：一次 getevents 至少 1 轮 harvest，多出的轮次对应一次 poll pass）：

| cell | harvest/op | getevents/op | P | `9+T+2P` | 实测 core lock/op | 残差 |
|---|---|---|---|---|---|---|
| A1/D1 | 2.000000 | 1.000000 | 1.000000 | 13.000000 | 13.000000 | 0 |
| A4/D4 | 1.250000 | 0.999999 | 0.250001 | 13.000001 | 12.999999 | +2e−6 |
| A2/D16 | 1.062495 | 0.999995 | 0.062500 | 18.624953 | 18.624953 | −3.6e−15 |
| A5/D64 | 1.015564 | 0.999938 | 0.015626 | 42.528827 | 42.528826 | +1e−6 |
| A3/64k | 1.249998 | 0.999998 | 0.250000 | 12.999995 | 12.999995 | 0 |

结论：**原 U3.2 公式漏了 2P**。A1 的 +2.0 残差正是 C=1 时 `2P=2`；这不是测量异常。`9+T` 只是“生命周期锁”口径，不是总 core 锁。U3.3 的 6.50/3.71/1.96/1.27 是**量纲错误**（总量/总量）；正确写法是 `(core lock/op)/(9+take/op)`，得 1.1818/1.04/1.0068/1.0007/1.04；若分母改用 `9+T+2P`，五 cell 全部为 **1.000**。字段应修，但它没有进入判词。

### F.3 成本分类（**不得**把 flat 份额当等待）

| 成本类别 | 本轮证据 | 类 | 结论 |
|---|---|---|---|
| uncontended lock/unlock 固定成本 | 1,436.2 cyc/op ÷ 48.256 次 libc 调用 = **28.6–29.8 cyc/调用**；top-2 1,380.6 ÷ 48.256 = 28.61。等价地，24.128 对/op → **57.2–59.5 cyc/对**（下文按“对”计价的项一律用此 pair 价） | SAMPLED 派生 | 主要成分。是 libc 快路径（含 `lock cmpxchg`）与调用/PLT，不是等待 |
| contended waiting | 见下表（futex 调用/等待/总时长分开） | COUNTED（bpf，n=6） | **fio 单 job 线程下未观察到稳态竞争**；job on-CPU 96–98% |
| cacheline bouncing | `xsnp_hitm` 0.0098/op、RFO HITM 0.0074/op、`machine_clears.memory_ordering` 0.2147/op（`E3` pmu-hitm/memorder，n=1） | COUNTED（PMU，100% running） | 未见实质 bouncing |
| atomic | `lock_loads 51.262/op` = 48.256（24.128 对 × 多线程标定 2.0003 locked ops/pair）mutex 快路径 + **3.005 core atomics/op**（2 提交 + 1 完成，与源码审计精确到 0.005） | COUNTED（PMU 分解为 INFERRED） | core atomics 很少；主体是 mutex 快路径 |
| critical-section work | `dispatch_one_locked` 3.30%、publication 1.39%、`consume` self 3.06%、`take` 11.68%（`E3`） | SAMPLED | 临界区内工作可测；小于锁原语总成本 |
| cross-thread handoff | uring 臂：submit 与 progress 同线程（fio job），**无用户态 handoff**；TP：`work_mtx_`+`work_cv_`、每完成 1 次 eventfd write + futex wake + 线程切换 | COUNTED（静态）/NOT_IDENTIFIED（切换成本） | uring 的 mutex 基本无竞争；TP 的真实竞争与切换未逐项计时 |

**futex：调用、等待事件、等待时长是三件事**（`E3` bpf-locks/bpf-sched，n=6；窗口 5 s，约 37.9k–38.5k op）：

| 量 | 观测 | 折算 |
|---|---|---|
| futex 调用 | 40–48 次/5s | 0.00105–0.00127/op |
| futex 等待事件 | 15–18 次/5s | 0.00040–0.00048/op |
| futex 等待总时长 | 103.3–104.9 ms/5s | ≈2.1% 墙钟；摊到全部 op ≈2.7–2.8 µs/op（**不代表** per-op 阻塞） |
| 单次最长等待 | ≈100.25–100.29 ms，1 次/5s | main 等 job 的一次 park，不是 per-op 竞争 |
| off-CPU 总计 | 100.8–101.0 ms/5s | 与 futex 等待同量级；job on-CPU 96–98% |

**scope**：以上全部是 **fio 单 job 线程**；多 submitter 的 product API 未测（`REQUEST_CORE_SYNCHRONIZATION_COST.md` §6），不得外推。

**硬边界**：record-root 的 31.56% 是**全部 libc lock 符号**，其中包含 fio 自己的 mutex。按锁对数分摊（18.625 对/op ÷ 24.128 对/op，分子分母同为 pair 单位）的**估计**是 `1,436.2 × 18.625/24.128 = 1,108.6 cyc/op`（24.4%），其余 ≈327.6 cyc/op（7.2%）属 fio/adapter。**这不是严格上界**：若 core 每次调用比全部 lock 的平均更贵，core 份额可以超过 1,108.6；可用的界只有 0–1,436.2（或把全部 libc mutex 计入 Sluice 的 3.1k）。该分摊还假设“每次调用成本相同”（**未测**），且 24.128 对/op 来自 release+sym+BPF、18.625 对来自 research → 跨构建拼接，**INFERRED**。

### F.4 为什么“29% 不是 29% 锁等待”

1. 样本落在 `pthread_mutex_lock`/`unlock` 符号上，只说明 CPU 在这些指令上退休了周期；无竞争时这也照样发生（每次 28.6–29.8 cyc）。
2. 竞争的直接证据（futex 次数、futex 等待时长、off-CPU、runqueue）全部指向**没有稳态竞争**：futex 0.001/op、on-CPU 96–98%、HITM 0.0098/op。
3. uprobe 测得的“锁持有 1,768 ns”与无竞争 mutbench 1,766.7/1,797.1 ns 在 2% 内，`lock_gt_5us 0.22%` vs 0.036%/0.065% → 测到的是 **uprobe/uretprobe 开销**，不是锁成本。
4. 因此 30.34%/31.56% 只能写成“libc 快路径 + 调用开销的 cycles 归属（上限）”，**不能**写成“锁等待 29%”，也不能据此提 lock-free 重写。
5. **scope**：结论只对 **fio 单 job 线程**成立；多线程 product API（多 submitter、并发 cancel/observer）的竞争形态**未测**。

### F.5 必要 ≠ 最小

`9+T+2P` 已被计数完全解释，且每条锁都有明确的正确性职责（§F.1）。**没有**“可删除的冗余锁”证据：C7“合并固定 9 次 core 锁”因此 **NOT_ADMITTED**——9 次是当前语义边界（reserve/accept/claim + offer/release/begin/complete/claim/retire observer），合并属于重新设计问题。C6 删除 core mutex **NO_GO**。要谈“每次获取能否更便宜”，必须先重新设计协议 + 语义等价证明 + 单因素 A/B（§G）。

详见 `REQUEST_CORE_SYNCHRONIZATION_COST.md`。

---

## G. Controlled Experiments（做了什么、没做什么）

### G.1 已执行（全部有回执与哈希清单）

| 会话 | 设计 | 规模 | 关键产物 |
|---|---|---|---|
| `E1` baseline | ABCCBA（io→sluice→TP→TP→sluice→io），clean0 + pmu-root g1/g2/g3 | 120 measured + 6 prep | fio JSON、perf-stat、denominator、env 快照 |
| `E2` counters | release/research ABBA（r1→x1→x2→r2）+ 每 cell 1 次 trace-bpf | 20 measured + 5 trace + 10 prep | `counters.json`、`logs/counts-summary.txt`、`perturbation-summary.txt`、10/10 invariants |
| `E3` sync/consumer | A2 record-root（dwarf callchain）+ BPF + PMU + consumer W02 uring/TP（n=2） | **24 个计入 = record_root 1 + bpf 6 + pmu 5 + consumer 5 + calibration 5 + prep 2**；另 2 个 discarded（stale-sym、schedcheck） | `perf-report-flat.txt`、`analysis/summary.json`、`SUMMARY.md`；逐项见 `E3/analysis/summary.json` 的 `run_counts`（bpf 6 / calibration 5 / consumer 5 / discarded 2 / pmu 5 / prep 2 / record_root 1） |

### G.2 未执行（本轮结论因此不能闭合）

| 未做 | 影响 | 类 |
|---|---|---|
| #579 要求的 adapter-only vs core 单因素 A/B | C1 的收益未证明 | NOT_RUN |
| Arm D（H3 scan vs free-list） | 反事实是合成模型，**非**生产路径语义等价，不能与 release 相减（`E3/analysis/summary.json` 的 `verdicts.arm_d`） | NOT_RUN |
| native 同 build 的 record 分区 | D16 全链 +5,219 instr/+3,164 cycles 的因果拆分做不到 | NOT_IDENTIFIED |
| D64 的 record-root / BPF | D64 的 cycles 余量无法归因 | NOT_IDENTIFIED |
| 对 `b1e28`+`ca4a6c` 重跑 M-SEM | 语义门沿用 `instrumentation-smoke-001`（release `3ba984`+research `ece910`，`direct=1,size=16M`） | NOT_IDENTIFIED |
| 内核 io-wq / IRQ / softirq | `cycles:k` 只覆盖 fio 线程上下文；io-wq CQE 1/run 不可归属 | NOT_IDENTIFIED |
| consumer 的 `max_outstanding` | stats off，未记录；achieved depth 只有 fio 桶下界 | NOT_IDENTIFIED |

### G.3 三个反事实假设的可证伪形态（**均未运行**）

| 假设 | 预测（若成立） | 单因素要求 | 当前状态 |
|---|---|---|---|
| H-A 消除 pending 重复 take/consume | take/op 从 ≈C/2+1.5 降到 ≈1/完成；D16 core 锁 18.625→≈10.1；D64 42.53→≈10.0；take+consume self 与 pending 对应的 core 锁按比例下降 | 保持 fio 完成观察/progress 语义；只改 scope ready 索引/单次消费 | 只有 counted 调用数 + SAMPLED 份额；**未跑** → C1 EVIDENCE_READY（仅申请 A/B） |
| H-B ready-index/事件驱动 harvest | O(C) 扫描与重复 take 同时下降 | 与 H-A 同一实验 | harvest self 仅 1.88%，价值主要在 H-A → 并入 C1 |
| H-D TP handoff 削减 | cycles/CPU 向 uring 靠拢而 instr 不变 | fio 级单因素（worker 数/唤醒/batching） | 机制未识别（D16 TP instr −1.1% 但 cycles +96%、CPU +11.9 µs）→ C8 RESEARCH_ONLY |

---

## H. Statistical Validity（统计有效性）

1. **n=2 只报观测极差，不是 CI**。`E1` clean0 IOPS 极差最大 12.55%（A4 TP）、10.41%（A4 io_uring）、7.83%（A2 TP）、6.91%（A1 sluice）；PMU g1 最大 18.41%（A1 TP）、5.37%（A2 sluice）；instr/op 最大 2.64%（A1 TP）；cycles/op 最大 8.76%（A1 sluice）、7.30%（A3 io_uring）；CPU/op 最大 20.17%（A1 TP）。任何方向的区间估计为 INCONCLUSIVE。
2. **`E2` A/A**：release IOPS 最大 2.196%（A4）、research 0.761%（A5）；instr/op 最大 0.170%（A3 research）。S8 的 10% 门通过，U5 可判读。
3. **`E3` fio pmu-atomic（n=2）**：IOPS 0.987%、instr 0.027%、cycles 2.646%、CPU 1.002%（本报告复算：`7,988.28/7,990.40` instr、`4,515.49/4,636.59` cycles、`2.6896/2.7167` µs、`51.262` lock_loads/op，与 `SUMMARY.md` 一致）。
4. **`E3` consumer**：uring/TP 的 A/A IOPS 0.771%/2.484%，instr 0.002%/0.007%，cycles 2.771%/4.289%；7 个 measured phase 的 95% CI（t6=2.447，描述性、phase 非独立）：uring `[421158.8,428478.8]/[425048.3,431163.9]`，TP `[328351.9,338230.2]/[306914.3,343313.5]`。TP rep2 首 phase 284,947.3 离群，within-run 16.46%>15% → 只作定性。
5. **A2 pmu 跨 abort/resume（本报告复算原始值）**：`A2__sluice-uring__pmu-root-g1` rep1 IOPS 361,971.4 → rep2 381,924.8，CPU 2.818 → 2.673 µs；`ABORT` 时间 04:39:52，rep1 04:38、rep2 04:43。instr 计数不受影响，但 **A2 pmu 的 IOPS/CPU 比较需限定**。
6. **跨构建**：`E1` release `.so` `3ba98405…`（`.text` `36a56c45…`）已被重建覆盖（当前 `b1e28d69…`/`cfe811b7…`）；`find build -name '*.so*'` 找不到旧文件。S2 的 instr/op 与 S1 五 cell 在 0.07% 内复现，**指令深度表**有旁证；cycles/组件/TP 的同一性未建立。
7. **record-root n=1**：组件份额只有采样二项 CI（N=10,195，例如 take ±0.62% abs、mutex ±0.90% abs），**不是** run-to-run CI；D64 无 record/BPF。
8. **U3.3 字段错误**：manifest 的 6.50/3.71/1.96/1.27 是总/总；正确 per-op 为 1.1818/1.04/1.0068/1.0007/1.04（对 9+T），含 2P 后为 1.000。该字段未进入判词。
9. **achieved depth 是 INFERRED**（fio 桶下界加权，`>=64` 记 64），不是逐 IO 深度；consumer 的实际 `max_outstanding` 未记录。
10. **consumer 与 fio 不同构**：64 MiB tmpfs 顺序 positional read、无 fio driver、每完成 `wait_for`、lifecycle 含 3 phase（`540,000 = 9 轮 × 3 phase × 20,000`，`S:bench/bench_w02_pipeline.cpp:137,526,581,589,735`）；不能把 TP−uring 的 +156.5%/+175.4% cycles 映射到 fio A2。
11. **bpftrace 扰动**：A2 被压到约 7.6k–7.7k IOPS（约 50× 放慢）；`bpftrace_returncode=124` 是 timeout 窗口到期；trace 计数标 `NO_LOSS_RECEIPT / NON_EXACT`，只作下界。
12. **协议连续性**：`E1` 中途把 loadavg 超限从 “aborts immediately” 改为 pause+keep（`tools/attribution/baseline_session.py:533-561`），3 个 run 在 2.56/2.53/2.65 下保留并写 `env-pause.json`；`runs-manifest.json` 只记 ok。这不影响 instr 计数，但影响“会话严格性”的叙述。
13. **D64 余量的算法（INFERRED，不是测量）**：D64−D16 cycles = 10,244.86 − 4,617.13 = **+5,627.73**；可解释部分 = 额外 take（33.4976 − 9.5000 = 23.9976 次 × 55.95 cyc/take = **1,342.7**）+ 额外 core 锁（42.5288 − 18.6250 = 23.9039 对 × **57.22–59.52 cyc/对** = **1,368.0–1,422.8**）= **2,710.7–2,765.5**；残差 = **2,862.2–2,917.0 cyc/op（≈2.86–2.92k）**。假设：单次 take 成本在 D16/D64 相同、core 锁按 libc pair 价、只计 take/lock 两项。D64 无 record-root，故这是模型残差，不是测量。**旧稿把 23.9039 对乘以 28.61–29.76 cyc/调用，少算一倍；已按 pair 价订正。**

---

## I. Removable Work Ledger（摘要）

完整表（含证据、预测、阻断项、Owner 授权项）见 `REMOVABLE-WORK-LEDGER.md`。状态标签是本轮唯一允许的准入语言。

| ID | 候选 | 状态 | 一句话理由 |
|---|---|---|---|
| C1 | pending 重复 take/consume（ready-index/单次消费） | **EVIDENCE_READY**（只申请 G1/G2 A/B） | counted pending/op 1/2.5/8.5/32.5、take/op 2/3.5/9.5/33.5；每次 pending take 一次 core 锁；D16 take+consume self 670.8 + pending 锁 486–506 = 模型 1,087–1,106 cyc/op（23.9–24.3%，INFERRED；非可省净收益）；反事实未跑 |
| C2 | O(depth) harvest 槽扫描 | **RESEARCH_ONLY**（并入 C1） | harvest self 1.72–1.88%；是 C1 的上游触发器，不单列 |
| C3 | `resolve_` → O(1) 槽索引 | **NOT_ADMITTED** | 单次 take 58.41→53.45/−8.5%（或 55.95/−4.2%），resolve 迭代 1.00→6.26 iters/take（8.94 是 adapter 的 harvest slots/harvest，不是 take 扫描）；跨 root/跨会话（E5/E4/E3，n=1）、非单因素；只否定“显著收益”，不证明无收益 |
| C4 | “扫描长度是 D16 增量主因” | **NO_GO** | 现有证据不支持（**非单因素证伪**）；增量主要随 take 调用次数 2→9.5 增长；三个 record root 各 n=1、跨会话，不能作因果证伪 |
| C5 | “harvest 扫描是用户态主成本” | **NO_GO** | harvest self 1.88%；take/consume/mutex 才是大头 |
| C6 | 删除/替换 core mutex、lock-free consume | **NO_GO** | publication/terminal visibility/reclamation/unique consumption 的必需串行化 |
| C7 | 合并固定 9 次 core 锁 | **NOT_ADMITTED** | 9 次是当前语义边界；计数已被 9+T+2P 完全解释 |
| C8 | ThreadPool 跨线程 handoff/调度 | **RESEARCH_ONLY** | instr −1.1% 但 cycles +96%；机制未识别 |
| C9 | io_uring 提交批处理 | **NOT_ADMITTED** | native/sluice 均 1.0000 enter/op（trace **NON_EXACT**，只作下界；主依据是 fio 默认 `min=max=1` 的静态配置 + 每 SQE 一次 submit）；无单独 enter 成本；#595 KEEP_IMMEDIATE |
| C10 | research 构建计数器开销 | **NO_GO（产品候选）** | +124–+3,367 instr/op、IOPS −4.6%…−56.8%，是测量扰动 |
| C11 | consumer bench 作为 fio adapter 成本代理 | **NOT_ADMITTED** | 64 MiB tmpfs vs 1 GiB btrfs、无 fio driver、3-phase、每完成 wait_for；非同构 |
| C12 | Kernel/io-wq/IRQ 成本 | **RESEARCH_ONLY** | 未归属；`cycles:k` 仅 fio 线程上下文；io-wq CQE 1/run 不可归属 |
| C13 | `find_live_router_cookie_` O(C) 倒扫 | **NOT_ADMITTED** | 每 CQE 一次；flat self 1.35% = 61.4 cyc/op；无扫描长度计数、无收益证据；与 C3 同类 |

**C13 说明（不是漏掉的候选）**：`find_live_router_cookie_`（`S:src/async/uring_transport.cpp:551-557`）是完成路径上第二处 O(C) 扫描——每个 CQE 从 router 尾部倒扫，router 大小受 capacity 约束。它在 B.3 的 other Sluice 中占 1.35%（61.4 cyc/op），但**没有**像 scope 那样的 counted 扫描长度分布（未插桩），也没有单因素实验；按与 C3 相同的逻辑（无收益证据）列为 C13 **NOT_ADMITTED**。

**唯一值得进入下一阶段研究的**：C1（含 C2），且必须作为**单因素 A/B**、保持 fio 完成观察/progress 语义，并先通过 #604 G0。

---

## J. Independent Review（独立复核）

### J.1 对抗性证伪的结论（10 项）

> 本表的 **REFUTED** 一律按 §0.1.3 的语义读作“**现有证据不支持**（weak refutation）”，不是单因素证伪；唯一接近单因素的是 C6 的源码级删除矩阵。

| 主张 | 判定 | 依据（本轮可见证据） |
|---|---|---|
| D16 增量由 `resolve_` 线性扫描造成 | **REFUTED（弱）** | 现有证据不支持：单次 take 成本不随 resolve 迭代上升（§D.3）；D4 全链差 < D1；三个 record root 各 n=1、跨会话，不能作单因素因果证伪 |
| adapter harvest/slot 扫描是用户态主成本 | **REFUTED** | harvest self 1.88%（`E3`），children 30.50% 里 28.47pp 是 core take |
| take 成本主要来自 ticket lookup | **REFUTED** | 同上；take self 11.68% < mutex 30.34% |
| consume 中存在可直接删除的同步 | **REFUTED** | `request_core.cpp:325` 的同一临界区完成 published 判定/拷贝/绑定释放/回收 |
| mutex flat 份额代表 lock contention | **REFUTED** | futex 0.001/op、on-CPU 96–98%、HITM 0.0098/op、uprobe 时长 = 探针开销 |
| enter/op 相同 ⇒ kernel work 完全相同 | **REFUTED** | punt/worker CQE、enter flags、`cyc:k +8.7%`（g2 均值）均不同 |
| A1/A2/W-C2 的 per-op 可无条件比较 | **REFUTED** | 窗口/模式不同；`MODE_OR_TIMING_EFFECT_NOT_IDENTIFIED` |
| 诊断构建与 release 成本可直接相减 | **REFUTED** | sym 与 release `.text` 相同但 record 扰动 −13.6%…+1.1%；research `.text` 不同 |
| 单因素实验未改变完成观察/progress 语义 | **NOT_TESTED** | #579 A/B 未做；Arm D 未跑；consumer 臂非同构 |
| 任何热点减少必提高 IOPS | **REFUTED** | A2 TP instr −1.5% 但 IOPS −43%（r2 口径）；A3 TP instr +14.7% 但 IOPS +82%（r2）；consumer TP +1.65% instr 但 IOPS −21.6%（`E3`） |

### J.2 证据审计发现的问题（本轮采纳）

| 级别 | 问题 | 本报告处理 |
|---|---|---|
| 高 | `E1` 的 uring release 二进制（`3ba984…`/`36a56c45…`）已被重建覆盖，无法复核 | §B.6 给出依赖映射、§H.6 给出旁证：指令表有 0.07% 旁证，cycles/组件/TP 跨 build 未建立 |
| 高 | 会话中途把 loadavg 超限从 abort 改为 pause+keep；A2 pmu 跨 abort/resume | §H.5 给出原始 rep1/rep2 值并限定比较 |
| 中 | M-SEM 未对 `b1e28`+`ca4a6c` 重跑（`session-002/manifest.json:2844-2846` 写 `run:false`） | 标 NOT_IDENTIFIED；不作为语义门通过 |
| 中 | A1 core lock/op 13.0 与静态 `9+T=11` 不符 | **本轮修正为 9+T+2P**，五 cell 闭合（§F.2） |
| 中 | U3.3 量纲错误 | §H.8 给出正确口径与数值 |
| 低 | `E3` runs-manifest 不完整、SUMMARY 两处数字混 rep、trace 无丢失回执、pass1 的 INVALIDATED 未入哈希、`E1` 无摘要 | 只引用可复核的 raw/汇总；trace 计数标 NON_EXACT |

### J.3 本报告对裁决材料的订正

1. **`9+T` → `9+T+2P`**：每 progress pass 另有两次 core 锁（`outstanding_summary` + `health_failed`），五 cell 与实测计数闭合到 ≤2e−6。这是**解释性修正**，不改变“锁数随深度上升”的方向，也不改变“无可删除冗余锁”的结论。
2. **两处数字未能复现，按 raw 处理**：
   - `E3` 的 `57.45 cyc/take`（来自 r2 的 12.15% 份额）在归档 flat 中找不到；本报告用 `53.45`（`E4`，10.45%）与 `55.95`（`E3`，11.68%）替代，结论不变。
   - 裁决材料的 “pending 分摊 core 锁约 506 cyc/op” 可用 `8.5 × (1,436.16/24.128) = 505.9` 复算，单位正确；本报告早期把它写成 243–253，是把 8.5 **对**乘以了 28.61–29.76 cyc/**调用**，少算一倍。订正后的可复算口径：pending take+consume self `670.8 × 0.894736 = 600.2` cyc/op，pending 对应的 core 锁按 libc **pair 价** `8.499953 × 57.22…59.52 = 486.4…505.9` cyc/op，合计 **1,086.5–1,106.1 cyc/op（约 1,087–1,106；23.9–24.3%）**。**该模型值仍叠加两个未验证假设**：pending 与 ready 的平均 self 相同、pending core 锁与全部 libc 锁的平均价相同；新方案还要支付就绪登记/单次消费成本。因此 1,087–1,106 是**模型估计，不是可省净收益，也不是真值区间**；旧稿的 `[243, 853]` 与 `923.8 上限` 已撤回，净节省必须由 A/B 测出。仍落在裁决给出的 0.6–1.1k（13–24%）区间内，且明确标 **INFERRED**：per-call 成本不均匀，core/fio 未拆。

---

## K. Owner Decision（需要 Owner 决定的事）

### K.1 判词

**`USERSPACE_COST_PARTIALLY_ATTRIBUTED`**。

判词按 §0.1.1 的判据（逐项排除）：

- **不是 `USERSPACE_COST_ATTRIBUTED`**：没有 D16/D64 增量的因果分解；没有 native 同 build 的 record 分区；没有内核/io-wq/IRQ；没有对 `b1e28+ca4a6c` 的 M-SEM；`E1` 的 uring 二进制不可复核；record n=1；mutex 的 core/fio 拆分未测。
- **不是 `USERSPACE_INCONCLUSIVE`**：总 per-op、深度计数、D16 flat 八组加总、锁语义与 `9+T+2P` 闭合都可复算且稳定；invariants 全过；ABCCBA/分母/哈希可复核。
- **不是 `MEASUREMENT_INVALID`**：manifest 全过（`E1` 936、`E2` 340、`E3` 179）、invariants 全过、跨 build instr 在 0.07% 内。
- **不是 `SEMANTIC_NO_GO`**：没有语义门失败；core mutex 删除只是候选级 NO_GO；pending ready-index 仍可在保持语义的前提下做 A/B；M-SEM 缺口是限制，不是失败。

### K.2 判词**不**授权的事

1. **不代表可以修改 Sluice**。本轮零产品结论；C1 只是研究候选，不是“已证明可删”。
2. **不解除 #604 的 G0**。fio synthetic 不是 real consumer / 代表性 trace（`docs/fio-integration/PHASE1-REPORT.md` §K）；进入 G0 需要单独 workload admission（真实消费者或可辩护 trace + 冻结 oracle/预算）。
3. **不构成 whole-system attribution**。io-wq/IRQ/softirq、K/R/P/O 的逐请求因果关联均未闭合，不得在 PR/文档中写成“全系统成本已归属”。
4. **竞争结论的 scope 是 fio 单 job 线程**。“无稳态竞争”只在该部署下测得；多 submitter 的 product API、并发 cancel/observer 未测（§F.3、`REQUEST_CORE_SYNCHRONIZATION_COST.md` §6），不得外推到产品 API。

### K.3 建议 Owner 批准的下一步（仅研究）

| 项 | 内容 | 前置 | 产出判据 |
|---|---|---|---|
| R1 | C1 单因素 A/B（ready-index/单次消费），保持 fio 完成观察与 progress 语义 | #604 G0 或 Owner 明确豁免；语义等价证明；同 build 对照 | **主判据（机制）**：take/op 与 core lock/op 达到预测降幅的 ≥90%（D16 take ≤1.85、core 锁 ≤11.0；D64 core 锁 ≤13.3）。**次判据（不伤害）**：D16/D64 clean0 IOPS 不低于同 build A/A 极差（S2 实测最大 2.196%）；CPU/op 不升。**决策规则**：若操作数达标而 IOPS 不变，C1 记为“机制成立、吞吐中性”，产品价值改看 CPU/op 与 headroom，不以 IOPS 作收益门槛。 |
| R2 | C2 并入 R1（不单独开实验） | 同 R1 | harvest 扫描与重复 take 同降；harvest self 变化可测 |
| R3 | native 同 build 的 record 分区（补 D16 因果拆分） | 有 `b1e28` 对应的 native 记录方案 | Sluice flat self 与全链 Δ 的可加性检验 |
| R4 | D64 record/BPF（解释约 2.86–2.92k cyc/op 余量，§H.13） | 采样预算 | 余量落到具体符号或内核侧 |
| R5 | 对当前二进制重跑 M-SEM | — | 语义门对 `b1e28+ca4a6c` 成立 |
| R6 | 内核/io-wq/IRQ 归因（C12） | 独立研究范围 | 至少给出 io-wq 工作量的可归属上界 |

不建议：C3/C7/C9/C11/C13（NOT_ADMITTED）、C4/C5/C6/C10（NO_GO）。C8 只在 fio 级单因素可行时另立研究。

### K.4 复现入口

- 复算：`python3 -I tools/attribution/recompute_userspace_report.py`（只读；exit 0 表示闭合与 flat 恒等式成立；输出 §B/§D/§F 的表）。`E1` 因 `3ba984…` 已被覆盖只能重解析、不能重测；`E2`/`E3` 的重跑需 sudo/perf，本轮未跑。逐 run 证据在 `E1`–`E5` 的 `runs/<id>/`。
- 支撑文档：`ADAPTER_SCAN_COST.md`、`REQUEST_SCOPE_LOOKUP_COST.md`、`REQUEST_BRIDGE_CONSUME_COST.md`、`REQUEST_CORE_SYNCHRONIZATION_COST.md`、`REMOVABLE-WORK-LEDGER.md`。
- 方法与口径：`EXPERIMENT-PROTOCOL.md`、`INSTRUMENTATION.md`、`USERSPACE-COST-SOURCE-AUDIT.md`、`docs/fio-integration/COST-LEDGER.md`、`docs/fio-integration/MEASUREMENT-WINDOW-CONTRACT.md`。

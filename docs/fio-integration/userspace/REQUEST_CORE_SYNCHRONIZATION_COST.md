# RequestCore 同步成本（锁图、计数闭合、成本分类）

- 对象：产品代码中的 5 把 `std::mutex` 与 1 个无锁 `RequestScope`（Sluice `5d94f233…`）。
- 判词归属：**`USERSPACE_COST_PARTIALLY_ATTRIBUTED`**。本文结论：这些锁在当前设计下 **`SEMANTICALLY_NECESSARY_UNDER_CURRENT_DESIGN`**；core 锁计数已被 `9 + T + 2P` 完全解释；flat mutex 份额**不是**锁等待。
- 标签：**COUNTED** / **SAMPLED** / **INFERRED** / **NOT_IDENTIFIED**。

## 1. 锁图（COUNTED 静态；获取点计数来自锁图审计）

| 锁 | 定义 | 产品获取点 | 持有范围（要点） | 锁序 |
|---|---|---:|---|---|
| `RequestCore::mutex_` | `S:include/sluice/async/detail/request_core.hpp:295` | 28 | 整个 public 方法体；O(1) 状态迁移或 O(capacity) 扫描；叶锁，不再取其它锁 | 最内层 |
| `UringAsyncBackend::dispatch_mtx_` | `S:include/sluice/async/detail/uring_backend.hpp:320` | 14 | `dispatch_`/`publication_pending_`/`delivery_`/`router_`/`ledger`/SQ；poll 的排空 + `io_uring_submit` 可整个在锁内 | 中层 |
| `ThreadPoolBackend::work_mtx_`（+`work_cv_`） | `S:include/sluice/async/detail/threadpool_backend.hpp:247-248` | 15 | `dispatch_`/`publication_pending_`/`active_workers_`/cv 谓词；worker 不跨 syscall 持锁 | 中层 |
| `AsyncIoContext::access_mtx_` | `S:include/sluice/async/async_io_context.hpp:336` | 34 | 整个 backend submit（含 uring 的 `io_uring_submit`）、cancel、drive 许可、dtor | 最外层 |
| `ProgressSource::mtx_` | `S:include/sluice/async/detail/progress_source.hpp:491` | 13 | epoch/control/eventfd（read/write 在锁内，非阻塞）；wait 不跨 poll 持锁 | 与 `access_mtx_`/`dispatch_mtx_` 并列 |

**“产品获取点”是静态调用点计数，不是每 op 执行次数。** 28 个 `lock_guard` 位置中，稳态每 accepted I/O 只用 **9 个固定点** + **1 个 consume 点**（计 T）+ **2 个 pass 点**（计 2P）；其余 **16 个**是冷路径/错误路径/observer API（`occupancy`、`collect_outstanding`、`rollback`、`close_admission`、`admission_open`、`note_health_failure`、`lookup`、`cancel`、`discard_public_result`、`observe_public_result`、`acquire_execution`、`acquire_control`、`release_control`、`register_observer`、`cancel_observer`、`observer_retirement_acquired`）。把 28 当成每 op 固定锁数会把固定部分高估约 3 倍（28 vs 9）。

观测到的锁序：`access_mtx_` → {`dispatch_mtx_`, `work_mtx_`} → `RequestCore::mutex_`；`dispatch_mtx_` → `ProgressSource::mtx_`；`access_mtx_` → `ProgressSource::mtx_`。`core` 与 `progress` 是叶锁，未发现反向边。仓库没有集中式锁序文档，顺序只能从调用链推断（`THREAD-02` 要求 multi-lock 路径记录顺序，`docs/explicit-io-contract.md:1029-1030`）。

## 2. core 锁计数闭合：`9 + T + 2P`

### 2.1 结构（COUNTED 调用 + 源码）

`9` 次固定：提交 3（`reserve`/`accept`/`claim_execution`，`S:src/async/uring_backend.cpp:447,514`；`S:src/async/uring_transport.cpp:152`）+ 完成 6（`offer_terminal`/`release_execution`/`begin_publication`/`complete_publication`/`claim_observer_delivery`/`retire_observer_delivery`，`S:src/async/uring_completion.cpp:70-95`；`S:src/async/uring_backend.cpp:584,598,611-613`）。

`T`：每次 `take` 一次 `consume_public_result`（`S:src/async/detail/request_core.cpp:325`）。

`2P`：每 progress pass 两次——`outstanding_summary`（`S:src/async/uring_backend.cpp:326,706-708` → `S:src/async/detail/request_core.cpp:93-96`）与 `health_failed`（`S:src/async/async_io_context.cpp:433` → `S:src/async/detail/request_core.cpp:246-249`）。

### 2.2 闭合（**COUNTED** 计数；`P` 为 **INFERRED** 恒等式）

| cell | T | harvest/op | getevents/op | P | `9+T+2P` | 实测 core lock/op | 残差 |
|---|---:|---:|---:|---:|---:|---:|---:|
| A1/D1 | 2.000000 | 2.000000 | 1.000000 | 1.000000 | 13.000000 | 13.000000 | 0 |
| A4/D4 | 3.499999 | 1.250000 | 0.999999 | 0.250001 | 13.000001 | 12.999999 | +2e−6 |
| A2/D16 | 9.499953 | 1.062495 | 0.999995 | 0.062500 | 18.624953 | 18.624953 | −3.6e−15 |
| A5/D64 | 33.497575 | 1.015564 | 0.999938 | 0.015626 | 42.528827 | 42.528826 | +1e−6 |
| A3/64k | 3.499995 | 1.249998 | 0.999998 | 0.250000 | 12.999995 | 12.999995 | 0 |

**结论**：

1. `9+T` 只是“生命周期锁”口径。原 U3.2 把 A1 的 +2.0 标 `UNEXPLAINED`，实际是漏了 `2P`（C=1 时 `2P=2`）。
2. 计数上**没有未解释的额外获取**：公式与实测闭合到 ≤2e−6。这**不**等于“没有可合并的锁”——冗余性不是计数问题，见 §5（C7 为 NOT_ADMITTED，且未找到具体冗余获取点）。
3. 随深度增长的是 `T`（2→33.5），不是固定 9 次。

## 3. 每把锁的语义职责与删除后果（`SEMANTICALLY_NECESSARY_UNDER_CURRENT_DESIGN`）

| 锁 | publication | terminal visibility | reclamation | unique consumption | 删除后果（源码审计 §5.5） |
|---|:--:|:--:|:--:|:--:|---|
| `RequestCore::mutex_` | ✅ | ✅ | ✅ | ✅ | double accept / double terminal / double consume / duplicate free |
| `dispatch_mtx_` | ✅（后端发布交接） | ◐（数据可见性在 core；本锁负责 CQE→发布队列顺序） | ◐（router/ledger 钉住借用） | — | 丢/重排 publication；错配 CQE；execution_ref 永不释放 → 借用无法退休 |
| `work_mtx_` + `work_cv_` | ✅（worker→owner 交接） | ◐ | ◐ | — | worker 用未发布的 prepared 参数执行（错误 fd/buffer）；丢唤醒；错误 quiescence |
| `access_mtx_` | —（仅接受线性化） | — | — | — | 多 submitter 串行接受失效；双 poll；move/destroy 竞态 |
| `ProgressSource::mtx_` | — | — | — | —（no-lost-wake/control 活性） | 丢唤醒、控制代际竞态 → settlement 永不收敛 |

`RequestScope` 无锁，其线程安全性由 adapter 的单 job 线程不变式提供（`B:adapters/fio/src/engine.cpp:71-73,187`），不是类自身保证。

## 4. 成本分类（**禁止**把 flat 份额当等待）

| 类别 | 本轮证据 | 类 | 判断 |
|---|---|---|---|
| **uncontended 固定成本** | 1,436.2 cyc/op ÷ 48.256 次 = 28.6–29.8 cyc/次；top-2 1,380.6 ÷ 48.256 = 28.61 | SAMPLED 派生 | 主要成分；libc 快路径 + 调用/PLT |
| **contended waiting** | futex 0.00099–0.00127/op（`E3` bpf-locks，n=6）；off-CPU 100.8–101.0 ms/5s 中约 100.2–100.3 ms 是单次 futex 等待（main 等 job，≈0.0004/op）；runqueue 20–29 µs/5s | COUNTED（bpf） | 稳态未见竞争；job on-CPU 96–98% |
| **cacheline bouncing** | `xsnp_hitm` 0.0098/op、RFO HITM 0.0074/op、`machine_clears.memory_ordering` 0.2147/op（`E3` pmu-hitm/memorder，n=1） | COUNTED（PMU 100% running） | 未见实质 bouncing |
| **atomic** | `mem_uops_retired.lock_loads:u` 51.262/op = 48.256 mutex 快路径 + **3.005 core atomics/op**（2 提交 + 1 完成，与源码审计差 0.005） | COUNTED（分解为 INFERRED） | core atomics 很少 |
| **critical-section work** | `dispatch_one_locked` 3.30%、publication 1.39%、`consume` self 3.06%、`take` 11.68% | SAMPLED | 小于锁原语总成本 |
| **cross-thread handoff** | uring：submit 与 progress 同线程（fio job），**无用户态 handoff**；TP：`work_mtx_`+`work_cv_` + 每完成 1 次 eventfd write + futex wake + 线程切换 | COUNTED（静态）/NOT_IDENTIFIED（切换成本） | uring 的锁基本无竞争 |

### 4.1 “29% 不是 29% 锁等待”的四条理由

1. **符号位置**：样本落在 `pthread_mutex_lock`/`unlock` 的指令上；无竞争时同样退休周期。
2. **竞争证据缺失**：futex 0.001/op、on-CPU 96–98%、HITM 0.0098/op。
3. **探针校准**：uprobe 锁持有 1,768 ns ≈ 无竞争 mutbench 1,766.7/1,797.1 ns；`lock_gt_5us` 0.22% vs 0.036%/0.065% → 探针开销。
4. **量级自洽**：48.256 次调用 × 28.6–29.8 cyc ≈ 1.38–1.44k cyc/op，正好等于 flat 的 1,380.6–1,436.2；如果是等待，应出现大量 off-CPU 与 futex，而它们不存在。

**futex 的四元组（`E3` bpf-locks/bpf-sched，n=6，窗口 5 s，约 37.9k–38.5k op）**：

| 量 | 观测 | 折算 |
|---|---|---|
| futex 调用 | 40–48 次/5s | 0.00105–0.00127/op |
| futex 等待事件 | 15–18 次/5s | 0.00040–0.00048/op |
| futex 等待总时长 | 103.3–104.9 ms/5s | ≈2.1% 墙钟；摊到全部 op ≈2.7–2.8 µs/op（不代表 per-op 阻塞） |
| 单次最长等待 | ≈100.25–100.29 ms，1 次/5s | main 等 job 的一次 park |

### 4.2 core / fio 拆分（**INFERRED**，分摊估计）

- BPF 计数：lock 24.128/op + unlock 24.128/op = 48.256 次调用/op（全部 fio 线程）。
- research 计数：core lock **18.625/op**（D16）→ core 占 77.2% 的锁对。
- 因此 record-root 的 1,436.2 cyc/op 中，core 的**分摊估计 = 1,108.6 cyc/op（24.4%）**，其余 ≈327.6（7.2%）属 fio/adapter。**这不是严格上界**：core 单次调用若比平均值更贵，份额可以更高；严格界只有 0–1,436.2。
- 该分摊假设“每次调用同价”，**未测**；24.128 来自 release+sym+BPF，18.625 来自 research → 跨构建拼接。

## 5. 必要 ≠ 最小：候选状态

| ID | 候选 | 状态 | 说明 |
|---|---|---|---|
| C6 | 删除/替换 core mutex、lock-free consume | **NO_GO** | 四职责必需；删除产生 double accept/terminal/consume/duplicate free |
| C7 | 合并固定 9 次 core 锁 | **NOT_ADMITTED** | 9 次是当前语义边界；计数已被 9+T+2P 完全解释；无可删除冗余证据；合并需重新设计 |
| C8 | ThreadPool 跨线程 handoff/调度 | **RESEARCH_ONLY** | D16 TP instr −1.1% 但 cycles +96%、CPU +11.9 µs；机制未识别 |
| C12 | Kernel/io-wq/IRQ 成本 | **RESEARCH_ONLY** | `cycles:k` 只覆盖 fio 线程上下文 |

### 5.1 若要申请 C7 的实验，最低要求

1. 语义等价证明：合并后的锁必须仍提供 `publication`、`terminal visibility`、`reclamation`、`unique consumption` 四项保证，且不引入新的锁序反转；
2. 单因素：只改锁结构，不改 scope 扫描、不改 harvest、不改 batch；
3. 先证明“有冗余”：必须指出具体哪两次获取保护同一状态且可合并——本轮的 28 个获取点逐项核对后**没有**找到；
4. 同 build 对照 + counted 锁计数 + record-root；收益判据是“锁计数下降 + 总 cycles 不升”，不是“热点份额下降”。

## 6. 未闭合项

- **持锁时长**：`access_mtx_`（跨 `io_uring_submit`）、`dispatch_mtx_`（跨 poll 排空/poison）、`work_mtx_` 的持锁时长**未测**；只有原语校准（7.8 ns 无竞争 lock+unlock，非产品）。
- **core/fio mutex 拆分**：只有按调用数的分摊估计（1,108.6），不是上界；未逐符号归属。
- **多线程 product API** 的竞争形态：fio 单 job 线程下 core 锁基本无竞争；非 fio 使用（多 submitter）未测。
- **内核侧**：io-wq/IRQ/softirq 的同步成本不在本文范围（`NOT_IDENTIFIED`）。
- **D64**：只有 counted 锁数（42.53/op），没有 record-root，因此“42.53 次锁”对应的 cycles 未测。

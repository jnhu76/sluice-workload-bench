# Adapter 扫描成本（fio adapter 的 queue / harvest / getevents）

- 范围：`adapters/fio/src/{queue,events,engine,files}.cpp` 在 sluice-uring 臂上的用户态工作。
- 判词归属：**`USERSPACE_COST_PARTIALLY_ATTRIBUTED`**。本文只回答“adapter 自己做了什么、多少钱、能不能算成扫描成本”。
- 记号：`E1`=`results/userspace-cost/session-001-baseline`，`E2`=`.../session-002-counters`，`E3`=`.../session-003-sync-consumer`，`E4`=`results/attribution/session-002-root`，`E5`=`results/attribution/session-001`；`S:`=Sluice 仓库，`B:`=本仓库。
- 标签：**COUNTED** / **SAMPLED** / **INFERRED** / **NOT_IDENTIFIED**。

## 1. 每 op 的 adapter 工作（COUNTED 调用数）

| 工作 | 每 op | 类 | 证据 |
|---|---:|---|---|
| `sluice_queue` 调用 | 1 | COUNTED | `E2` `submit_read/op=1.000000`（五 cell）；`B:adapters/fio/src/queue.cpp:35` |
| fd→`FileAccess` 哈希查找（1 条 64 位 `div` + 桶链比较） | 1 | COUNTED（静态） | `B:adapters/fio/src/queue.cpp:56`；`D:0x3fc3`、`0x4001` |
| adapter 自由槽 pop/push | 各 1 | COUNTED（静态） | `B:adapters/fio/src/queue.cpp:64,72-73`；`B:adapters/fio/src/events.cpp:85` |
| harvest 轮数 | 2.0 / 1.25 / 1.0625 / 1.0156 / 1.25（D1/D4/D16/D64/A3） | COUNTED | `E2/logs/counts-summary.txt` |
| 每轮扫描槽数 | C = 1 / 4 / 16 / 64 | COUNTED（结构） | `B:adapters/fio/src/events.cpp:92`（`i < slots.size()`） |
| `harvest_slots_examined/op` | 2 / 3.499999 / 9.499953 / 33.497576 | COUNTED | `E2/runs/*/counters.json` |
| 每轮 `take` 调用 | ≤ C（`staged.size()<max` 截断） | COUNTED | `B:adapters/fio/src/events.cpp:92,100,114` |
| `sluice_getevents` 调用 | ≈1（0.999938–1.000000） | COUNTED | `E2`；fio 默认 `min=max=1` |
| `sluice_event` 调用 | 1/交付完成 | COUNTED（静态） | `B:adapters/fio/src/events.cpp:298` |
| 堆分配 | 0（稳态；`staged`/`free_slots` 已 `reserve(capacity)`） | COUNTED（静态） | `B:adapters/fio/src/engine.cpp:133,138`；反汇编 `operator new` 只在增长分支 |

## 2. 扫描结构：O(C) 外套 O(C)

`harvest` 的循环（`B:adapters/fio/src/events.cpp:92-171`）对每个非空槽调用 `state->scope->take(slot.data)`（`:100`、`:114`）。而 `RequestScope::take` 内部先做 `resolve_` 的 O(C) 全表扫描（`S:include/sluice/async/request_scope.hpp:114-129`），再 `Request::take_result()`（`S:include/sluice/async/request.hpp:146-159`）→ `consume`（core 锁）。

因此单轮 harvest 的槽访问次数是 **O(C + K·C)**（K=在飞占用槽数），k 为命中槽下标时典型 `K+k`。**注意：这里的 O(C) 是请求 scope 的扫描，不是 `RequestCore` 的查找**——core 的 `resolve_internal_` 是按 slot 索引的 O(1)（`S:src/async/detail/request_core.cpp:121-145`）。

### 2.1 扫描长度分布（COUNTED，`E2` research，rep1/rep2 一致 ≤1e−6）

| cell | C | 扫描均值 slots/harvest | p50 / p90 / p99 / max | `scan_hist` 形状 |
|---|---:|---|---|---|
| A1/D1 | 1 | 1.0 | 1 / 1 / 1 / 1 | bucket1 = 7,741,084（bucket0=0） |
| A4/D4 | 4 | 2.8 | 3 / 4 / 4 / 4 | bucket1..3 各 973,849/973,849/973,850；bucket4=1,947,699 |
| A2/D16 | 16 | 8.9412 | 9 / 16 / 16 / 16 | bucket1..15 各 ≈188,130；bucket16=376,260 |
| A5/D64 | 64 | 32.9842 | 33 / 59 / 64 / 64 | bucket1..63 各 ≈15,621；bucket64=31,242 |
| A3/64k | 4 | 2.8 | 3 / 4 / 4 / 4 | bucket1..3 各 281,042/281,041/281,041；bucket4=562,084 |

形状 = **1..C−1 近似均匀 + C 桶约为单桶 2 倍**，均值落在 `C(C+3)/(2(C+1))`。这是本轮 5 cell × 2 rep 的**描述性观测恒等式**，不是通用定律；`scan_hist` 在 >64 时饱和（本轮 max iodepth=64，未发生，weighted-sum 不变量通过）。

## 3. adapter 自己的成本有多大（SAMPLED）

A2/D16 record-root（`E3`，n=1，10,195 samples，Lost 0，4,550.59 cycles/op）：

| 符号 | flat self | cyc/op | 类 |
|---|---:|---:|---|
| `fio_sluice::sluice_queue` | 2.88% | 131.1 | SAMPLED |
| `fio_sluice::harvest` | 1.88% | 85.6 | SAMPLED |
| `fio_sluice::sluice_getevents` | 0.55% | 25.0 | SAMPLED |
| **adapter 合计** | **5.31%** | **241.6** | SAMPLED |

r2 root 会话给出 D16 `harvest` self **1.72%**（`COST-LEDGER.md:175`），与 1.88% 同量级；两个 root 的差异属采样/构建差异，不是趋势证据。

### 3.1 不能把 children 当 adapter 成本

调用树（`perf-report.txt` children，路径总量、彼此嵌套）：

- `harvest` 30.50% → 其中 **28.47pp 是 `RequestScope::take`**（core 侧），adapter 自身扫描只有 1.88%。
- `sluice_queue` 21.34% → `RequestScope::submit_read` 18.45% → `AsyncIoContext::submit_read` 17.58% → `UringAsyncBackend::submit_read` 15.90% → `dispatch_after_accept` 9.56–9.58%。
- `sluice_getevents` 49.02% → `harvest` 30.50% + `poll_progress` 17.97%。

**结论**：单看调用树会把 30–49% 记到 adapter 头上，这是错的；adapter 自己的 self 是 5.31%。

## 4. 与深度、构建、观察者的关系（边界）

1. **深度**：adapter 扫描是 O(C)/轮，但每轮成本小；随深度增长的主项是**重复 take 的调用次数**（take/op 2→3.5→9.5→33.5），不是单次扫描变贵。单次 take 成本在扫描长度 1.0→8.94 时没有上升（见 `REQUEST_SCOPE_LOOKUP_COST.md` §3）。
2. **构建**：research 构建插入计数器，instr/op +124…+3,367、IOPS −4.63%…−56.81%（`E2/logs/perturbation-summary.txt`，COUNTED）。计数值（调用数、扫描分布）来自被扰动构建，对 release 动力学的代表性 `NOT_IDENTIFIED`。
3. **观察者**：record/bpftrace 会改变吞吐（A1 sluice record −13.6%、TP record −35.9%；bpftrace 压到约 7.6k IOPS）。份额只能作路径指示，不作性能结论。
4. **窗口**：A1/A2 是 10s `time_based`，**W-C2** 是固定 20G；跨模式差 sluice 7.41%，`MODE_OR_TIMING_EFFECT_NOT_IDENTIFIED`，不得当启动成本。

## 5. 候选裁决（本层）

| ID | 主张 | 状态 | 依据 |
|---|---|---|---|
| C2 | 消除 O(depth) harvest 槽扫描 | **RESEARCH_ONLY**（并入 C1） | harvest self 1.72–1.88%，直接成本小；它是重复 take 的上游触发器，应并入 C1 的同一实验，不单列 |
| C5 | “harvest 扫描是用户态主成本” | **NO_GO** | self 仅 1.88%；take 11.68%、consume 3.06%、mutex 31.56% 才是大头 |
| C9 | io_uring 提交批处理 | **NOT_ADMITTED** | native/sluice 均 1.0000 enter/op（`E2`、`E4`；trace **NON_EXACT**，只作下界），主依据是 fio 默认 `min=max=1` 的静态配置；无单独 enter 成本；#595 KEEP_IMMEDIATE |
| C10 | research 计数器开销 | **NO_GO（产品候选）** | 是测量扰动，不是产品成本 |

## 6. 什么证据才能把“扫描成本”说成因果

需要同时满足：

1. **同 build 对照**（release vs release，或同一计数器构建内开关），避免把构建差异当扫描成本；
2. **单因素**：只改扫描长度/槽分布（例如 ready-index），不改完成观察顺序、不改 `getevents` 语义、不改 batch；
3. **counted 分母**：take/op、scan mean、core lock/op 同 run 复核；
4. **可证伪预测**：若扫描是独立成本，缩短扫描应使“单次 take 的 cycles”下降；现有数据（58.41→53.45/55.95，跨 root、n=1，**不是单因素**）**未观察到**这一点，所以不要再立“扫描长度”假设，应直接测 C1 的重复调用假设。

## 7. 未闭合项

- adapter 的 `div`、分支、槽拷贝的**周期成本**未测（NOT_IDENTIFIED）。
- `harvest` 的写侧（`staged.push_back`、`release_slot`）与 cache 行为未单独归因。
- 内核侧 `io_uring_enter`/CQE 的 per-op 成本不计入 adapter（在 §B.4 的内核行）。
- fio driver 自身的 25.68% 是 `object=fio` 的 flat self，不是 adapter，也不是 Sluice。

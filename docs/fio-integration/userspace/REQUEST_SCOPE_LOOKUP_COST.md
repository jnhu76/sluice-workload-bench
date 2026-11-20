# RequestScope 查找成本（resolve_ / take / 重复消费）

- 对象：`RequestScope`（Sluice `5d94f233…`）的槽表查找与 `take` 路径，以及 adapter `harvest` 对它的调用形态。
- 判词归属：**`USERSPACE_COST_PARTIALLY_ATTRIBUTED`**。本文的核心结论是**否定性**的：没有证据支持“扫描长度是 D16 增量主因”，也**没有**证据支持 O(1) 索引能带来可测收益；唯一值得做 A/B 的是“pending 重复 take/consume”。
- 标签：**COUNTED** / **SAMPLED** / **INFERRED** / **NOT_IDENTIFIED**。

## 1. 代码事实（COUNTED 静态）

| 事实 | 证据 |
|---|---|
| `RequestScope::take` 是 public 模板入口，转发到 `take_<T>` | `S:include/sluice/async/request_scope.hpp:56-57`；`S:src/async/request_scope.cpp:237-244` |
| `resolve_` 从 index 0 扫到 `capacity_`，逐项判 `state==owning`、variant 索引、`id()` 全等 | `S:include/sluice/async/request_scope.hpp:114-129`；`D:0x9320` 循环（步长 0x40） |
| **ticket 自带 `id().slot_` 但 `resolve_` 不使用它** | 同上；源码审计 §3（`resolve_` 无下标寻址） |
| `take_` 先 `resolve_`，再 `take_result()`；仅 `ready` 时 `release_slot_` | `S:include/sluice/async/request_scope.hpp:131-141` |
| `take_result()` 调 `RequestBridge::consume`，**pending 判定在 core 锁之后** | `S:include/sluice/async/request.hpp:146-159`（consume 在 `:153`）；`S:src/async/detail/request_core.cpp:325-331` |
| `RequestCore` 的查找是 O(1)（slot 索引 + context/phase/generation 校验） | `S:src/async/detail/request_core.cpp:121-160` |
| `RequestScope` 无锁；单线程安全性来自 adapter 单 job 线程不变式 | `S:include/sluice/async/request_scope.hpp:46-72`；`B:adapters/fio/src/engine.cpp:71-73,187` |
| adapter 的 harvest 是**非破坏性**的：pending 槽不释放，下一轮再扫 | `B:adapters/fio/src/events.cpp:91-171`（pending 分支只 `break`） |

## 2. 计数（COUNTED，`E2` research；rep1/rep2 一致 ≤1e−6）

| cell | C | take/op | pending/op | pending/take | resolve iters/op | resolve iters/take |
|---|---:|---:|---:|---:|---:|---:|
| A1/D1 | 1 | 2.000000 | 1.000000 | 0.500000 | 2.000000 | 1.000000 |
| A4/D4 | 4 | 3.499999 | 2.499999 | 0.714286 | 7.499997 | 2.142857 |
| A2/D16 | 16 | 9.499953 | 8.499953 | 0.894736 | 59.499681 | 6.263156 |
| A5/D64 | 64 | 33.497575 | 32.497575 | 0.970147 | 747.443814 | 22.313371 |
| A3/64k | 4 | 3.499995 | 2.499995 | 0.714285 | 7.499991 | 2.142858 |

读法：

- `take/op ≈ C/2 + 1.5`、`pending/op ≈ C/2 + 0.5`、`harvest/op ≈ 1 + 1/C` 是本轮 5 cell 的描述性拟合，不是定律。
- **每 op 的 resolve 迭代 = take/op × 平均扫描长度**：D64 的 747.4 ≈ 33.5 × 22.3。所谓“O(depth²)”只在这个意义上成立（调用次数 × 每次扫描），不是 core 查找变慢。
- `resolve iters/take`（1.0→22.3）与 `scan mean`（1.0→32.98）不同：`harvest` 统计的是“看过的槽”（含空槽），`resolve_` 在命中处停止。两者不能互相替代。

## 3. 单次 take 的成本不随 resolve 迭代上升（SAMPLED，n=1/root）

| 根 | run | take self | cycles/op | take/op | **cyc/take** | resolve iters/take | harvest slots/harvest |
|---|---|---:|---:|---:|---:|---:|---:|
| `E5` | A1 record rep1 | 2.44% | 4,787.93 | 2 | **58.41** | 1.00 | 1.0 |
| `E4` | A2 record-root rep1 | 10.45% | 4,859.52 | 9.5 | **53.45** | 6.26 | ≈8.94 |
| `E3` | A2 record-root rep1 | 11.68% | 4,550.59 | 9.5 | **55.95** | 6.26 | 8.94 |

`resolve_` 迭代/take 从 1.00 增至 6.26（约 6.3×）；8.94 是**上一层 adapter 的 harvest slots/harvest**，不是 take 的扫描长度。单次 take 成本 58.41 → 53.45（−8.5%）或 55.95（−4.2%）。**没有单调上升**；差异在 n=1 的采样噪声量级内，方向也不支持“resolve 迭代越多越贵”。

> 裁决材料里的 `57.45 cyc/take` 使用 r2 的 12.15% 份额（`COST-LEDGER.md:175`），本轮在归档 flat 中**未能复现**该三元组（12.15% / 4,492.11 / 9.499953）。换成 53.45 或 55.95 后，结论方向不变且更强。

**证据强度限制**：上表三个 root 是**不同会话**（E5 A1、E4 A2、E3 A2），不是同 build、同 root、只改扫描长度的对照；E4 与 E3 的 A2 本身就有 10.45% vs 11.68% 的份额差与 4,859.52 vs 4,550.59 cyc/op 的差异（record n=1，只有采样二项 CI，无 run-to-run CI）。因此本表只能得出“**没有观察到随扫描长度上升**”，不能给出 cyc/take 的置信区间，也不能单独承担证伪；D4<D1 的全链旁证同样受 depth 多因素打包限制。

**这不是说扫描免费**：take self 11.68%（531.5 cyc/op）是真实成本；但它包含每次调用的固定工作（variant 检查、`take_result`、ready 时 `release_slot_`、以及 consume 的调用/PLT 边缘），而不是“扫描 N 个槽的边际成本”。在没有单因素 A/B 前，不能把 531.5 拆成“扫描 X + 固定 Y”。

## 4. pending 的重复付费（结构 + COUNTED 调用）

D16 时每 op 有 8.5 次 pending take；每次都要：

1. `resolve_` 线性扫描到该 ticket 的槽（`request_scope.hpp:114-129`）；
2. `take_result()` → `RequestBridge::consume` → `RequestCore::consume_public_result`，**取一次 core 锁后才判 `pending`**（`request_core.cpp:325-331`）；
3. 返回 pending，槽保持 owning，下一轮 harvest 重复 1–2。

所以 pending 的成本是“扫描 + 一次 core 锁”的**重复**，而不是“等待”。这与 §F 的锁计数闭合一致：core 锁 = `9 + T + 2P`，T 里 89.47% 是 pending。

### 4.1 可复算的成本分摊（INFERRED）

以 `E3` A2/D16 为基：

| 项 | 值 | 说明 |
|---|---:|---|
| take self | 531.5 cyc/op | SAMPLED，11.68% |
| consume self | 139.3 cyc/op | SAMPLED，3.06% |
| pending 占 take 调用 | 89.47% | COUNTED |
| pending 分摊 take+consume self | 600.2 cyc/op | INFERRED（按调用数比例，per-call 成本不均匀） |
| pending 对应的 core 锁（8.5 对 × 57.22–59.52 cyc/对） | 486.4–505.9 cyc/op | INFERRED（libc pair 价，非 core 专用价） |
| **合计（模型估计，不是净收益）** | **1,086.5–1,106.1 cyc/op（约 1,087–1,106；23.9–24.3%）** | INFERRED；pending 与 ready 的平均 self 相同、pending core 锁与全部 libc 锁平均价相同，两个假设均未验证；新方案还要支付就绪登记/单次消费成本。旧稿 243–253/843–853 把 pair 数乘以 per-call 价，少算一倍；`[243,853]` 与 `923.8` 已撤回 |

裁决材料给出的 0.6–1.1k（13–24%）区间与本模型相容；材料中的 506 cyc/op = `8.5 × (1,436.16/24.128)` 是 **pair 口径**，单位正确。本表早期把 8.5 对乘以 28.61–29.76 cyc/调用，少算一倍，已订正为 486–506。

## 5. 候选裁决

| ID | 候选 | 状态 | 说明 |
|---|---|---|---|
| **C1** | pending 重复 take/consume → ready-index / 单次消费 | **EVIDENCE_READY**（只申请 G1/G2 单因素 A/B） | 有 counted 调用负担（pending/op 1/2.5/8.5/32.5）与 SAMPLED self 份额；**反事实未跑，收益未证明** |
| C3 | `resolve_` → O(1) 槽索引 | **NOT_ADMITTED** | 单次 take 不随 resolve 迭代上升（1.00→6.26 iters/take；8.94 是 adapter 的 harvest slots/harvest，不是 take 扫描）；D4 全链差 < D1；**跨 root/跨会话（E5/E4/E3，n=1）、非单因素**；只否定“显著收益”，不证明无收益 |
| C4 | “扫描长度是 D16 增量主因” | **NO_GO** | 现有证据不支持（**非单因素证伪**）：增量主要随 take 调用次数 2→9.5 增长；D4<D1 只是旁证 |

### 5.1 C1 的可证伪预测（**若**做 A/B，应观测到的量）

若“消除 pending 重复 take/consume”成立且语义保持：

| 预测 | 现测（D16） | 预期 | 观测方式 |
|---|---:|---:|---|
| take/op | 9.499953 | ≈1/完成（≈1.0） | research 计数器 |
| core lock/op | 18.624953 | ≈`9 + 1 + 2P ≈ 10.125` | 同上 |
| D64 core lock/op | 42.528826 | ≈`9 + 1 + 0.031 ≈ 10.03` | 同上 |
| pending take 份额 | 89.47% | ≈0 | 同上 |
| take+consume self | 670.8 cyc/op | 下降（幅度取决于 per-call 成本，**不可线性外推**） | record-root |
| IOPS / CPU | 379.6k / 2.745 µs | 不劣化（这是“不伤害”判据，不是收益判据） | fio run |

**必须同时满足的语义条件**（否则实验无效）：

1. 每个 ready 恰好一次消费（`binding_live=false` 只发生一次，`request_core.cpp:334-341`）；
2. 回收谓词 `published && !binding_live && execution_refs==0 && control_refs==0 && !publication_inflight && observer retired` 不被提前满足（`:580-590`）；
3. **不能**改变 fio 的完成观察形状：`getevents` 仍按 batch 交付、`staged` 仍按 `event()` 逐个归还（`B:adapters/fio/src/events.cpp:212-216,298`）；
4. 陈旧 ticket 仍返回 `empty` 并 fail-closed（`:104-106`）。

任何“先看 ready 再消费”的实现必须证明：在 core 锁之外观察到 ready 之后，消费时仍能命中同一 generation（否则会 double consume 或漏消费）。

## 6. H5 / 扫描因果的边界（必须与本文一起引用）

`docs/fio-integration/COST-LEDGER.md` 的 H5 当前判词是 **`HOTSPOT_OBSERVED / CAUSAL_COST_NOT_ISOLATED`**（`:414`），其含义：

- 调用树只支持“D16 存在函数级热点（core `take`）”；**不支持**容量扫描的净因果成本。
- counted 深度差只支持“D16 用户态差额更大”，且是全链差额，**不能**拆归 take/scan。
- 采样份额（12.15% / root 10.45%）是**各 run 内占比**，不是 per-op 函数计数；份额上升不能写成“净因果成本上升”。
- D4<D1 非单调未闭合；depth 是多因素打包；#579 要求的单因素 A/B 未做。

本文的额外证据（单次 take 不随扫描长度上升）把 H5 从“未隔离”进一步推向“扫描长度假设不成立”，但**仍不是**净成本分解。要让 H5 变成 `ATTRIBUTED`，最低要求是 §5.1 的同 build 单因素实验 + 每 op 计数闭合。

## 7. 未闭合项

- 531.5 cyc/op 的 take self 中“固定开销 / 扫描 / consume 边缘”三者的拆分：**NOT_IDENTIFIED**（需要指令级或单因素）。
- `resolve_` 扫描的 cache 行为（64 B 步长、C 个槽的足迹）未单独测量。
- 本轮没有 D64 的 record-root，因此 33.5 take/op 的 self 成本没有直接采样值；D64 的 42.53 锁/op 是 counted，其 cycles 归属未知。
- ticket 里的 `slot_` 未被使用是源码事实，但“改为 O(1) 能省多少”**没有**证据（且现有数据反对它有显著收益）。

# RequestBridge consume 成本（唯一消费 / 释放绑定 / 回收）

- 对象：`RequestBridge::consume` → `RequestCore::consume_public_result`（release 中已完全内联）以及其后的 `try_reclaim_`/`release_slot_`。
- 判词归属：**`USERSPACE_COST_PARTIALLY_ATTRIBUTED`**。本文结论：consume 的 core 锁是**唯一消费 + 回收**的语义点，`SEMANTICALLY_NECESSARY_UNDER_CURRENT_DESIGN`；其 flat self 是**快路径 cycles 归属**，不是等待；删除属 **NO_GO**。
- 标签：**COUNTED** / **SAMPLED** / **INFERRED** / **NOT_IDENTIFIED**。

## 1. 调用链（COUNTED 静态）

| 步骤 | 代码 | 关键动作 |
|---|---|---|
| 1 | `S:include/sluice/async/request_scope.hpp:131-141`（`take_`） | `resolve_` 找到槽后调 `take_result()` |
| 2 | `S:include/sluice/async/request.hpp:146-159` | `take_result()` → `RequestBridge::consume(core_, id_, &data)`（`:153`）；`ready` 时清空 `core_/backend_/id_`（`:155-158`） |
| 3 | `S:src/async/detail/request_core.cpp:323-344` | `consume_public_result`：`lock_guard(mutex_)`（`:325`）→ `resolve_public_`（O(1)，`:147-154`）→ `published` 判定（`:329-331`）→ 拷 outcome/metadata（`:332-337`）→ `binding_live=false` + `try_reclaim_`（`:339-341`） |
| 4 | `S:src/async/detail/request_core.cpp:580-597` | `reclaimable_` 五条件谓词；满足则 `try_reclaim_` → `release_slot_` |
| 5 | `S:src/async/detail/request_core.cpp:599-631` | 清 borrow/outcome/metadata/refs/observer，`generation+1`，槽回 `free_slots_`（generation 耗尽则 retired） |

`RequestCore::consume_public_result` 在 release 中**没有独立调用点**（完全内联进 `RequestBridge::consume`），`reclaimable_`、`release_slot_`（除 rollback 外）同样内联——因此 `record-root` 里 `consume` 的 self 已经把 core 的那段算进去了，而 `try_reclaim_` 只以 0.65% 单独出现。

## 2. 每 op 计数与成本

| 项 | 值 | 类 | 证据 |
|---|---|---|---|
| consume 调用/op | T = 2 / 3.5 / 9.5 / 33.4976（D1/D4/D16/D64） | COUNTED | `E2`；`bridge_consume_calls == take_calls == scope_take_calls` 不变量全过 |
| 每次 consume 的 core 锁 | 1 对 | COUNTED 调用 + 源码 | `S:src/async/detail/request_core.cpp:325` |
| pending 返回比例（D16） | 89.47%（8.499953 / 9.499953） | COUNTED | `E2` |
| consume self（A2/D16） | 3.06% = **139.3 cyc/op** | SAMPLED（`E3`，n=1） | flat |
| consume 子树的 lock/unlock | 12.71pp（7.29 + 5.42）= 578.4 cyc/op | SAMPLED（children） | `perf-report.txt`；是 mutex 31.56% 的**子集**，不得叠加 |
| core 查找复杂度 | O(1) | COUNTED（静态） | `S:src/async/detail/request_core.cpp:121-160` |

**读法**：consume self 139.3 cyc/op 是**函数体自身**的 cycles（含内联的 core 临界区工作，但不含被调用者 `pthread_mutex_lock` 的样本——那些落在 libc 符号上）。子树的 16.78% 是路径总量，不能当“consume 成本”。

### 2.1 与 pending 的关系

pending 也走完整路径：一次 `resolve_` 扫描 + 一次 core 锁 + `published` 判定失败后返回（`request_core.cpp:329-331`）。它**不**拷 outcome、**不**清 `binding_live`、**不**回收。所以 pending 的 consume 成本 ≈“锁 + 几次比较”，比 ready 的 consume 便宜；用调用数比例去分摊 self（`REQUEST_SCOPE_LOOKUP_COST.md` §4.1）是**近似**，不是分解。

> 因此把 take+consume self 按调用数比例分摊给 pending（`REQUEST_SCOPE_LOOKUP_COST.md` §4.1 的 600.2）会**高估** pending 的真实 self；该项的 pair 口径模型值为 **1,086.5–1,106.1 cyc/op（约 1,087–1,106；23.9–24.3%，INFERRED）**，不是可省净收益；两个未验证假设是 pending 与 ready 平均 self 相同、pending core 锁与全部 libc 锁平均价相同；早期 `[243,853]` 区间已因单位错误撤回。

## 3. 为什么这把锁不能删（`SEMANTICALLY_NECESSARY_UNDER_CURRENT_DESIGN`）

`RequestCore::mutex_` 在 consume 里同时承担四件事（缺一即错）：

| 职责 | 若缺失 | 具体后果 |
|---|---|---|
| **terminal visibility** | `offer_terminal` 写 outcome/metadata（`request_core.cpp:346-365`）与 consume 读之间没有 release/acquire | outcome 撕裂；读到半写的 `IoOutcome`/`FileInfo` |
| **unique consumption** | 两个线程同时看到 `published` | 同一请求被消费两次；`binding_live` 被两次清；`take_result` 两次清空句柄 |
| **reclamation** | `reclaimable_` 的五个条件与 `release_slot_` 不在同一临界区 | 槽在借用/observer/control 未 retire 时被复用 → 数据写错 fd/buffer |
| **generation 推进** | `generation+1` 与回收不同步 | 陈旧 ticket 命中新请求 → 误消费（ABA） |

源码审计的删除影响矩阵把 `RequestCore::mutex_` 的删除后果列为 **double accept / double terminal / double consume / duplicate free**（`USERSPACE-COST-SOURCE-AUDIT.md:411`）。这不是推测，而是“同一状态机被两个线程无串行化推进”的直接结果。

### 3.1 30% 不是等待

- `pthread_mutex_lock` 16.03% + `unlock` 14.31% = 30.34%（top-2），全部 lock 符号 31.56%。**SAMPLED**，n=1。
- BPF：48.256 次 lock/unlock 调用/op（24.128 对/op），折算 28.6–29.8 cyc/调用、57.2–59.5 cyc/对；futex 仅 0.00099–0.00127/op；job on-CPU 96–98%；HITM 0.0098/op。
- uprobe 的“锁持有 1,768 ns”与无竞争 mutbench 1,766.7/1,797.1 ns 在 2% 内 → 测到的是探针开销。
- 结论：这是**无竞争快路径的 cycles 归属（上限）**，不是 contention，也不是可删除的等待。

### 3.2 如果要做 lock-free consume，最低门槛（研究问题，不是提案）

1. 给出“唯一消费”的协议（例如每槽一次性 token / CAS 状态机 / epoch-based reclamation），并证明：
   - 任何线程最多成功消费一次；
   - 回收只能在 `execution_refs==0 && control_refs==0 && !publication_inflight && observer retired` 之后；
   - 陈旧 ticket 不命中新 generation；
2. 保留 outcome 的 release/acquire（否则要引入独立 publication fence）；
3. 单因素 A/B：只改 consume 的同步，不改 scope 扫描、不改 harvest 顺序、不改 batch；
4. 同 build 的 record-root + counted 对照，证明 139.3 cyc/op 或锁计数真的下降，且 IOPS/CPU 不劣化。

在这些条件满足前，C6 的状态是 **NO_GO**（不是“待研究”）。

## 4. 候选裁决

| ID | 候选 | 状态 |
|---|---|---|
| C1 | pending 重复 take/consume（含 consume 的重复锁） | **EVIDENCE_READY**（只申请 G1/G2 A/B；见 `REQUEST_SCOPE_LOOKUP_COST.md` §5） |
| C6 | 删除/替换 core mutex、lock-free consume | **NO_GO** |

## 5. 未闭合项

- **core vs fio 的 mutex 拆分**：31.56% 含 fio 自己的 mutex；按 core 锁对数分摊的**估计**是 1,108.6 cyc/op（24.4%），其余 ≈327.6（7.2%）属 fio/adapter。**它不是严格上界**（core 单次更贵时可更高；真界 0–1,436.2）。该分摊假设每次调用同价，**未测**；且 24.128 对/op（release+sym+BPF）与 18.625 对/op（research 计数）是跨构建拼接 → INFERRED。
- consume 的**持锁时长**没有单独测量；`dispatch_mtx_`/`access_mtx_` 的持锁时长同样没有（NOT_IDENTIFIED）。
- `try_reclaim_` 的 0.65% self 只覆盖未内联的部分；内联进 consume 的回收工作包含在 139.3 内，无法再拆。
- 多线程 product API 下 consume 的真实竞争形态（非 fio 单 job 线程）未测。

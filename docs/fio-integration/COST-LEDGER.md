# 成本账本与热点判定（COST-LEDGER）

状态：**正式账本**（session-001 原始回执独立复算；生成链 =
`tools/profile_fio_sluice.py analyze` → `analysis/normalized-costs.json` →
本表）。所有数字可回指 `results/attribution/session-001/runs/<run_id>/`
原始文件及其 sha256（`sha256-manifest.txt`）。

作用域声明（全表适用）：

- `PARTIAL_CPU_ACCOUNTING`：非 root 会话——perf 仅用户态（`:u`）事件；
  kernel cycles / io-wq CPU / IRQ 不可测（capability.json 为证）。本表
  **没有任何内核态 CPU 数字**，缺项一律 `UNAVAILABLE`，不以 0 冒充。
- per-op 口径 = `PROCESS_LIFECYCLE_*`（ramp=0，窗口=进程生命周期，分母=
  io_bytes/bs；启动/收尾占比见 window-contract.json）。
- 采样占比（perf record）只进 SAMPLED 列，永不与 counted 相加、不进
  per-op 表。cycles/instructions/CPU-time/latency 各维度独立呈现，不相加。
- 引擎：`io_uring`=fio 内建；`sluice-uring`/`sluice-threadpool`=external
  .so（release stripped 为计数基线；record 用 `.text` 哈希相等的 sym 双生）。

## 1. 主表：每 I/O 成本（用户态，counted）

<!-- LEDGER-DATA:BEGIN (numbers from analysis/normalized-costs.json; do not edit inside markers) -->
LEDGER_PLACEHOLDER
<!-- LEDGER-DATA:END -->

读法：Δ = sluice-uring − io_uring（同 cell 同 driver 同 offset 序列），
该差 = Adapter+Sluice 用户态组合差额（full-chain difference）——**不能**
直接当作"Sluice Core 指令数"（见 §3 边界）。

## 2. 内核交互与调度（strace 受控计数 + 软件事件）

<!-- TRACE-DATA:BEGIN -->
TRACE_PLACEHOLDER
<!-- TRACE-DATA:END -->

strace 行为 STRACE_DIAGNOSTIC：计数来自扰动运行（扰动幅度见表内
bare-vs-straced IOPS 比）；计数对形态成立（enter/op 等），不用于时延。

## 3. 归因边界（Attribution Limits）

full-chain 差额（§1 Δ）分解为：

| 层 | 证据 | 状态 |
| --- | --- | --- |
| fio driver 增量（async 语义 vs 内建） | 两臂同 fio 进程、同 job 机器；driver 增量包含在 Δ 内，无法单独扣除 | NOT_SEPARATED_WITHOUT_COUNTERFACTUAL |
| Adapter（queue/harvest/event） | perf record SAMPLED：`fio_sluice::` 命名空间占比 | SAMPLED |
| Sluice Core（RequestScope/RequestCore/Backend） | perf record SAMPLED：`sluice::` 命名空间占比 | SAMPLED |
| Kernel 侧差（io-wq/inline/FS） | 非 root 不可测；#622 已证 tmpfs vs btrfs 条件依赖 | UNAVAILABLE |
| 重复工作（adapter scan × scope resolve） | 结构存在（源码）；深度扩展检验见 §4 H5 | DEPTH_DIFFERENTIAL |

## 4. H1–H6 热点判定（#639 准入清单）

预注册判定词：`ATTRIBUTED` / `LIKELY_BUT_UNPROVEN` / `ADAPTER_ONLY` /
`NOT_EXPOSED` / `INCONCLUSIVE` / `INVALID_MEASUREMENT`。

<!-- H-CLASSIFICATION:BEGIN -->
H_PLACEHOLDER
<!-- H-CLASSIFICATION:END -->

## 5. 观察扰动（G7）

<!-- PERTURBATION:BEGIN -->
PERTURBATION_PLACEHOLDER
<!-- PERTURBATION:END -->

## 6. 完成观察边界（G6，#623 K/R/P/O）

K（kernel completion）/ R（reap）/ P（publication）/ O（take/observation）
四锚点在本环境（非 root、无产品探针）的可辨识性：

- K：`NOT_OBSERVABLE`（无 tracepoint/无 :k PMU）。
- R：部分——syscall 形态（enter 的 N2/min_complete）+ SAMPLED reap 成本；
  无逐请求时间戳。
- P：`UNIDENTIFIABLE_WITH_CURRENT_INSTRUMENTATION`（publication 边界在
  RequestCore `complete_publication`，产品 API 无时间戳；不得为观测改
  产品）。
- O：SAMPLED（`take`/`harvest`/`event` 采样占比）；无逐请求配对。
- 任何 CQE→publication→take 的逐请求时序主张：`AMBIGUOUS`，拒绝输出。

## 7. 禁止与未做

- 未把 wall/IOPS 差距换算成"机器工作"总量；未跨维度相加。
- 未用 strace 时延；未把采样占比当 cycles 份额。
- 未运行 F3 排名；未做 tmpfs 对照（单 FS=btrfs 限制，明示）。
- 未修改 Sluice/fio 语义；诊断构建 `.text` 哈希与 release 一致（binary-
  manifest.json `text_section_match`）。

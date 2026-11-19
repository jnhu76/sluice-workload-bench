# G0 — 仓库现实与既有证据（PMU/eBPF 归因战役）

状态：**正式审计输出**（本轮战役 G0 门）。本文件冻结战役开始时的仓库/二进制/环境
现实，以及 #622 既有研究成果的可用性边界。所有历史数字在本轮一律不作为已验证
事实复用；本轮所有判词以当前 session 的原始回执为准。

## 1. 仓库现实（2026-11-19 现场）

| 项 | 读数 |
| --- | --- |
| bench HEAD = origin/main | `7a8b851b5994a3b819ea67c27747e48ee92aad31`（clean，`git fetch --prune` 后无漂移） |
| 研究分支 | `research/fio-pmu-ebpf-attribution`（自 7a8b851 切出） |
| SLUICE_REVISION（仓内钉住） | `5d94f233967c3aef993eb19ba9e3252c532b0951` |
| Sluice 产品 checkout | `/home/jnhu/Source/Sluice` @ `5d94f23…`（与钉住一致；**只读**） |
| fio 上游钉住 | `build/fio/upstream` @ `ff930c4653ae3952d6b09ab3ec89671aeabf2cbe`（tag fio-3.40）；宿主 `/usr/sbin/fio` = fio-3.40 |
| 引擎产物（release，stripped） | `build/linux/x86_64/release/fio-sluice-ring.so` sha256 `0b03514af886330f1a6923c6185247a355bf6e77f1def5008b3b6570fb3ae132`；`fio-sluice-noring.so` sha256 `21e41cf924b3c97ab3df43414c588031c6ae53151da0cd032979b46a1b52c4fc` |
| SDK 静态库 | `build/ring-pic/sdk/lib/libsluice_*.a` 等四套 profile（ring/noring × pic/非pic），符号表完整（libsluice_async.a 382 个局部符号） |
| 既有工具 | `tools/run_fio_matrix.py`（串行矩阵 + 环境快照 + rusage + tripwire）、`tools/compare_fio.py`、`tools/check_fio_engine.py`（正确性冒烟）、`tools/check.py`、`tools/run_matrix.py`、`tools/build_sdk.sh` |

## 2. #622 既有研究的可用性边界（关键裁决）

**#622 本地证据树已不存在**：`/home/jnhu/sluice-622-observability/` 目录缺失
（含 `manifest.sha256`、`results/TOOL_MATRIX.md`、`results/COST_MAP.md`、
`results/VALIDITY.md`、`results/DECISION.md`、`correlation/CORRELATION.md`、
bpftrace 模板），repro tarball `sluice-622-e5-evidence-20261104-043116.tar.gz`
（sha256 `e9eea646…3d2ab0`）在 home/Source/Downloads 均未找到。按战役指令：
从 #622 的 GitHub issue 归档信息重建方法边界，不假装旧工作区存在，不重做 #622
的工具对比战役。

由此，#622 成果的可用形态 = **issue 正文与评论中的归档叙述**（方法与门禁可继承；
数字不可作为已验证事实引用）。#622 关键方法边界（继承为约束，非数字）：

- 工具裁决 `EXISTING_TOOLS_SUFFICIENT`：不建新 tracing 引擎；最多薄编排层。
- E5（当时内核 **7.2.11**）能力验证：perf/PMU root full-scope、bpftrace 0.24.2、
  BTF、uringscope、DynamoRIO 各有既验能力面；BCC python 工具部分损坏。
- 证据纪律：missing counter ≠ 0；维度不可相加（cycles/instr/CPU-time/latency）；
  采样百分比 ≠ 精确计数；scaled 样本剔除；负控制必须成立。
- 已证机制结论（形态级，非本轮数字）：旧 workload 下 1-enter/op 提交形状、
  tmpfs io-wq 全 punt vs btrfs NOCOW 全 inline 的 FS 条件依赖、uringscope ~2x
  与 DBI ~31x 量级扰动、join 键必须 (ctx,user_data)+序（裸 cookie/req-ptr 会
  66–99% 误配）。
- #622 的 Phase 1 fio 数字（~5–6k instr/IO、futex 因果）已被 Owner 降级
  `UNVERIFIED_EXPLORATORY_ATTRIBUTION`（回执从未归档）。

**内核版本漂移**：本轮 E5 内核 **7.3.3-200.fc44**（#622 验证时为 7.2.11）。
#622 的 tracepoint 字段/工具行为不得直接沿用，probe 模式全部重新实测。

## 3. fio → Adapter → Sluice → Kernel 路径（源码级，本轮冻结）

fio 3.40 调用链（引擎回调）与 Sluice `5d94f23` 内部路径，file:line 均为本轮
会话实测读取：

```text
fio io_u 生成 (io_u.c)
  → adapters/fio/src/queue.cpp:35 sluice_queue()          [取 LIFO 空槽 → scope->submit_*]
    → Sluice request_scope.cpp:46 reserve_slot_           [O(capacity) 线性扫描]
    → async_io_context.cpp:255 submit_read/write          [access_mtx_ 全程持有]
      → uring: uring_transport.cpp:43 dispatch_after_accept
               → submit_transport_locked:218 io_uring_submit  [每次 submit 即 flush ⇒ 结构性 ~1 enter/submit]
               （flags=0：无 SQPOLL；无 FORCE_ASYNC；inline vs io-wq 由内核决定）
        → kernel io_uring_submit_sqe … io-wq / inline
      → CQE: uring_completion.cpp:200 reap_cqes            [peek_batch 批量；每 CQE 重取 dispatch_mtx_]
        → router 查找 uring_transport.cpp:551              [O(router capacity) 线性扫描]
        → request_core.cpp:346 offer_terminal              [published=false，不可见]
  → uring_backend.cpp:631 poll()/poll_progress             [进度所有者驱动 publication]
    → request_core.cpp:462 complete_publication            [published=true ← 用户可见边界]
    → progress_port_.signal()                              [eventfd 写]
  → adapters/fio/src/events.cpp:91 harvest()               [O(capacity) 槽扫描]
    → events.cpp:100 scope->take(ticket)                   [每 ticket O(capacity) resolve_ 线性扫描]
    → staged → events.cpp:254 sluice_event() → fio 完成记账
```

ThreadPool 臂：dispatch → BoundedHandleRing + condvar `notify_one`
（threadpool_backend.cpp:299-332）；worker `pread/pwrite` 后
offer_terminal → publication ring → eventfd signal（:444-464）。等待路径为
ProgressSource eventfd + `poll(2)`（progress_source.hpp:198-203），无 futex
直用；futex 出现在 glibc mutex/condvar 底层。双扫描结构（adapter harvest ×
scope resolve）即 #639 H5 候选的源码形态；1-enter/submit 即 H2 候选形态。

## 4. 研究权限与门禁（冻结）

- #595：io_uring 提交策略唯一 Owner（KEEP_IMMEDIATE）；更少 enter ≠ 产品收益。
- #604：G0_NOT_ADMITTED；fio 负载不自动获得产品优化准入；本轮不回写优化建议。
- #617：DEFERRED（G1 未达）；本轮不架构升级。
- #621：已关闭；不重开 W1/progress 重设计。
- #623：C2O 边界权威；K/R/P/O 锚点语义沿用，不建新观测引擎。
- #639：H1–H6 唯一准入清单；本轮只做归类判定，不改产品。
- 本仓 #3：执行 issue；工具为薄编排层 + clean/profile 分离。
- Sluice 产品源码只读；不修 v2 公共契约；不动 fio Engine 语义。

## 5. G0 结论

`REPOSITORY_REALITY_VERIFIED` — 仓库/二进制/版本三方一致（bench HEAD、
SLUICE_REVISION、Sluice checkout、fio tag）；既有 harness 可复用其
prep/guard/rusage/tripwire 惯用法；#622 证据树缺失已如实记录，方法边界以
issue 归档重建；内核 7.3.3 与 #622 的 7.2.11 存在版本漂移，全部工具能力以
本轮 probe 实测为准（见 `capability.json` 与 MEASUREMENT-WINDOW-CONTRACT）。

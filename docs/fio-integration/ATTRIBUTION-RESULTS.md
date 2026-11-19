# fio-sluice 跨层成本归因结果（A–K 研究报告）

状态：**正式报告**。数据源：`results/attribution/session-001/`（原始回执
+ `sha256-manifest.txt` 独立校验）；复算链：`tools/profile_fio_sluice.py
analyze`。环境判词：`PARTIAL_CPU_ACCOUNTING`（非 root：用户态 PMU 可用；
kernel/eBPF/io-wq 不可用）。方法与预注册：COST-ATTRIBUTION-METHOD.md；
窗口与分母：MEASUREMENT-WINDOW-CONTRACT.md；账本：COST-LEDGER.md。

## A. Repository & Build Reality

RESULTS_PLACEHOLDER_A

## B. Instrumentation Capability

RESULTS_PLACEHOLDER_B

## C. Workload Identity & A/A

RESULTS_PLACEHOLDER_C

## D. Measurement Window & Denominator

RESULTS_PLACEHOLDER_D

## E. User-Space Cost Attribution

RESULTS_PLACEHOLDER_E

## F. Kernel & Scheduler Attribution

RESULTS_PLACEHOLDER_F

## G. Completion Causality

RESULTS_PLACEHOLDER_G

## H. Observer Perturbation

RESULTS_PLACEHOLDER_H

## I. H1–H6 Hotspot Ledger

见 COST-LEDGER.md §4（同一 session 数据，双入口单事实源）。

## J. Independent Review

RESULTS_PLACEHOLDER_J

## K. Final Owner Gate

RESULTS_PLACEHOLDER_K

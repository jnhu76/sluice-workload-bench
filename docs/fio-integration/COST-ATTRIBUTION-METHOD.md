# fio-sluice 成本归因方法（G2 预注册冻结）

状态：**预注册**——本文件在正式测量开始前冻结假设、指标、重复、验收与停止
规则。测量后不得回看调整。方法权威：Sluice #622（E5_NATIVE_ONLY）；C2O 边界
#623；热点清单 #639；产品准入 #604（本轮不触碰）。执行 issue：本仓 #3。

## 1. 研究问题与假设（预注册）

> fio 原生 io_uring 与 fio-sluice io_uring 执行等价 I/O 时，为什么产生不同的
> CPU 成本、内核交互、调度行为与完成观察成本？成本各属 Adapter / Sluice /
> Kernel / 不可辨识哪一层？

预注册假设（均为待检验命题，不预设结论）：

- HA1：sluice-uring 的用户态 instructions/op 高于 native io_uring，且差额
  主要来自 Adapter+Sluice 用户态路径（采样可见），而非 fio driver 本身。
- HA2：sluice-uring 的 io_uring_enter/op ≈ 1（结构性每 submit 即 flush），
  native io_uring（fio 内建引擎自带批量提交）显著 < 1。
- HA3：sluice-TP 的成本主体是跨线程握手（futex/ctx switch/唤醒），其
  instructions/op 与 uring 臂同量级但 CPU 时间/op 显著更高。
- HA4：Adapter harvest 的 O(capacity) 扫描在 D16 产生可测的深度扩展成本
  （D16 与 D1 的每 op 用户态指令差中，与深度成超线性关系的部分）。
- HA5：非 root 环境下 kernel/io-wq CPU 不可测 —— `PARTIAL_CPU_ACCOUNTING`，
  内核侧归因整体 `UNAVAILABLE`（如 root 可用则升级，另行声明）。

## 2. Primary Cells（冻结）

| ID | 负载 | depth | 目的 |
| --- | --- | ---: | --- |
| A1 | 4 KiB randread，warm buffered | 1 | 单请求固定开销 |
| A2 | 4 KiB randread，warm buffered | 16 | 排队/进度/完成开销（H5 扫描深度敏感性） |
| A3 | 64 KiB seqread，warm buffered | 4 | 大块对照 |
| A4 | 4 KiB randread，warm buffered | 4 | 深度过渡（A1→A2 中点） |

每 cell × 引擎 {`io_uring`, `sluice-uring`, `sluice-threadpool`}；
D1 参照臂 `psync`（同步引擎，fio 钳 D1；只作同步成本参照，不进异步排名）。
**F3（写混合）不进入本轮任何排名**（SATA SLC 环境混杂，PERF-BASELINE §3）。

## 3. 身份冻结（Required Identity）

沿用 `run_fio_matrix.py` 惯用法：fio fio-3.40（ABI FIO_IOOPS_VERSION 37）、
randseed=81（randrepeat=1）、numjobs=1、direct=0、invalidate=0、
buffer_pattern=0x5a、thread=1、group_reporting、每 case 专用 1G 文件
（psync 顺序写 + fsync_on_close + `chattr +C` + 全量预热读）、
`taskset -c 0-9`（每物理核一超线程）、btrfs(compress=zstd:1) sda3 热缓存。
Sluice SDK `5d94f23…`；引擎 .so sha256 随 binary-manifest 归档；
TP workers=4（引擎默认）。configured iodepth ≠ achieved depth：以
`iodepth_level` 直方图/mean_depth 记录实际深度。各引擎间同 case 为同分布
形状、同 offset 序列（randseed=81）——**fio 内建引擎与 external 引擎重放
同一 randseed 走查**，满足"同一 fio driver 下匹配对照"。

## 4. 模式与顺序（串行，占用实验 CPU 的步骤互斥）

每 session 依次：`probe`（能力）→ `clean`（每 cell×引擎×repeat，无任何
插桩）→ `pmu`（perf stat 计数；每 cell×引擎×repeat）→ `record`（perf record
采样，符号版引擎，DIAG 构建）→ `trace`（strace -c 短窗受控计数）。
模式间不复用；任何两个被测 run 不并行。每 run 记录：mode、cell、engine、
repeat、起止时间、引擎 load 行、exit code、rusage、fio JSON 原件。

## 5. 预注册验收与停止规则

1. **重复数**：clean ×3 repeats/cell/engine；pmu ×2；record ×1；trace ×1
   （短窗 3s/256M 专用文件，仅为 syscall 形态计数）。
2. **A/A 验收**：沿用 PERF-BASELINE §2 的 OBSERVED_TWO_REPEAT_SPREAD 语义
   （非噪声底）；本轮 clean 3 repeats 的极差若 >15%（IOPS）标记该 cell
   `HIGH_SPREAD`，只作定性解读。
3. **环境漂移接受域**：governor 集合不变； Package 温度 <80°C；
   run 前 1min loadavg <1.0；无其他 fio/perf 进程。超域暂停等待，不删数据。
4. **异常值**：tripwire 诊断、exit≠0、JSON 不可解析 ⇒ 该 run 作废并补跑一次，
   作废记录保留在 manifest（`discarded: true`），不静默删除。
5. **负控制**：psync D1 臂兼作"无异步机制"对照；strace 模式自带
   bare-vs-straced 同负载对照量化扰动（≥1 组/引擎）。
6. **停止条件**：同一 cell×engine 连续 3 次 run 作废 ⇒ 停止该臂并记录
   `ARM_BLOCKED`；任何引擎 load 行 backend 与请求不符 ⇒ 立即停止全 session。
7. **分母规则**：见 MEASUREMENT-WINDOW-CONTRACT.md（Option B 主 +
   Option C 交叉核验）；per-op 计算仅在分母与计数窗口一致时输出，否则拒绝。
8. **不下结论条件**（任一成立则相关结论只能 `INCONCLUSIVE`）：
   Option B/C 差 >10% 且无解释；主事件组 time-running <100% 且无法重排；
   strace 扰动使 enter/op 形态本身改变（straced IOPS < bare 的 50%）；
   A/A 极差 > 引擎间差距的 1/3。
9. **禁止事项**：不因结果调整本节任何阈值；采样占比不进 per-op 表；
   kernel 指标不以 0 冒充；不同 Sluice SHA 不混 session。

## 6. 交付物

`tools/profile_fio_sluice.py`（probe/clean/pmu/record/trace/analyze 六模式，
薄编排层）+ `tools/attribution/*` 模块 + `tools/attribution/tests/`
（12 项强制用例）；session 证据树 `results/attribution/<session>/`
（git-ignored，含 PROTOCOL/environment/capability/binary-manifest/
fio 原件/perf 原件/strace 原件/window-contract/normalized-costs/analysis/
sha256-manifest）；报告 `docs/fio-integration/ATTRIBUTION-RESULTS.md`、
`COST-LEDGER.md`（仓内，含小型计数回执与原始文件 SHA）。

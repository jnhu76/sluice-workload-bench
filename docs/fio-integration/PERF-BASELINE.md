# E5 性能基线：F1–F4 × 四引擎（Phase 5）

状态：**正式报告** —— 串行正式矩阵（`tools/run_fio_matrix.py`，mode=formal，
wall 609.4s）与对比（`tools/compare_fio.py`）均已执行；50 个 fio JSON 原件
与 manifest 数字交叉核对一致（`results/fio/compare.json` `crosschecks`）。
数据表在下方标记区由 compare_fio.py 生成；本文其余部分为测量员的解读与
资格声明，§1–§4 与数据表的全部数字可回指到 `results/fio/` 原件；§5
Attribution 除外——其计数回执未归档（UNVERIFIED_EXPLORATORY_ATTRIBUTION，
见 §5 标注）。

- 生成链：`run_fio_matrix.py`（A/A 对 + F1/F2/F4/F3 × psync/io_uring/
  sluice-threadpool/sluice-uring × 2 repeats，串行，fio JSON 原件落
  `results/fio/*.jsonl` + `manifest.json`）→ `compare_fio.py`
  （汇总 + envelope 对照 → 本文件数据表 + `results/fio/compare.json`）。
- 负载形态：A/A（F2 形态同配置重复）+ F1 randread 4KiB D1 /
  F2 randread 4KiB D16 / F3 randrw 70/30 4KiB D16 / F4 seqread 64KiB D4；
  numjobs=1、direct=0、randseed=81（fio 默认 randrepeat=1，同一 case 形态
  下各引擎重放同一偏移序列）、time_based runtime=10s + ramp 2s。
- 方法论控制（预演阶段以实验确定，回执见 git 历史与本文）：`invalidate=0`
  （fio 默认会在每个 job 启动时丢弃目标文件页缓存，实测把热缓存基线偷换成
  冷存储基线）；每 case 专用 1G 文件 = psync 顺序写 + `fsync_on_close` +
  一次全量预热读（页缓存常驻，各引擎起点一致）；文件 `chattr +C`
  （NODATACOW，规避 compress=zstd:1 的解压-改-再压路径）；统一
  `buffer_pattern=0x5a`；F3 每个 run 后 fsync settle + diskstats io_ms
  静默 4s 的确定性间歇；测试文件延到矩阵结束统一删除（async TRIM 风暴）。
- 硬约束重申：所有 I/O 只落在 `build/scratch` 下的临时 regular file；
  不以单次 IOPS 高低下产品结论；不可比配置不混排。

## 1. 环境资格（现场复核 2026-11-19）

原始数据：`results/fio/manifest.json` 的 `environment` 节（矩阵运行时现场
采集）；预检命令与读数在测量员会话内执行。

| 项 | 读数 | 判定 |
| --- | --- | --- |
| 平台 | 裸机 Xeon E5-2666 v3，10C/20T，SMT 对 0-9/10-19，单 NUMA 节点；内核 7.3.3-200.fc44 | 资格成立 |
| governor/Turbo | intel_cpufreq（intel_pstate passive）+ schedutil×20；no_turbo=0；1.2–3.5GHz；空闲 cpu0 稳定 1.2GHz，采样见单核 3.5GHz | 未钉 performance（sudo 需密码），记录为环境限制；沿用 e5-fullpath-machine-work 结论：≥4 核满载钳 3.2GHz all-core 桶 |
| 亲和 | 全部 fio 进程及引擎 worker 经 `taskset -c 0-9` 继承同一掩码（每物理核一个超线程，无 SMT 同核竞争） | 已控制 |
| 温度 | Package 46°C / cores 39–41°C（crit 93°C）；thermal_zone0=39°C | 无降频迹象 |
| 文件系统 | `/` 与 `/home` 均为 /dev/sda3 btrfs（compress=zstd:1, ssd, discard=async, space_cache=v2）；/tmp tmpfs(32G)。按任务书选择 build/scratch 位于 btrfs | 记录：真实 FS 路径，非 tmpfs |
| 存储底座 | sda = GS-480 447G SATA SSD（rotational=0, zoned=none）。**实测持续写在 SLC 缓存耗尽后跌至 ~10–20MB/s**（诊断实验：同一文件三连写 1004MiB/s→13MiB/s→3.7MiB/s） | 环境限制：F3 写混场景受此支配（见 §3） |
| 内存 | 62G（buff/cache ~30G），1G 测试文件页缓存常驻 | 已利用（热缓存基线） |
| PMU | perf 7.3.3 可用，perf_event_paranoid=2 → **仅用户态计数**，内核态计数为环境限制；strace/taskset/chattr 可用 | Attribution 以用户态 + syscall 混合完成（§5）；§5 计数回执未归档，UNVERIFIED_EXPLORATORY_ATTRIBUTION |
| sudo | `sudo -n true` 失败（需密码）→ governor 钉住、turbostat、bpftrace 不可用 | 环境限制，未假装控制 |
| 工具链 | fio fio-3.40（与引擎 ABI 一致）；四产物齐备；引擎 load 行 revision=5d94f23… = SLUICE_REVISION；native io_uring 与 sluice-uring 探针均可用（本次无 UNAVAILABLE 行） | 资格成立 |

## 2. A/A 一致性

数字见下方数据表 A/A 节（F2 形态、10s 窗口、同配置重复两次）。

- **psync（native）**：788149 vs 775290 IOPS，Δ1.65%；p99 1.304/1.320µs，
  Δ1.22%；CPU 秒 Δ0.035%。该值为 psync 的 OBSERVED_TWO_REPEAT_SPREAD
  （两次重复的极差，非噪声底、非最小可检测效应、非 95% 区间）。
- **sluice-threadpool**：220629 vs 202952 IOPS，Δ8.35%；p99 Δ6.76%；
  CPU 秒 Δ0.81%。该值为 sluice-TP 的 OBSERVED_TWO_REPEAT_SPREAD（两次
  重复极差）；方差方向与引擎侧（futex 挥手的调度抖动）相容，但两次
  样本不足以将其确立为整链路噪声底。
- 结论（OBSERVED_TWO_REPEAT_SPREAD，INSUFFICIENT_FOR_GENERAL_NOISE_THRESHOLD）：
  以上仅是 psync 与 sluice-TP 各两次重复的观测极差（1.6%/8.35%），不是
  native io_uring 或 sluice-uring 的噪声底（二者无 A/A 对；F1 io_uring
  rep spread 11.0% 即超出 8%），不是最小可检测效应，不是 95% 置信区间，
  不构成普适 <5%/<10% 判读规则。判读以两 repeats 均值 + 逐 rep 值
  （compare.json `matrix.*.iops_total_per_rep`）逐 case 呈现，引擎间差距
  远大于观测极差的 case 才作解读。

## 3. F1–F4 × 四引擎对照

数字见下方数据表；每格为两 repeats 均值，`rep spread%` 为逐 rep 极差。
psync 为同步引擎：D16/D4 配置被 fio 钳到 D1（stdout note 自动捕获，
`mean depth` 列显示 `1 (capped)`）——这是 fio 引擎语义，如实记录而非
缺陷；其 achieved depth 即 1。

**读为主的 case（F1/F2/F4）——EXPLORATORY_F1_F2_F4（探索性对照基线，有效部分）：**

- F1（4k randread D1，热缓存）：psync 785.8k > io_uring 616.0k >
  sluice-uring 422.5k > sluice-TP 39.2k IOPS。sluice-uring 达到 native
  io_uring 的 0.69×；sluice-TP 在 D1 被 futex 挥手支配（机制表述；p99 27.8µs
  与 ctx/kiop 2445 为已归档原件，握手成本的定量依据见 §5——
  UNVERIFIED_EXPLORATORY_ATTRIBUTION）。
- F2（4k randread D16）：psync(D1-capped) 789.5k ≈ 其 D1 水平；
  io_uring 618.9k（achieved depth 16）；sluice-uring 368.0k（0.59×
  native，p99 53.8µs）；sluice-TP 211.6k（p99 149.5µs；CPU 41.4s，
  为其他引擎的 3.4×——4 个 worker 自旋 + 挥手）。
- F4（64k seqread D4）：**sluice-TP 211.0k ops/s（13.8GB/s）为四者最高**
  ——大块 + D4 流水线下 TP 的并行预读生效；psync(D1-capped) 158.1k；
  io_uring 126.9k；sluice-uring 117.3k（0.92× native io_uring）。
- 引擎间关系稳定：F1/F2/F4 的两 rep spread 除 F1/io_uring（11.0%）与
  F2/sluice-uring（6.7%）外均 ≤3.7%，远小于引擎间差距。

**F3（4k randrw 70/30 D16）——F3_ENVIRONMENT_CONFOUNDED（SATA SSD SLC 缓存耗尽环境混杂），不支持跨引擎排名：**

- 正式跑的 F3 落在 SSD SLC 耗尽区：F3_prep 本身耗时 40.6s（1G 写入仅
  ~25MB/s，此前 5×1G prep 已累积 ~4.5G 写）；psync 4.6k 总 IOPS
  （两次 rep 一致地慢，wall 20–24s）；io_uring 两次 rep 699 vs 11267
  （spread 177%，rep1 wall 27.3s）；sluice-TP 1282 vs 54468
  （spread 191%）；**只有 sluice-uring 两 rep 稳定（97.1k/99.0k，
  spread 1.9%）**。
- 判读：F3 的数字刻画的是"btrfs + SLC 耗尽 SATA SSD 的缓冲写路径"，
  不是引擎差异；除 sluice-uring 的稳定性本身是一个可报告观察外，
  **本 run 的 F3 不支持任何跨引擎强弱结论**。预演（SSD 较新鲜时）
  异步引擎可达 90–160k 总 IOPS，进一步证明该场景随盘状态漂移。
- 该限制已按方法论预先声明（run_fio_matrix.py CASES 注释与
  compare_fio.py 的逐 rep 呈现），非事后修饰。

## 4. envelope 对照（探索性，非严格 A/B）

数字见下方数据表 envelope 节（sluice-workload-bench：posix/direct/
threadpool 用 noring 产物，uring 用 ring 产物；256M working set，
seed 81；blocking 后端仅 D1，D>1 的格子按构造跳过）。

可比性边界（任务书 §8.3 轴；该节号在两仓文档未找到原文，轴内容取自
任务书正文，已在生成文档中声明）：

- **相同 arrival trace：否**。fio 用自身 randseed=81 偏移走查驱动
  time_based 闭环；bench 用自己的 PRNG 从 seed=81 生成请求计数闭环的
  独立确定性 trace。分布形状相同（尺寸、读写比、工作集跨度、目标深度），
  trace 实例不同。
- **achieved concurrency**：fio 侧 = iodepth_level 直方图 / mean_depth；
  bench 侧 = peak_inflight_requests；blocking 后端恒为 1。
- **driver overhead**：fio 在被测进程内背负完整 job 机器（io_u 管理、
  延迟直方图、偏移生成）；bench 是直连后端的最小进程内 driver。跨族
  差值因此混合 driver 开销与后端差异。
- **文件与缓存状态**：fio 每 case 共享预建 1G 文件（seqwrite +
  fsync_on_close + 全量预热读，time_based 跨引擎/rep 复用）；bench 每次
  调用新建 mkstemp fixture（pwrite + fdatasync 预热，末尾字节 oracle
  校验）；bench 'direct' 是同步 sluice::File 而非 O_DIRECT，全后端皆缓冲。
- **明确声明：envelope 行是探索性对照，不用于严格 A/B 裁决。**

尽管如此，envelope 给出了两个有价值的一致性观察（数字在下方表格）：

1. **TP 与 uring 后端的吞吐在不同 driver 下相互印证**：F1 D1 处
   bench-TP 39.9k vs fio-sluice-TP 39.2k（差 1.8%）；bench-uring 426.9k
   vs fio-sluice-uring 422.5k（差 1.0%）；F4 处 uring 122.7k vs 117.3k
   （差 4.6%）。Sluice 后端本身的吞吐与 driver 无关地复现。
2. **driver 开销差可见且随 case 变化**：F1 D1 处 bench-posix 1014.8k >
   fio-psync 785.8k（fio job 机器在同速率下多耗 ~23%）；F2 D16 处差距
   更大（bench-uring 535.0k vs fio-sluice-uring 368.0k，还叠加了
   256M vs 1G 工作集差异）。这正是禁止跨族直接 A/B 的原因。

## 5. Attribution（driver / adapter / Sluice / kernel / worker）

**UNVERIFIED_EXPLORATORY_ATTRIBUTION**——本节数字来自前一测量会话的未归档
统计回执；本轮无独立原始计数复核；不作为产品性能裁决依据；Adapter+Sluice
为组合开销，不能独立推断 Adapter 净开销；strace 未见额外 syscall 不代表
不存在用户态开销。

采样方法（测量员会话内串行，不进正式门）：`perf stat -x, -e task-clock,
instructions,cycles,context-switches,cpu-migrations`（用户态，
paranoid=2 限制）与 `strace -c -f`，各引擎 3s D16 randread 4k 热缓存
短跑（64M +C 文件，taskset 0-9）。原始输出在测量员会话记录；
正式矩阵侧旁证 = manifest 的 rusage（CPU 秒、ctx 切换）与 fio
usr/sys CPU。目的仅是发现适配层的严重阻塞、重复遍历或拷贝。
（注：instructions/IO 的归一化分母——计数窗口内的完成 IO 数——亦未随回执
归档。）

- **无隐藏 POSIX fallback**：sluice-uring 的 strace 仅 io_uring_enter
  （61.4k 次 ≈ 1 次/IO，无 pread64）；sluice-TP 仅 pread64（74.0k ≈
  1 次/IO）。各引擎 syscall 数 ≈1/IO，**未见重复遍历或拷贝的 syscall
  特征**。
- **每 IO 用户态指令（UNVERIFIED_EXPLORATORY_ATTRIBUTION，数字保留原读数）**：
  native psync ~2.4k、native io_uring ~2.9k；sluice-uring ~7.9k、
  sluice-TP ~8.9k → **adapter + Sluice 组合层约多 ~5–6k 用户态指令/IO
  （约 3× native driver 成本；组合开销，不能独立推断 Adapter 净开销）**。
  sluice-uring 在 CPU 受限的热缓存读上达到 native io_uring 的
  0.59–0.69×（此吞吐比来自已归档矩阵原件），与该未复核指令差距方向
  相容——解释为'真实机器工作而非阻塞'是假设，需归档计数复核后方可确立。
- **TP 的成本在挥手不在指令（短跑计数为 UNVERIFIED_EXPLORATORY_ATTRIBUTION；
  正式矩阵旁证来自已归档 manifest rusage）**：sluice-TP 每指份数与 native
  相当，但 task-clock(u) 10.3s vs 3.2s（3.2×）、IPC 0.89 vs ~1.9；strace
  每 IO ~3.4 次 futex（251k futex / 74k pread，8.2s 累计耗在 futex）
  （未归档回执）。正式矩阵旁证（已归档，manifest rusage）：F2 处 TP CPU
  41.4s vs 其他引擎 12.2s；ctx/kiop 1308–5999 vs native/uring 的
  0.17–1.2（10³ 倍量级）。D1 时该挥手完全串行化（F1 仅 39.2k IOPS）；
  D4 大块时流水化后反超（F4 最高）。
- **kernel 侧计数不可得**（perf_event_paranoid=2），内核/适配边界只能
  用 syscall 计数 + 用户态指令 + rusage 旁证，无法做内核态周期分解——
  记录为环境限制。
- 结论（探索性，UNVERIFIED_EXPLORATORY_ATTRIBUTION）：在未归档回执所示
  采样内未观察到严重阻塞、重复遍历或缓冲拷贝特征（strace 未见额外
  syscall 不代表不存在用户态开销）；主要开销 (a) 适配+Sluice 组合每多
  ~5–6k 用户态指令/IO（未复核），(b) TP 后端的 futex 挥手（Sluice
  产品侧属性，非 fio adapter 引入；此归因方向有已归档 rusage/ctx 旁证）。
  不作为产品性能裁决依据。

## 6. fio JSON 原件索引

全部在 `results/fio/`（该目录在 .gitignore 内，为工作区证据）：

- 测量 run：`<case>_<engine>_rep<N>.jsonl`（16 个，case ∈ F1/F2/F4/F3，
  engine ∈ psync/io_uring/sluice-threadpool/sluice-uring，N ∈ 1,2）；
  A/A：`aa-<engine>_rep<N>.jsonl`（4 个）。
- 准备 run：`<case>_prep.jsonl`、`<case>_warm.jsonl`、`aa-*_prep/warm`
  （写入与预热读原件）；探针：`probe-io_uring.jsonl`、
  `probe-sluice-uring.jsonl`。合计 50 个 JSON 原件。
- 每个 run 的 job 文件在 `results/fio/jobs/<run_id>.fio`（50 个）。
- 汇总：`results/fio/manifest.json`（每 run 的指标、rusage、engine load
  行、失败/跳过原因、环境快照）与 `results/fio/compare.json`
  （A/A 差异、四引擎聚合 + 逐 rep、envelope、可比性边界、交叉核对）。
- 本阶段无 UNAVAILABLE 行（两个 uring 探针均可用）；引擎 load 行
  backend 与请求一致（run_fio_matrix.py 强校验，防 hidden fallback）。
<!-- PERF-DATA:BEGIN (auto-generated by tools/compare_fio.py; do not edit inside the markers) -->
### 自动生成数据表（compare_fio.py；正式矩阵原件见 results/fio/*.jsonl）

模式：`formal`；fio `fio-3.40`；Sluice revision `5d94f233967c3aef993eb19ba9e3252c532b0951`；randseed `81`；numjobs=1；direct=0；runtime=10s(+ramp 2s)；cpu_set=0-9。

#### A/A（同配置重复两次，F2 形态；仅 psync 与 sluice-threadpool——OBSERVED_TWO_REPEAT_SPREAD，INSUFFICIENT_FOR_GENERAL_NOISE_THRESHOLD：非 io_uring/sluice-uring 噪声底，非最小可检测效应，非 95% 区间，非普适 5%/10% 规则）

| engine | rep1 IOPS | rep2 IOPS | ΔIOPS% | rep1 p99µs | rep2 p99µs | Δp99% | Δmean-depth% |
|---|---:|---:|---:|---:|---:|---:|---:|
| psync | 788149.2 | 775289.9 | 1.645 | 1.304 | 1.32 | 1.22 | 0 |
| sluice-threadpool | 220629.137 | 202952.305 | 8.346 | 146.432 | 156.672 | 6.757 | 0 |

#### F1–F4 × 四引擎（EXPLORATORY_F1_F2_F4 探索性对照；F3_ENVIRONMENT_CONFOUNDED：F3 受 SATA SSD SLC 耗尽支配，不支持跨引擎排名。两 repeats 均值；读方向延迟；IOPS 逐 rep 见 compare.json）

| case | engine | status | IOPS r/w/total | rep spread% | BW MiB/s r/w | p50µs | p95µs | p99µs | mean depth | CPU s | ctx/kiop |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| F1 | io_uring | ok | 615964.215 / - / 615964.215 | 11 | 2406.111 / - | 0.108 | 0.115 | 0.122 | 1 | 12.171 | 0.217 |
| F1 | psync | ok | 785824.85 / - / 785824.85 | 0.8 | 3069.628 / - | 0.976 | 1.208 | 1.304 | 1 | 12.172 | 0.174 |
| F1 | sluice-threadpool | ok | 39211.878 / - / 39211.878 | 3.6 | 153.171 / - | 21.376 | 22.912 | 27.776 | 1 | 14.108 | 2444.814 |
| F1 | sluice-uring | ok | 422478.35 / - / 422478.35 | 0.2 | 1650.306 / - | 0.524 | 0.56 | 0.576 | 1 | 12.172 | 0.323 |
| F2 | io_uring | ok | 618908.86 / - / 618908.86 | 1.1 | 2417.619 / - | 23.936 | 25.728 | 34.368 | 16 | 12.172 | 0.222 |
| F2 | psync | ok | 789472.55 / - / 789472.55 | 0.5 | 3083.877 / - | 0.972 | 1.208 | 1.296 | 1 (capped) | 12.179 | 0.173 |
| F2 | sluice-threadpool | ok | 211591.291 / - / 211591.291 | 1.2 | 826.534 / - | 65.664 | 128.512 | 149.504 | 16 | 41.372 | 1307.644 |
| F2 | sluice-uring | ok | 368040.796 / - / 368040.796 | 6.7 | 1437.665 / - | 40.96 | 45.312 | 53.76 | 16 | 12.164 | 0.367 |
| F3 | io_uring | ok | 4191.752 / 1790.826 / 5982.578 | 176.7 | 16.377 / 6.997 | 63.744 | 90.624 | 103.936 | 16 | 1.361 | 211.764 |
| F3 | psync | ok | 3192.506 / 1359.461 / 4551.967 | 1.5 | 12.471 / 5.31 | 2.368 | 2.832 | 3.856 | 1 (capped) | 0.708 | 53.696 |
| F3 | sluice-threadpool | ok | 19522.064 / 8352.655 / 27874.719 | 190.8 | 76.261 / 32.631 | 105.984 | 205.824 | 260.608 | 16 | 8.21 | 5998.742 |
| F3 | sluice-uring | ok | 68681.144 / 29390.133 / 98071.277 | 1.9 | 268.288 / 114.808 | 61.696 | 79.872 | 95.744 | 16 | 6.632 | 1.861 |
| F4 | io_uring | ok | 126867.864 / - / 126867.864 | 0.3 | 7929.266 / - | 23.424 | 24.32 | 34.304 | 4 | 12.181 | 1.078 |
| F4 | psync | ok | 158062.644 / - / 158062.644 | 0.2 | 9878.915 / - | 6.08 | 6.496 | 7.232 | 1 (capped) | 12.189 | 0.863 |
| F4 | sluice-threadpool | ok | 210990.351 / - / 210990.351 | 0.3 | 13186.916 / - | 17.024 | 23.04 | 27.008 | 4 | 46.565 | 1561.433 |
| F4 | sluice-uring | ok | 117309.519 / - / 117309.519 | 3.7 | 7331.864 / - | 25.856 | 26.88 | 34.88 | 4 | 12.184 | 1.171 |

#### envelope 对照（sluice-workload-bench，探索性非严格 A/B）

| case | backend | status | IOPS | MiB/s | 读 p99µs | peak inflight | CPU ns/req |
|---|---|---|---:|---:|---:|---:|---:|
| F1 | posix | ok | 1014773.38 | 3963.959 | 1.346 | 1 | 982.067 |
| F1 | direct | ok | 781933.63 | 3054.428 | 2.45 | 1 | 1273.473 |
| F1 | threadpool | ok | 39914.142 | 155.915 | 31.763 | 1 | 29141.003 |
| F1 | uring | ok | 426916.636 | 1667.643 | 4.891 | 1 | 2333.51 |
| F2 | posix | skipped | - | - | - | - | - |
| F2 | direct | skipped | - | - | - | - | - |
| F2 | threadpool | ok | 272452.809 | 1064.269 | 104.872 | 16 | 13022.323 |
| F2 | uring | ok | 535038.588 | 2089.994 | 35.531 | 16 | 1862.966 |
| F3 | posix | skipped | - | - | - | - | - |
| F3 | direct | skipped | - | - | - | - | - |
| F3 | threadpool | ok | 16787.406 | 65.576 | 137.751 | 16 | 18092.628 |
| F3 | uring | ok | 7665.475 | 29.943 | 78.631 | 16 | 6609.399 |
| F4 | posix | skipped | - | - | - | - | - |
| F4 | direct | skipped | - | - | - | - | - |
| F4 | threadpool | ok | 153532.428 | 9595.777 | 53.439 | 4 | 25032.585 |
| F4 | uring | ok | 122705.481 | 7669.093 | 39.101 | 4 | 8124.035 |

envelope 可比性边界（按任务书 §8.3 轴标注；该节号在两仓文档中未找到原文，轴内容取自任务书正文）：

- **same_arrival_trace**: False — fio drives time_based closed loops with its own randseed=81 offset walk; the bench generates a separate deterministic trace from its own seed=81 PRNG over a request-count closed loop. Same distributional shape (sizes, r/w ratio, working-set span, target depth), different trace instance.
- **achieved_concurrency**: fio=iodepth_level histogram / mean_depth per run (manifest metrics)；bench=peak_inflight_requests per run; blocking backends (posix/direct) are depth-1 by construction
- **driver_overhead**: fio=full fio job machinery (io_u management, latency histograms, offset generation) in the measured process；bench=minimal in-process driver calling the backend directly; cross-family deltas therefore mix driver overhead with backend differences
- **file_and_cache_state**: fio=shared pre-created 1G per-case file: psync seqwrite + fsync_on_close (clean), then one recorded full sequential warm-up read so every engine starts with the file page-cache resident (btrfs compress=zstd cold reads would otherwise dominate and order runs by cache warmth); time_based reuses the same file across engines and repeats; direct=0；bench=fresh mkstemp fixture per invocation, pwrite-prepared + fdatasync (warm), full byte oracle verify; all backends buffered (bench 'direct' is synchronous sluice::File, not O_DIRECT); working set 256 MiB
- 明确声明：以上 envelope 行是探索性对照，不用于严格的 A/B 裁决。

交叉核对：全部 fio JSON 原件与 manifest 数字一致（50 runs）。

<!-- PERF-DATA:END -->

# fio-sluice 确定性正确性矩阵（Phase 5）

- 工具：`python3 tools/check_fio_engine.py --scratch-dir build/scratch --output results/fio/correctness.json [--require-uring]`
- 裁决：exit 0 = 全部通过；exit 1 = 任何失败（含环境门禁失败）；`--require-uring` 且
  io_uring 不可用 → 打印 `UNAVAILABLE: <原因>` 并 exit 77，绝不回退 threadpool 臂。
- 实测记录（本分支，2026-11-19，fio-3.40 @ /usr/sbin/fio，内核 7.3.3-200.fc44，
  引擎 revision `5d94f23…`（= 仓库 `SLUICE_REVISION`，逐 run 对账））：
  - 默认调用：**90 passed / 0 failed / 0 skipped，exit 0**；
  - `--require-uring`：**90 passed / 0 failed / 0 skipped，exit 0**（uring 可用）；
  - 隐藏 ring 引擎 + `--require-uring` → `UNAVAILABLE: ring engine not built …`，**exit 77**；
  - 隐藏 ring 引擎（无标志）→ threadpool 臂 35 通过 + `uring-arm` 记 1 skipped，exit 0。
  JSON 证据（逐 case 的 rwts 记账、err、引擎版本行、stdout/stderr 尾部）落
  `results/fio/*.json`（git-ignored）。

## 1. 语义矩阵覆盖

job 文件由脚本内嵌模板生成（与 `workloads/fio/smoke-*.fio` 同款占位契约，
`ioengine=external:<绝对路径>`；串行执行；数据目标全部是 `build/scratch/data/`
下的专用临时 regular file，脚本带 filename 守卫拒绝 scratch 之外的任何目标，
用完即删）。覆盖（backend × 引擎）：

| 组 | case 集 | threadpool/noring | threadpool/ring | uring/ring |
| --- | --- | --- | --- | --- |
| read | 4KiB D1/4/16、64KiB D1/4（prefill + 读校验） | ✔ | ✔ | ✔ |
| write | 4KiB D1/4/16（write+do_verify） | ✔ | ✔ | ✔ |
| write-pattern | verify=pattern 4k D4（脚本逐字节核对） | ✔ | ✔ | ✔ |
| mixed | 70/30 randrw D4/16 + 全量 audit 读 | ✔ | ✔ | ✔ |
| mixed-size | bsrange 4k-64k D4/16（randwrite+do_verify） | ✔ | ✔ | ✔ |
| sparse | thinktime=2ms D1/4（randread+verify） | ✔ | ✔ | ✔ |
| eof | 73 块+2048B 非对齐文件，D4 | ✔ | ✔ | ✔ |
| sync | fdatasync=8，D2 | ✔ | ✔ | ✔ |
| error | §3 负控制 + D1024 无溢出臂 | ✔ | ✔ | ✔ |
| cleanup | 短 runtime + SIGTERM，D16 | ✔ | ✔ | ✔ |

## 2. 数据 oracle（双层）

1. **fio 内置 verify=crc32c**（read-after-write / do_verify / audit 读；任务书写
   "如 crc32c"，fio 3.40 posval 确认存在，options.c:3164）。混合负载的合法结果
   集先冻结：预填（write+verify）→ 70/30 randrw（fio 位图只校验本 job 写过的
   块）→ 全量线性 audit 读——audit 恰好接受"任意合法交错写"这一集合，不把
   未定义竞争当预期。并发重叠写之间不逐字节断言（合法集合无法穷举），由
   audit 全量兜底。
2. **脚本独立 POSIX 校验**：只读类 case 前后 sha256 + size 不变；write 类
   size 精确；verify=pattern 类对全文件逐字节比对（预期 = 每 4k 块内 pattern
   相位重排，实测冻结）；不可算术推导的记账（write+verify 的 short 计数、
   70/30 分割、bsrange 计数）用 **fio 原生 sync 引擎同形状校准**，引擎必须与
   原生参考逐字段一致（parity oracle）。
   - 校验非空转已实证：人为翻转 1 字节后 audit 读 exit 1 并给出
     `crc32: verify failed at file … offset 98304`（探测记录）；工具级同样实证
     （case 失败 + verify 行入 JSON + 整体 exit 1）。

## 3. 负控制（任务 §10 逐条 → case → 证据）

| 要求 | case | 证据（实测） |
| --- | --- | --- |
| 非法 backend 参数被拒 | `invalid-backend` | exit 1 + `invalid sluice_backend "nosuchbackend"` |
| 无隐藏回退 | `noring-rejects-uring` | exit 1 + `io_uring unavailable: engine built from the noring SDK profile` |
| 引擎侧参数失败关闭 | `uring-workers-rejected` / `verify-async-rejected` / `workers-zero-rejected` / `iodepth-zero-rejected` / `offload-rejected` | 全部 exit 1，带对应拒绝信息（后两者为 fio 解析层拒绝：fio 自身 minval 先于引擎 init，已如实归类） |
| iodepth 超容量无 silent overflow | `depth1024-*`（capacity≡iodepth） | D1024 读 4096k：issued total=(1024,0,0,0)、short=0、dropped=0、exit 0 |
| 请求失败不丢结果 | `trim-rejected` | 引擎对 DDIR_TRIM 以 EINVAL 走完成路径交回：exit 1、err=22、rwts trim 列=1、`ddir 2 is not supported` |
| sync/trim 不被当成功 | `trim-rejected` + `fdatasync-*` | trim 被拒（非静默成功）；fdatasync=8 出现独立 `fsync/fdatasync/sync_file_range:` 计量段（sync 记为 sync，非数据 io） |
| finite timeout 不永久阻塞 | 每个 case 的 subprocess 看门狗 | 默认 90s（D1024 臂 180s），超时强杀并计失败（本次运行零触发） |
| 退出时已接受 Request 均结算 | 全部 case 的引擎诊断扫描 | `staged completions were never consumed` / `still outstanding` 等清零；cleanup 臂额外断言 |
| fd 未提前关闭 / buffer 未提前复用 | 全部 case 的 tripwire 扫描 + D16 双层 oracle | `closing fd … in-flight` / `fd … never registered` / `slot accounting bug` / `lost accepted request` / `request failed: code=` 等引擎自检线零出现；D16 下 crc32c + pattern 字节级通过（buffer 提前复用必致错） |
| 验证失败 → 整体 exit 1 | 工具逻辑 + 人为损坏实证 | 任何 verify 行 → 该 case 失败 → 汇总 failed≥1 → exit 1 |

## 4. 与任务书信件的偏差（如实声明，均有探测依据）

1. **`timeout_threshold` 在 fio 3.40 不存在**（上游 options.c 全树 grep 为空；
   `timeout` 仅是 `runtime` 的别名）。cleanup-with-inflight 改用等价确定性 seam：
   `time_based runtime=30` + 在 `/proc/<pid>/io` 观察到 ≥128KiB 真实 I/O 后发
   SIGTERM（条件触发，非随机 sleep）。实测：三后端均在 ≤236ms 内干净退出
   （exit 128，`fio: terminating on signal 15`），无 outstanding 诊断，scratch
   文件可删。
2. **fio 在 regular file 上永不发出跨 EOF 的读**（io 范围取整到已布局文件内的
   整块；实测 `filesize<size` 会被 fio 扩容或拒绝）。因此"短 I/O"以两个确定性
   seam 承接：EOF 边界 case（非对齐文件，floor 计数 + 位不变 + 零 short）与
   trim EINVAL（内核级请求失败交付）。内核短读经由 fio 生成器不可达，不在本
   矩阵中伪造。
3. **mixed-size 无法跨 job audit**：固定 randseed 下 fio 的 randread 也不复现
   randwrite 的 (offset,len) 序列（实测 `bad magic header`）。故 mixed-size 的
   数据 oracle 用同 job `do_verify` 回放（fio 原生机制，恰好重放自己写过的
   io），并如实放弃跨 job audit。
4. fio 对 `iodepth=0` / `sluice_workers=0` 的拒绝发生在解析层（引擎的
   `iodepth must be nonzero` 分支经 fio 不可达）——拒绝语义仍被断言，归属层
   如实记录。

## 5. 复跑

```
xmake build -y fio-sluice-noring && xmake build -y fio-sluice-ring
python3 tools/check_fio_engine.py --scratch-dir build/scratch --output results/fio/correctness.json
python3 tools/check_fio_engine.py --scratch-dir build/scratch --output results/fio/correctivity-uring.json --require-uring
```

运行时长约 2–3 分钟（串行）；结束后 `build/scratch/{cases,data}` 必须为空
（工具自检，非空即 fatal）。

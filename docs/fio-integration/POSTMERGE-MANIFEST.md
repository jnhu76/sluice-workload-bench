# Post-Merge Manifest（fio Phase 1 收口证据固定）

- 日期：2026-11-19；范围：PR #1 合并后的状态核实 + 回归 + 证据固定（非性能轮）。
- benchmark main：`bb4d387df5a3b39e4a21884c542ac64a7fb92e6b`（PR #1 squash merge，
  mergedAt 2026-11-19T04:39:28Z）。tree `c32cb737ea9dd384eb0a4e588933416e3b906f8d`
  与交付分支 HEAD `da24bc4` 树哈希一致（`git diff da24bc4 origin/main` 为空）——
  main 完整包含 Phase 1 实现与 r2 修复。
- Sluice SDK pin：`5d94f233967c3aef993eb19ba9e3252c532b0951`（`SLUICE_REVISION`；
  四个 SDK 前缀的 `.ready` 均为该 SHA）。本地 Sluice 产品仓库 clean 于同一 SHA，
  #621 三个 evidence 目录未触碰。
- fio：`fio-3.40`，冻结源码 `ff930c4653ae3952d6b09ab3ec89671aeabf2cbe`，
  `FIO_IOOPS_VERSION 37`（`ioengines.h:12`）。
- 平台：Xeon E5-2666 v3（20 逻辑核）；Fedora 44，kernel `7.3.3-200.fc44.x86_64`；
  GCC `16.2.1 20261029 (Red Hat 16.2.1-3)`；xmake `v3.1.2+HEAD.5d21e9d`；
  liburing `2.13`；scratch 所在 FS：btrfs（/home）。

## 1. 本轮实际执行的验证命令与结果（main 工作树，全部可复算）

| 命令 | 结果 |
| --- | --- |
| `xmake f -m release -y` | exit 0 |
| `xmake build -y` × 6 目标（bench×2、.so×2、errno-test、lifecycle-test） | exit 0（仅冻结 fio 头的两个已知 -Wunused-parameter 警告） |
| `bash adapters/fio/tests/smoke.sh` | exit 0（errno/lifecycle 单测 ok；tp/uring × read/write × D1/4/16 全 ok；io_uring: available (exercised)；无残留线程/fd） |
| `REQUIRE_URING=1 bash adapters/fio/tests/smoke.sh` | exit 0 |
| `python3 tools/check_fio_engine.py --scratch-dir build/scratch --output results/fio/correctness-postmerge.json --require-uring` | exit 0；**90 passed / 0 failed / 0 skipped** |
| `xmake check`（noring） | exit 0（noring 无 uring，io_uring 项正确 skip） |
| `xmake check-ring`（ring） | exit 0 |
| `nm -D` + `nm` 两个 .so | 无 seam 符号：`SLUICE_FIO_INIT_SEAM` 仅存在于 lifecycle 测试二进制 |
| 结束时 scratch 检查 | `build/scratch` 已清空（smoke 自建空 `jobs/` 目录已随清理移除） |

- T1（borrowed-fd close 负控制）与 T2（init 失败注入/回滚/同进程再 init）在两次
  smoke 内通过（`ok: engine lifecycle unit test`；其上方一行 `terminate called
  without an active exception` 为 T1.4 contract-guard 子进程的预期 SIGABRT 回执）。
- 异常诊断扫描：全部 gate 日志无 in-flight 关闭、Request 遗失、fallback 或
  segfault 诊断。
- 工作区日志（未入 git，小体量）：`results/fio/postmerge/{build,smoke,
  smoke-uring,correctness-matrix,check-noring,check-ring}.log`；
  矩阵 JSON `results/fio/correctness-postmerge.json`
  （sha256 `56d1ff2efbfca0f0b5965694ff38b5a73f76e20459e61474ac766427502bff64`）。

## 2. 产物 SHA-256（canonical；复现性见 §3）

| 产物 | sha256 |
| --- | --- |
| `fio-sluice-noring.so` | `21e41cf924b3c97ab3df43414c588031c6ae53151da0cd032979b46a1b52c4fc` |
| `fio-sluice-ring.so` | `0b03514af886330f1a6923c6185247a355bf6e77f1def5008b3b6570fb3ae132` |
| `fio-sluice-lifecycle-test` | `5c65c85a11de2277a089e60ad47ba0141f468ed1ec027f360b51af86b3ac0325` |
| `fio-sluice-errno-map-test` | `f14bc7e49c1a8052c5edd1db45ef3511021776b186eb4c7226d37335d38f6d09` |
| `sluice-workload-bench` | `c8538ec9adf314dc5ec1f7c5036faa31c0847c8389ca0c15d4a2d437d2ae701a` |
| `sluice-workload-bench-ring` | `b5042d65912529aa22ee97c5052a11553567340706917e320d0122b2ae40b10c` |

## 3. 复现性记录，及与 r2 台账 (e) .so 哈希差异的处置

- 四个非 PIC 链接产物（bench×2、errno-test、lifecycle-test）本轮重链后与
  r2 台账 (e) **逐字节一致**。
- 两个 .so：本轮三次增量重链哈希稳定；随后各自**从零重建 PIC SDK**
  （删除 `build/{noring,ring}-pic`，按 `.ready`=5d94f233 重新 checkout+构建），
  PIC 归档逐字节复现（core `24a67093e9c0dda0…`；noring async `b8eb4dfa8c9f8e40…`；
  ring async `d9eeaa50a8530d72…`），重链 .so 哈希不变。
- r2 台账 (e) 记录的 .so 哈希 `b589cff4…`/`969fa921…` **无法**自
  （树 `c32cb737` + SDK `5d94f233`）复现；差异输入边界明确为仅 .so 链接的
  PIC 归档（四个非 PIC 产物均吻合），r2 中间产物已不存在、无法进一步归因，
  判断为 r2 过程中的陈旧中间 PIC SDK 状态。r2 台账作为历史回执保留不动；
  **本清单 §2 为 canonical**。正确性判定与该差异无关（gate 所跑 .so 与 §2
  字节一致）。
- 环境事件：从零重建 noring PIC SDK 时 GitHub 浅取两次瞬时失败
  （`expected 'packfile'` / SSL `unexpected eof`），改自本地 pinned 克隆播种
  源码后成功；ring 经远端 fetch 成功。不影响任何 gate（gate 运行时 `.ready`
  命中，不重建 SDK）。

## 4. 既有 F1–F4 与测量证据等级（不变更，引用 r2 定性）

- F1/F2/F4：**EXPLORATORY_F1_F2_F4**（已归档矩阵原件；sluice-uring 相对
  native io_uring：F1 4 KiB D1 ≈0.69×、F2 4 KiB D16 ≈0.59×、F4 64 KiB D4
  ≈0.92×）。
- F3：**F3_ENVIRONMENT_CONFOUNDED**（SATA SLC 缓存耗尽支配，不参与排名）。
- PMU 指令/IO 归因：**UNVERIFIED_EXPLORATORY_ATTRIBUTION**（原始计数从未
  归档；组合开销口径；不作产品结论依据）。
- A/A：**OBSERVED_TWO_REPEAT_SPREAD**（仅 psync Δ1.645% 与 sluice-TP
  Δ8.346%；INSUFFICIENT_FOR_GENERAL_NOISE_THRESHOLD）。

## 5. 未覆盖范围（诚实记录）

- F1–F4 正式性能矩阵未重跑（非本轮要求；已归档原件不变）。
- io_uring/sluice-uring A/A 补充采样未执行（归 bench #2/#3 的新会话）。
- read_iolog/真实 trace 回放仍未实装（Phase 2 范围）。
- PMU/eBPF 归因测量未启动——交接 bench issue #3（ATTRIBUTION-READINESS）。

## 6. 本清单伴随的最小文档修正

`README.md`、`adapters/fio/README.md`、`PHASE1-REPORT.md` §E.2 的目标清单
补入 r2 增补的第 6 个目标 `fio-sluice-lifecycle-test`（post-merge 文档审计
发现的唯一可证明错误类别；构建命令本身全部有效）。

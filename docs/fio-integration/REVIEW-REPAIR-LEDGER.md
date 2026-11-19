# 评审修复台账（r2）

## (a) 头部

- BASE：`bea54ac8`；修复以 4 个提交落地（`fix(fio)`×2 = `ed8ca4f`/`3e995c5`、
  `test(fio)` = `3734974`、`docs(fio)` 收尾——本台账随 docs 提交入库；提交序列见 git log）。
- 评审输入：<https://github.com/jnhu76/sluice-workload-bench/pull/1#pullrequestreview-5968562085>
- 日期：2026-11-19。
- 环境：裸机 Xeon E5-2666 v3；fio `fio-3.40`（`FIO_IOOPS_VERSION 37`）；
  SLUICE_REVISION `5d94f233967c3aef993eb19ba9e3252c532b0951`。
- 代码阶段（r2 代码修复 + 确定性测试）已被 lead 复跑全部门后接受；
  文档轮完成证据强度降级与文档对账。

## (b) 发现处置表

| 编号 | 发现 | 处置 | 依据 |
| --- | --- | --- | --- |
| R-F1 | 根 README 仍写 make，构建已迁 xmake | RESOLVED_BEFORE_THIS_ROUND（commit `a9a9fd8`；在 `bea54ac8` 复核；本轮未再改动） | git 历史 |
| R-F2 | `sluice_close_file` 对在飞行借用仅记录后照常关闭 | RESOLVED（Plan A：违反不变式 → 记录 + `std::terminate()`，不关闭 fd、不返回错误；`adapters/fio/src/files.cpp`；T1 守卫测试） | §(c)/(d) |
| R-F3 | `sluice_init` 的 `make_unique` 在 try 外、slot 容器分配在 catch 后，异常可穿透 C 回调 | RESOLVED（单一 try 覆盖全部可抛初始化；unique_ptr 持有至成功、失败 RAII 回滚；`adapters/fio/src/engine.cpp`；T2；`nm` 证实 seam 不在两个 .so 产物中） | §(d) |
| R-F4 | 文档漂移 ×2（adapters/fio/README 测量工具"尚未实现"；ADAPTER-ARCHITECTURE 状态"未实现"） | RESOLVED（两行改为已交付表述） | git diff |
| R-F5 | 指令/IO 等归因计数回执从未归档（树内与 git 历史均无） | QUALIFIED（降级 UNVERIFIED_EXPLORATORY_ATTRIBUTION；数字保留，加限定；证据审计结论 NOT_ARCHIVED anywhere incl. git history） | PERF-BASELINE §5、PHASE1-REPORT §H、PR-BODY |
| R-F6 | A/A 仅有 psync 与 sluice-TP、各 2 次重复；无 io_uring/sluice-uring A/A 对 | QUALIFIED（降级 OBSERVED_TWO_REPEAT_SPREAD / INSUFFICIENT_FOR_GENERAL_NOISE_THRESHOLD；psync Δ1.645% / sluice-TP Δ8.346%） | PERF-BASELINE §2、PHASE1-REPORT §G.2、PR-BODY |

## (c) fio 3.40 生命周期分析（close-with-in-flight 可达性）

冻结源树 `build/fio/upstream` @ `ff930c4` 的逐行审计链：

- `close_file` 回调仅在 `put_file` 且 `f->references == 0` 时触发
  （filesetup.c:1898、1912-1913）；
- 每个 io_u 自 `set_io_u_file` 起持有文件引用（io_u.c:1488），直到
  `put_io_u` 收割时才释放（io_u.c:855-856；IO_U_F_FLIGHT 在 io_u.c:2103
  由 io_completed 清除在前）；
- `td_io_close_file` 将真实关闭推迟到最后一个 io 完成
  （ioengines.c:636-641）。

判词：**CLOSE_WITHINFLIGHT_UNREACHABLE_VIA_LEGAL_FIO_SEQUENCES** —— 合法
fio 序列不可能带着在飞行借用进入 close_file；出现该状态只可能是引擎
记账 bug（如 event() 重复交付）。"返回错误但不关闭"不可行：put_file 在
回调返回后断言 `f->fd == -1`（filesetup.c:1921），本 fio 构建启用 assert，
该路径同样中止 fio（NDEBUG 构建则使文件状态失同步）。故修复取 Plan A：
视为被违反的不变式，记录后 `std::terminate()`，与引擎既有 fail-closed
惯例一致（queue.cpp slot 记账 bug → terminate；events.cpp `fail_closed()`）。

## (d) T1/T2 RED→GREEN 回执（关键行）

原始日志：`results/fio/lifecycle/red-t1t2.log`、`results/fio/lifecycle/green-t1t2.log`。

RED（未修复引擎，仅 seam）：

```text
fio-sluice: closing fd 3 with an in-flight request
FAIL t1 violation: stub closer never ran while borrowed
FAIL t1 violation: engine failed closed (SIGABRT)
terminate called after throwing an instance of 'std::bad_alloc'
RUN_EXIT=134
```

（旧行为实际关闭了借用中的 fd——stub 写出 'X'、子进程 exit 42；
T2 的 seam 异常穿透 C 回调导致进程 SIGABRT。）

GREEN（修复后）：

```text
fio-sluice: close_file invariant violated: fd 3 still has in-flight request(s); failing closed
terminate called without an active exception
ok: t1 violation: engine failed closed (SIGABRT)
lifecycle_test: PASS
RUN_EXIT=0
```

（违反路径 fail closed：SIGABRT、管道 EOF 证明 stub 关闭器从未运行；
T2 四变体（step 0/1/2 × bad_alloc + step 1 非 std 异常类型走 catch(...)）
全部受控：返回 1、`io_ops_data` 为空、同进程再 init 成功且 cleanup 干净。）
T1 为 contract-guard 测试：该状态经合法 fio 序列不可达（§(c)），故以
合成方式直接调用回调。

## (e) r2 回归门（lead 复跑，全部 exit 0）

门跑时 HEAD = `bea54ac8`，修复在工作树未提交。

| 门 | 结果 |
| --- | --- |
| `xmake build` × 6 目标（noring/ring .so、bench ×2、errno-test、lifecycle-test） | exit 0 |
| `bash adapters/fio/tests/smoke.sh` | exit 0 |
| `REQUIRE_URING=1 bash adapters/fio/tests/smoke.sh` | exit 0 |
| `tools/check_fio_engine.py --require-uring`（90/90/0） | exit 0 → `results/fio/correctness-review-r2.json` |
| `xmake check`（tools/check.py, noring） | exit 0 |
| `xmake check-ring`（tools/check.py, ring） | exit 0 |

门时产物 sha256：

- `fio-sluice-noring.so` `b589cff4ab5da4fe458b9460093960711c29382a0f94e487295ad5d7988f6f7f`
- `fio-sluice-ring.so` `969fa921fa0972eb15ba2c0ab61511044ae07b1c29f320ba7e14593ed3be03ca`
- `fio-sluice-lifecycle-test` `5c65c85a11de2277a089e60ad47ba0141f468ed1ec027f360b51af86b3ac0325`
- `fio-sluice-errno-map-test` `f14bc7e49c1a8052c5edd1db45ef3511021776b186eb4c7226d37335d38f6d09`
- `sluice-workload-bench` `c8538ec9adf314dc5ec1f7c5036faa31c0847c8389ca0c15d4a2d437d2ae701a`
- `sluice-workload-bench-ring` `b5042d65912529aa22ee97c5052a11553567340706917e320d0122b2ae40b10c`

（已与本轮 `sha256sum build/linux/x86_64/release/*` 逐一复核一致。）

## (f) 统计与归因词汇表

- **UNVERIFIED_EXPLORATORY_ATTRIBUTION**：数字来自未归档回执的前一测量
  会话，本轮无独立原始计数复核；不作为产品性能裁决依据；Adapter+Sluice
  为组合开销，不能独立推断 Adapter 净开销。
- **OBSERVED_TWO_REPEAT_SPREAD**：两次同配置重复的观测极差（本次仅
  psync Δ1.645% 与 sluice-TP Δ8.346%）。
- **INSUFFICIENT_FOR_GENERAL_NOISE_THRESHOLD**：上述极差不覆盖
  io_uring/sluice-uring（无 A/A 对），不是最小可检测效应、不是 95% 区间、
  不成立为普适 <5%/<10% 判读规则。
- **EXPLORATORY_F1_F2_F4**：F1/F2/F4 为探索性对照基线（数字为已归档
  记录）；判读以两 repeats 均值 + 逐 rep 值逐 case 呈现。
- **F3_ENVIRONMENT_CONFOUNDED**：F3 受 SATA SSD SLC 缓存耗尽支配
  （环境混杂），不支持跨引擎排名。

## (g) 未执行项

- 正式性能矩阵本轮未重跑（r2 范围为代码修复 + 文档对账，无此要求；
  矩阵原件与交叉核对仍为 2026-11-19 已归档版本）。
- 可选的 uring A/A 补充采样未执行（选择以措辞降级
  OBSERVED_TWO_REPEAT_SPREAD / INSUFFICIENT_FOR_GENERAL_NOISE_THRESHOLD
  替代，而非补数据）。

## (h) 独立对抗复核（未参与修复的 Agent，新 HEAD）

- 范围：`bea54ac8..42d141f`（docs 提交 amend 前）；12 问逐项复核 + 自行
  重建/重跑（lifecycle-test exit 0、真实 3s fio randread D4 过 .so exit 0、
  产物 sha256 与本台账 (e) 逐一一致、nm 证实 seam 不在两个 .so）。
- 判词：**INDEPENDENT_REVIEW_CLEAN（无 P1/P2 正确性问题）**。
- P3 备注三条及处置（均已在 docs 提交内修复）：
  1. PHASE1-REPORT:410 "未定义符号集无 pread/pwrite" 为不实表述（nm 显示
     两个 .so 均有 `pread/pwrite@GLIBC`，来自所链接 Sluice SDK）→ 已改写为
     源码 grep + SDK 符号来源 + §5 运行时证据的准确表述；
  2. 台账/PR-BODY 对 `3734974` 的"代码 HEAD"标注不精确（其为 test 提交）→
     已改为逐提交标注（ed8ca4f/3e995c5/3734974/docs 收尾）；
  3. PERF-BASELINE:77 与 PHASE1-REPORT G.3 表的 futex 机制表述未挂定性标签
     → 已补"机制表述"标注并指向 §5（UNVERIFIED_EXPLORATORY_ATTRIBUTION），
     ctx/kiop 旁证保留为已归档原件。

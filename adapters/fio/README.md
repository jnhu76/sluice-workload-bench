# adapters/fio — Sluice fio external ioengine

- 状态：**已实现并自测通过**（fio-3.40 冻结树 ABI、threadpool/uring 双 backend、
  xmake 构建全部落地）。设计与全部决策见
  `docs/fio-integration/ADAPTER-ARCHITECTURE.md`（下称 ARCH）；本文件只记实现
  现状与落地时的偏差，不重复论证。
- 上游约束：fio 冻结树 `build/fio/upstream`（tag `fio-3.40`，`ff930c4`，
  `FIO_IOOPS_VERSION 37`，git-ignored；`xmake` 构建引擎目标时自动校验并在
  缺失时克隆/`./configure`）。Sluice SDK 由 `tools/build_sdk.sh`（非 PIC 前缀）
  与 `xmake.lua` 内置的等价 PIC 流程（`build/<profile>-pic/`）安装，revision
  读仓库根 `SLUICE_REVISION`。`SLUICE_SOURCE` env 优先，两种路径一致。

## 1. 文件清单（按 ARCH §1）

| 文件 | 职责 | 状态 |
| --- | --- | --- |
| `src/fio_cxx.hpp` | 唯一 include fio 头的 wrapper：固定 std 头集合 → 需要的 fio C 函数先以 `extern "C"` 预声明（首声明 linkage 粘滞，规避 C++ mangling）→ `config-host.h`/`fio.h`/`optgroup.h` | ✔ |
| `src/options.hpp` + `src/engine.cpp` 内表 | `struct EngineOptions`（首字段 pad 指针）+ `fio_option` 表（`sluice_backend`/`sluice_workers`；无容量旋钮，capacity ≡ td->o.iodepth） | ✔ |
| `src/state.hpp` | `EngineState`（context/scope/slots/staging/files/failed；成员序=析构序：scope 先于 context 释放） | ✔ |
| `src/errno_map.hpp` `.cpp` | `IoError → errno` 两规则映射（os_errno 透传；invalid_state/backend_error → EIO + 诊断）；无 fio 依赖，可独立单测 | ✔ |
| `src/engine.cpp` | ops 表（flags=`FIO_NO_OFFLOAD`）、init/cleanup、版本串 log（revision/profile/fio_source） | ✔ |
| `src/files.cpp` | open/close（转 generic_*，`F_GETFL` 登记 FileAccess）/get_file_size（纯转交） | ✔ |
| `src/queue.cpp` | queue（DDIR 映射：READ/WRITE/SYNC/DATASYNC 支持，TRIM 等带 EINVAL 拒绝）+ commit（引擎私有 `pending_submits` 计数喂 `io_u_mark_submit`，不用核心共享 `td->io_u_queued`——后者在 `td_io_getevents` 的 min>0 直调路径上会幽灵/双计提交直方图） | ✔ |
| `src/events.cpp` | getevents 主循环（take 扫描 → poll_progress → wait_one park；零超时轮询不 park）+ event + 健康失败两 tier | ✔ |
| `tests/smoke.sh` | 契约冒烟（见 §4） | ✔ |
| `tests/errno_map_test.cpp` | 映射穷举（os_errno 透传、EIO+诊断分支、eof 非错误） | ✔ |
| `tests/lifecycle_test.cpp` | 生命周期契约（T1 close_file 借用不变式 fail-closed；T2 init 分配失败遏制/回滚/再 init），链接真实引擎 TU；`SLUICE_FIO_INIT_SEAM` 仅编译进该测试二进制 | ✔ |
| `workloads/fio/smoke-read.fio` / `smoke-write.fio` | 占位符模板（`__ENGINE__/__FILENAME__/__BACKEND__/__IODEPTH__`） | ✔ |

## 2. 硬性不变式的落点（验收线，ARCH §2）

1. queue 返回值域 = {QUEUED, COMPLETED(带 error)}；无 BUSY（capacity≡iodepth
   论证 + 空槽显式检查 terminate，`queue.cpp` 头注释）。
2. 每个 QUEUED 的 io_u 恰好 stage 一次、经 `event()` 交付一次；getevents 入口
   检查上一批 cursor 一致（`events.cpp`）。
3. io_u 缓冲区只在 take 取得 publication 后才可能被 fio 复用；fd 只借不关。
4. 异常零逃逸：init/queue/getevents 均 try/catch 翻译。
5. 失败结算：Tier 1 = owner 驱动 `shutdown(drain)` 后按真实终态交付；Tier 2 =
   log + `std::terminate`（`settle_health_failure`）。
6. 无 fallback：noring 引擎 + uring → init 失败并打印
   "io_uring unavailable: engine built from the noring SDK profile"（smoke 有断言）。
7. 引擎调用的 fio 内部符号固定集：`generic_open_file`、`generic_close_file`、
   `generic_get_file_size`、`log_err`、`io_u_mark_submit`（全部经 `extern "C"`
   预声明；`nm -D` 实测未定义集 = 该 5 符号 + libc/libstdc++/pthread 版本化符号）。
8. 记账不变式运行时检查（log_err + terminate），不用 assert。
9. `event()` 绝不返回 nullptr（越界 terminate）。

## 3. 构建与自检（xmake）

```
xmake f -m release -y                 # 配置（不触发任何 SDK 构建）
xmake build -y sluice-workload-bench          # build/noring/sdk（经 tools/build_sdk.sh）
xmake build -y sluice-workload-bench-ring     # build/ring/sdk + -luring
xmake build -y fio-sluice-noring              # build/noring-pic/sdk（PIC，独立源树+前缀）
xmake build -y fio-sluice-ring                # build/ring-pic/sdk + -luring
xmake check / xmake check-ring                # python3 tools/check.py（显式触发才跑负载）
```

产物：`build/linux/x86_64/release/{sluice-workload-bench[-ring],
fio-sluice-noring.so, fio-sluice-ring.so, fio-sluice-errno-map-test,
fio-sluice-lifecycle-test}`。
引擎编译注入 `-DSLUICE_REVISION/-DSLUICE_BUILD_PROFILE/-DFIO_SOURCE_TAG`，
init 时打印一行版本串供对账。

落地时发现并解决的构建现实（与 ARCH §7 的差异）：

1. **非 PIC SDK 不能链接进 .so**（`relocation R_X86_64_32`，实测复现）→ PIC
   前缀按 ARCH 落地；`tools/build_sdk.sh` 无 pic 参数且按约束保持只读，PIC
   变体在 `xmake.lua` 内以同等流程实现（git 浅取/校验 tracked-clean/
   `xmake f --cxxflags=-fPIC`/install/`.ready` 戳）。
2. **xmake 3.1.2 嵌套项目解析**：`build/<profile>/sluice` 内的裸 `xmake` 会
   解析到最外层祖先（本仓库）并拒绝 `--liburing`。PIC 路径显式带 `-P`；
   `tools/build_sdk.sh`（只读）通过 `build/xmake-local-shim/xmake`
   （`exec real-xmake "$cmd" -P . "$@"`）在脚本进程树内注入 `-P .`——
   语义即脚本本意（在其 cd 进入的树内构建），不改脚本一行。
3. **hook 内嵌套 xmake 的项目锁**：嵌套调用取“工作目录”的项目锁，在仓库根
   起子进程会与父构建互相等锁 → PIC 流程先 `os.cd` 进 SDK 源树再调用。
4. **`-fvisibility=hidden`（xmake release 默认）**：ops 表变量显式
   `__attribute__((visibility("default")))`，否则 dlsym 找不到 `ioengine`。
5. **fio 头的 minmax 宏**：sluice 头必须在任何 fio 头之前 include（TU 内
   顺序：sluice 闭包 → 预计算常量 → `state.hpp`/`fio_cxx.hpp` → 其余）；
   `nanoseconds::max()` 之类调用在宏区之前求值成常量。

`xmake clean -a` 会清掉整个 `build/`（含冻结 fio 树与已装 SDK）：均可按需
重建（引擎目标前置钩子自动做）；`.xmake/` 已入 `.gitignore`。

## 4. 冒烟与测量入口

- 冒烟：`bash adapters/fio/tests/smoke.sh [scratch-dir]`（默认 `build/scratch`，
  脚本自建目录；串行）。覆盖：插件加载（版本串断言）、TP/uring × 读/写 ×
  iodepth 1/4/16、`verify=crc32` 数据校验（见下）、非法 backend 拒绝、
  noring 引擎拒绝 uring（无 fallback）、errno 映射单测、/proc 线程/fd 采样 +
  进程退出无残留 + scratch 无泄漏。
- **verify=crud 说明**：任务契约写 `verify=crud`，但 fio 3.40 无此方法
  （`options.c` posval 只有 md5/crc*/sha* 系），模板按意图取最廉价的
  `verify=crc32`。
- `REQUIRE_URING=1 bash adapters/fio/tests/smoke.sh`：uring 不可用 → 打印
  `UNAVAILABLE: <原因>` 并 **exit 77**，绝不回退 TP；不带 REQUIRE_URING 时
  uring 项打印记录并跳过，其余必须通过（两种分支均已实测）。
- 测量矩阵工具已交付：`tools/run_fio_matrix.py`（串行正式矩阵，A/A 仅
  psync/sluice-TP×2 repeats）与 `tools/compare_fio.py`（汇总 + envelope +
  交叉核对，写入 PERF-BASELINE.md 数据表）；实测记录见 PERF-BASELINE.md 与
  `results/fio/`（git-ignored 工作区证据）。

## 5. 实测记录（2026-11-19，本分支）

- 4 个契约目标 + errno 单测目标全部经 `xmake build -y <target>` 构建成功。
- `bash adapters/fio/tests/smoke.sh`：PASS（19 项 job + 2 项拒绝 + 单测；
  exit 0）；REQUIRE_URING=1：exit 0；隐藏 ring 引擎时 REQUIRE_URING=1 →
  exit 77、无 REQUIRE_URING → 跳过并 PASS。
- `python3 tools/check.py build/linux/x86_64/release/sluice-workload-bench`：
  PASS（io_uring skipped, noring）；`...-ring --require-uring`：PASS（含
  uring 臂，byte oracle 通过）。
- `io_submit_mode=offload`：装载期被拒（FIO_NO_OFFLOAD）；`verify_async=2`：
  init 拒绝（非 owner 线程 queue）。

## 6. 许可

引擎含 fio 头并链接其结构，属 GPLv2 衍生作品；源码随本仓库（开源）分发即
满足义务（ABIQ §3.9）。不要把 fio 头复制进本目录。

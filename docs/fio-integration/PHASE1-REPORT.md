# Phase 1 交接报告：fio 外部 Sluice ioengine（PHASE1-REPORT）

- 日期：2026-11-19。分支：`feat/fio-sluice-engine`。汇编人角色：交付工程师（本文件为交付阶段唯一新增写入物之一，另含 README 更新与 PR-BODY）。
- 本文是 Phase 1（fio 引擎集成）的**总交接文档**：A–K 十一节汇总全部阶段产出，附 Owner Gate 十问与建议判词。
  各节不重复论证，只给结论 + 指回证据；详细论证在四份上游文档里：
  - `docs/fio-integration/REPOSITORY-REALITY.md`（下称 REALITY，Phase 0 审计）
  - `docs/fio-integration/FIO-ABI-QUALIFICATION.md`（下称 ABIQ，Phase 1 ABI 资格）
  - `docs/fio-integration/ADAPTER-ARCHITECTURE.md`（下称 ARCH，设计定稿）
  - `docs/fio-integration/CORRECTNESS-MATRIX.md` 与 `docs/fio-integration/PERF-BASELINE.md`（Phase 5 结果）
  以及实现自述 `adapters/fio/README.md`。
- 引用格式：本仓库文件为仓库相对 `path:line`；Sluice 只读 checkout 内文件记为 `Sluice:<path>:line`；
  fio 冻结树（git-ignored，`build/fio/upstream`）记为 `fio:<path>:line`。
  凡本交付阶段亲自执行的检查标注"本次复验"；其余来自上游文档并指明出处。

---

## A. 仓库现实

### A.1 两仓库 HEAD（本次复验，2026-11-19 交付时点）

| 仓库 | HEAD | 状态 |
| --- | --- | --- |
| `jnhu76/sluice-workload-bench`（本仓库，开发库） | `e7b4603`（分支 `feat/fio-sluice-engine`；本报告与 README/PR-BODY 的交付提交叠在其后） | 工作区干净（`git status`：nothing to commit） |
| `/home/jnhu/Source/Sluice`（只读 checkout，上游 `jnhu76/Sluice`） | `5d94f233967c3aef993eb19ba9e3252c532b0951`（`master`，与 `origin/master` 一致，REALITY:51-53） | **零已跟踪文件改动**（本次复验 `git status --short`：仅 3 个 Phase 1 之前就存在的未跟踪 evidence 目录 `docs/evidence/621-*`、`docs/evidence/e5-fullpath-machine-work`） |

`SLUICE_REVISION` 文件内容 = `5d94f233967c3aef993eb19ba9e3252c532b0951`，与只读 checkout HEAD 逐字节相等（本次复验 `cat` 对照）。**Sluice 仓库在本阶段全程序只读；引擎对 Sluice 的全部消费经安装 SDK 的公共头 + 静态库（§E.4、§J 问题 1 的零 diff 证明）。**

分支提交序列（`git log --oneline`，main..HEAD）：

```
e7b4603 docs(fio-integration): E5 performance baseline report (formal matrix)
33c294b perf(fio-engine): serial F1-F4 four-engine matrix tooling and baseline skeleton
a7c442c test(fio-engine): deterministic correctness matrix tool and results
20c710e fix(fio-engine): count submit batches with an engine-private counter
1900d3c feat: add fio-sluice external ioengine and migrate build to xmake
d10bd51 docs: fio 3.40 external ioengine ABI qualification
```

（2abda4d 及之前为 Phase 0 之前的基线历史；`d10bd51` 是 ABI 资格文档提交。）

### A.2 工具链与环境（本次复验 + REALITY §5）

本次复验命令与读数（2026-11-19，均只读）：

| 项 | 读数 | 命令 |
| --- | --- | --- |
| fio | `fio-3.40`（distro 包 `fio-3.40-3.fc44.x86_64`，`/usr/sbin/fio`） | `fio --version`；REALITY:251 |
| fio 上游冻结树 | tag `fio-3.40`，commit `ff930c4653ae3952d6b09ab3ec89671aeabf2cbe`，`FIO_IOOPS_VERSION 37` | ABIQ:47-52, 66；`xmake.lua:35-36` 构建期断言 |
| 编译器 | `gcc (GCC) 16.2.1 20261029 (Red Hat 16.2.1-3)`（g++ 同源，引擎为 C++20） | `gcc --version`；REALITY:247 |
| xmake | `v3.1.2+HEAD.5d21e9d` | `xmake --version` |
| liburing | `2.13`（devel 包 `liburing-devel-2.13-2.fc44`，**仅动态库**，无 liburing.a） | `pkg-config --modversion liburing`；REALITY:249 |
| Python | `3.14.10` | `python3 --version` |
| 内核 | `7.3.3-200.fc44.x86_64`（Fedora 44） | `uname -r` |
| io_uring 系统开关 | `/proc/sys/kernel/io_uring_disabled` = 0（未禁用） | `cat` |
| CPU | Xeon E5-2666 v3，10C/20T，单 NUMA；governor=schedutil（未钉 performance，sudo 需密码） | REALITY:239-240；PERF-BASELINE.md:35 |
| 文件系统 | `/`、`/home` 均为 /dev/sda3 btrfs（compress=zstd:1, ssd, discard=async）；/tmp tmpfs | REALITY:242-244 |

### A.3 SDK

- 四个独立安装前缀：`build/{noring,ring}/sdk`（非 PIC，bench 二进制用，`tools/build_sdk.sh`）与
  `build/{noring,ring}-pic/sdk`（PIC，引擎 .so 用，`xmake.lua:84-121` 等价流程）。
  就绪契约 = 前缀下 `sdk/.ready` 内容等于 `SLUICE_REVISION`（`xmake.lua:153-171`）。
- 安装面 = 公共头 allowlist（`Sluice:xmake/libraries.lua:17-22,47-51`）：`sluice/async/{async_io_context,request,request_scope,threadpool_backend,uring_backend}.hpp`、
  `sluice/blocking/file.hpp`、`sluice/{effect,error,file_resource,measurement,result}.hpp` + 两静态库（REALITY:161-172）。
  **任何 `detail/` 头不安装、不被引擎引用**（本次复验：`grep -rn "detail" adapters/fio/src/` = 0 处）。

---

## B. fio ABI 报告要点（详见 ABIQ）

1. **资格判定：QUALIFIED**（ABIQ:3-5）。distro `fio-3.40` 接受按冻结 tag `fio-3.40` 构建的外部引擎；
   版本门禁 `FIO_IOOPS_VERSION 37` 是**严格相等**（36 被拒，实测回执 ABIQ §4.2）。
2. **加载约定**：合格且唯一的调用形态是 `--ioengine=external:<绝对路径>`（ABIQ §3.3；
   RTLD_LAZY dlopen，符号解析顺序 `<引擎名>` → `ioengine` → `get_ioengine`）。
   短名 dlopen 依赖宿主 `CONFIG_DYNAMIC_ENGINES`，未探测、仓库内不依赖（ABIQ:131-136, 473-477）。
3. **`struct ioengine_ops`**（fio:ioengines.h:29-75）只在单一 `FIO_IOOPS_VERSION` 值内保证布局
   （ABIQ §3.2 逐字段表）；必选回调 `queue`，异步另需 `getevents`+`event`，`open_file` 事实必选。
4. **queue 返回协议**：`FIO_Q_COMPLETED`=本次调用内完成（核心自记完成）、`FIO_Q_QUEUED`=稍后必须经
   getevents/event **恰好一次**交付、`FIO_Q_BUSY`=未接受且必须有 commit 走出重试（ABIQ §3.5）。
5. **getevents/event 协议**：返回 `[0,max]` 完成数，负数进 `td_verror`；`t==NULL` 阻塞至 min、
   `{0,0}` 立即轮询；`event(td,i)` 的返回值被核心**无判空**解引用（ABIQ §3.6）。
6. **C++ 边界裁决：纯 C++20 插件，无 C shim**——裸含 `fio.h` 在 C++20 下通过、`extern "C"` 包裹
   非法（arch.h 的 `<atomic>`）、`ioengine` 全局变量不 mangle 可被 dlsym、libstdc++ 随 .so 进
   DT_NEEDED（ABIQ §4.4/§4.5/§5，实证 `nm -D`/加载运行回执）。头文件宏污染
   （fio:minmax.h:4-18）决定了"std 头先于 fio 头"的单 wrapper 纪律。
7. **引擎私有选项**：`td->eo` 首指针字段被核心写入 `td`（fio:init.c:1276），选项结构必须以 pad
   指针开头、`off1` 永不为 0（ABIQ §3.8）。
8. **许可**：引擎含 fio 头、链其结构，属 GPLv2 衍生作品（ABIQ §3.9；`adapters/fio/README.md` §6）。
   源码随本开源仓库分发即满足；**禁止**把 fio 头复制进仓库（冻结树留在 git-ignored 的
   `build/fio/upstream`，`xmake.lua:173-192` 自动克隆/校验/`./configure`）。
9. **版本钉死是硬约束**：fio 升级必须换对应 tag 重建重验；工具链在运行前断言
   `fio --version` 与引擎构建版本一致（ABIQ §6.1；`tools/check_fio_engine.py` 实装）。

---

## C. 适配器架构（详见 ARCH；实现自述 `adapters/fio/README.md`）

### C.1 所有权模型

- **每 job 恰好一个 `EngineState`**，挂 `td->io_ops_data`，`init` 中 `new`、`cleanup` 中 `delete`
  （`adapters/fio/src/state.hpp:21-68`、`engine.cpp:89-135,138-160`）。进程级全局可变状态为零。
- **fd 所有权始终在 fio**：`open_file/close_file` 纯转交 `generic_open_file/generic_close_file`，
  引擎只以 `fcntl(F_GETFL)` 推导 `FileAccess` 并登记借用表（`files.cpp:10-60`）；每次提交即时构造
  值类型 `NativeFileRef{fd, access}`（`queue.cpp:62`，公共外来 fd 借用入口
  `Sluice:include/sluice/async/async_io_context.hpp:30-38`）。引擎绝不 close fio 的 fd（LIFE-02）。
- **缓冲区借用到 publication 为止**：`io_u->xfer_buf` 只在 `getevents` 的 `take()` 取得
  publication 之后才随事件交还 fio 复用（REQ-04）；两侧边界重合，无窗口（ARCH §4.3）。
- **成员声明序 = 析构序**：`RequestScope`（progress owner 句柄）先于 `AsyncIoContext` 释放
  （`state.hpp:39-42`），满足 SHUT-04 析构前置条件。

### C.2 Request 生命周期映射

| fio 侧 | Sluice 侧 | 引擎侧载体 |
| --- | --- | --- |
| `io_u`（FLIGHT 期间借出） | `ScopeTicket`（RequestId 票根） | `slots[i]` 同持两者（`state.hpp:27-34`） |
| `io_u->file->fd` | `NativeFileRef`（借用） | `files` 登记表（`state.hpp:51`） |
| `io_u->xfer_buf/xfer_buflen/offset` | `ReadOp/WriteOp` 描述符 | `scope.submit_read/submit_write`（`queue.cpp:89-108`） |
| `io_u->error/resid` | `Result<size_t>`（确认字节数或 IoError） | getevents 内写入（`events.cpp:42-77`） |

容量恒等式：`capacity ≡ td->o.iodepth`（`engine.cpp:78`，无用户旋钮）——这是"queue 永不 BUSY、
空槽必有"论证的承重墙（ARCH §3/§4.5；`queue.cpp:3-14` 头注释）。

### C.3 Completion 映射

- `harvest()`：非破坏 `take()` 扫描全部槽——pending 原样保留、ready 即消费并回收 scope 槽、
  同步填写 `error/resid` 后 push 进 `staged[]`（`events.cpp:91-130`）。
- 成功：`resid = xfer_buflen - confirmed`（短读短写如实上报，`events.cpp:46`）；
  `eof` 语义 = 全量短读非错误（`events.cpp:50-54`，SEM-05）。
- 错误：两规则映射（§D.4）；`event(td,i)` 返回 `staged[i].u`，**绝不 nullptr**（`events.cpp:254-266`）。
- 每个 QUEUED 的 io_u 恰好 stage 一次、交付一次；getevents 入口断言上一批被 event() 消费完
  （`events.cpp:168-170`）。

---

## D. 回调契约（实现层逐条）

### D.1 queue / commit

- queue 返回值域 = {`FIO_Q_QUEUED`, `FIO_Q_COMPLETED`(带 error)}，**BUSY 不可达**
  （`queue.cpp:3-14`）：容量恒等式 + 空槽显式检查（记账 bug → log_err + terminate，`queue.cpp:64-71`，
  不信任 by-construction 到 UB 的程度）；非瞬态提交错误（admission 关闭/健康/invalid_state）映射
  BUSY 会造成重试死锁，故一律带错 COMPLETED（请求未被接受，INV-02：拒绝无副作用）。
- ddir 映射：READ/WRITE/SYNC/DATASYNC 支持；TRIM/SYNC_FILE_RANGE/WAIT 以 `EINVAL` 带错完成
  （`queue.cpp:50-54`）——fail closed，绝不静默当成功。
- 未登记 fd → `EBADF` 带错完成（`queue.cpp:56-61`）；Sluice 提交异常 → catch → `EIO` 带错完成
  （`queue.cpp:122-132`），异常零逃逸。
- commit 是薄记账：以**引擎私有** `pending_submits` 计数喂 `io_u_mark_submit`，不用核心共享
  `td->io_u_queued`——后者在 `td_io_getevents` 的 min>0 直调路径上会幽灵/双计提交直方图
  （`state.hpp:53-61`、`queue.cpp:22-33`；修复提交 20c710e）。

### D.2 getevents

两种核心调用形态（`events.cpp:3-7`）：`min>0, t==NULL` 阻塞至 min；`min==0, t={0,0}` 立即轮询；
非零有限 t 按相对预算处理。主循环（`events.cpp:197-246`）：

```
harvest → (staged≥min 或 ==max → 返回) → poll_progress（有界推进，owner 职责）
  → 有欠活（immediate/dispatch_retry）→ 继续，不许 park（PROG-01）
  → 静默 → wait_one(剩余预算或无界) park
  → progress/deadline_expired → 回到 harvest 再判超时（PROG-04：到期不取消 I/O，也不搁浅完成）
  → control_interrupted → acknowledge_progress_control 后再等
```

- 零预算轮询至多一次有界推进、绝不 park（`events.cpp:198-216`）。
- 健康失败两 tier 结算（§D.5）；记账不变式违反 → log_err + terminate（release 下 assert 会被
  编译掉，`events.cpp:34-40`）。

### D.3 event

返回 `staged[i].u`，越界/状态缺失 fail closed（terminate），绝不返回 nullptr——核心
`io_completed` 第一行解引用返回值（`events.cpp:254-266`；ABIQ §3.6）。

### D.4 错误映射（errno_map）

规则一：`IoError.os_errno != 0` 原样透传（ERR-01）。规则二：code 归约
（canceled→ECANCELED、interrupted→EINTR、would_block→EAGAIN、no_space→ENOSPC、
permission_denied→EACCES、not_found→ENOENT、invalid_argument→EINVAL、not_supported→ENOTSUP、
invalid_state/backend_error→EIO + log_err 诊断；eof 非错误走 resid 路径）
（`adapters/fio/src/errno_map.cpp`；穷举单测 `adapters/fio/tests/errno_map_test.cpp`）。

### D.5 结算（settlement）与 shutdown

- **健康失败 Tier 1**：本线程是 owner → 驱动阻塞 `shutdown(ShutdownPolicy::drain)`；completed 后
  每个在途槽按**真实终态或映射错误**交付为完成，`failed=true`，后续 queue 一律拒绝
  （`events.cpp:138-158`）。
- **Tier 2**：drain shutdown 自身失败/未决 → log_err + `std::terminate`。理由：在途借用
  （buffer/fd）不能被宣布结束（SHUT-03），交还 fio = UAF、扣住 = 死锁，唯一不撒谎不损坏数据的
  出口是进程级失败（ARCH §5.5）。
- **cleanup**：`scope->finish()`（drain 语义；正常路径在途=0 即刻返回）→ 按成员序析构 →
  `delete state`；容忍半初始化（`io_ops_data == NULL` 直接返回）与未消费 staged 的诊断记录
  （`engine.cpp:138-160`）。
- **cancel 故意不实现**：借出缓冲区安全优先——提前 put_io_u 会让 fio 复用仍被借用的缓冲区
  （CANCEL-02）；不提供 cancel 使 fio 的提前退出整体退化为阻塞排空（ARCH §4.9）。
- **init 拒绝封闭集合外形态**：`verify_async`（非 owner 线程会调 queue）、非法 backend 名、
  iodepth=0、TP workers=0、uring 下非默认 workers（`engine.cpp:61-87`）；
  `io_submit_mode=offload` 由 `FIO_NO_OFFLOAD` 装载期拒绝（`engine.cpp:170`）。

---

## E. 实现与构建

### E.1 文件清单

`adapters/fio/src/{fio_cxx.hpp, options.hpp, state.hpp, errno_map.hpp/.cpp, engine.cpp, files.cpp,
queue.cpp, events.cpp, callbacks.hpp}` + `adapters/fio/tests/{smoke.sh, errno_map_test.cpp}` +
`workloads/fio/smoke-{read,write}.fio` 模板 + `tools/{check_fio_engine.py, run_fio_matrix.py,
compare_fio.py}`。逐文件职责见 `adapters/fio/README.md` §1。

### E.2 xmake 迁移（替代 Makefile；硬约束 6）

- **Makefile 已删除**（本次复验：仓库根无 Makefile），`xmake.lua` 是唯一构建入口。
  四个契约目标 + 单测目标 + 两个 check 任务（`xmake.lua:253-297`）：

| target | 产物 | SDK 前缀 |
| --- | --- | --- |
| `sluice-workload-bench` | bench 二进制（noring） | `build/noring/sdk`（非 PIC，`tools/build_sdk.sh`） |
| `sluice-workload-bench-ring` | bench 二进制（ring，链 `-luring`） | `build/ring/sdk` |
| `fio-sluice-noring` | `build/linux/x86_64/release/fio-sluice-noring.so` | `build/noring-pic/sdk`（PIC，独立源树+前缀） |
| `fio-sluice-ring` | `…/fio-sluice-ring.so`（DT_NEEDED 含 liburing.so.2） | `build/ring-pic/sdk` |
| `fio-sluice-errno-map-test` | 映射单测二进制 | noring SDK |
| task `check` / `check-ring` | `tools/check.py` / `--require-uring`（显式触发才跑负载，exit 77 语义保留） | — |

- 落地时发现并解决的构建现实（`adapters/fio/README.md:63-83`）：
  1. 非 PIC 静态 SDK 链不进 .so（`relocation R_X86_64_32` 实测）→ PIC 独立前缀按 ARCH 落地；
     `tools/build_sdk.sh` 按约束保持只读，PIC 流程在 `xmake.lua:84-121` 内实现同等校验
     （按 revision 浅取、tracked-clean 断言 `xmake.lua:101-105`、`--cxxflags=-fPIC`、`.ready` 戳）。
  2. xmake 3.1.2 嵌套项目解析会取最外层祖先 → `build_sdk.sh` 进程树经 PATH shim 注入 `-P .`
     （`xmake.lua:123-151`，不改脚本一行）；PIC 路径显式 `-P` 且先 `os.cd` 进源树避项目锁死锁。
  3. `-fvisibility=hidden`（release 默认）→ ops 表变量显式 `visibility("default")`
     （`engine.cpp:167`），否则 dlsym 找不到 `ioengine`。
  4. fio 头的 minmax 宏 → sluice 头/常量求值必须在任何 fio 头之前（`events.cpp:11-24` 注释、
     `fio_cxx.hpp` 单 wrapper 纪律）。
- `xmake clean -a` 会清掉整个 `build/`（含冻结 fio 树与 SDK）：均可由 before-build 钩子按需重建。

### E.3 构建与加载方法

```sh
xmake f -m release -y
xmake build -y fio-sluice-ring            # 或 fio-sluice-noring / sluice-workload-bench[-ring]
xmake check-ring                          # 显式触发 bench 功能检查（跑小负载）

/usr/sbin/fio --name=demo \
    --ioengine=external:$PWD/build/linux/x86_64/release/fio-sluice-ring.so \
    --sluice_backend=uring --rw=randread --bs=4k --iodepth=16 --direct=0 \
    --size=1G --time_based --runtime=10 --filename=<build/scratch 下的临时文件>
```

- 引擎选项：`sluice_backend`（`threadpool` 默认 | `uring`）、`sluice_workers`（TP worker 数，
  默认 4；uring 下非默认值被拒）（`engine.cpp:33-58`）。**无容量旋钮**：capacity ≡ iodepth。
- init 打印版本对账行：`fio-sluice: loaded revision=<SLUICE_REVISION> sdk_profile=<noring|ring>
  fio_source=fio-3.40 backend=… iodepth=… workers=…`（`engine.cpp:130-133`）；
  `tools/run_fio_matrix.py` 用正则强校验该行与请求一致（防 hidden fallback，PERF-BASELINE.md:177）。

### E.4 只消费公共 API 的证明（本次复验回执）

- 引擎源码的 sluice include 全集 = 6 个公共安装头：`async_io_context.hpp`、`request_scope.hpp`、
  `threadpool_backend.hpp`、`uring_backend.hpp`、`error.hpp`（+ 传递的 request.hpp）；
  `grep -rn "detail" adapters/fio/src/` = **0 处**；无任何对 `/home/jnhu/Source/Sluice` 源树路径的引用。
- Sluice 静态库被链进 .so：`nm -D --defined-only fio-sluice-noring.so | grep -c sluice` = 15；
  ring 变体 DT_NEEDED 含 `liburing.so.2`（liburing 2.0/2.3 版本化符号由 SDK 内嵌的 uring backend
  引用）。未定义符号全集 = 固定 5 个 fio 内部符号（`generic_open_file`、`generic_close_file`、
  `generic_get_file_size`、`log_err`、`io_u_mark_submit`）+ libc/libstdc++/libgcc/libm(/liburing)
  运行时——与 `adapters/fio/README.md` §2.7 声明的固定集一致，无越界 fio 内部依赖。

---

## F. 正确性矩阵结果（详见 CORRECTNESS-MATRIX.md）

- 工具：`python3 tools/check_fio_engine.py --scratch-dir build/scratch --output results/fio/correctness.json [--require-uring]`；
  裁决 exit 0/1/77（77 = --require-uring 且 uring 不可用，绝不回退 TP 臂）。
- **实测（本分支，2026-11-19）：默认 90 passed / 0 failed / 0 skipped（exit 0）；
  --require-uring 90 / 0 / 0（exit 0，uring 可用）**（CORRECTNESS-MATRIX.md:7-12；
  本次复验 `results/fio/correctness.json` summary = {passed: 90, failed: 0, skipped: 0}，
  `correctness-uring.json` 同且 uring.available=true）。
  负控制（隐藏 ring 引擎 + --require-uring）→ exit 77 已实测。
- 覆盖（backend × 引擎，全 35 case/臂）：read 4K D1/4/16 + 64K D1/4、write D1/4/16、
  verify=pattern 逐字节、mixed 70/30 D4/16 + 全量 audit、mixed-size bsrange、sparse thinktime、
  EOF 非对齐、fdatasync、负控制（非法 backend/noring 拒 uring/TRIM EINVAL 等）、
  cleanup-with-inflight（SIGTERM）。
- 双层数据 oracle：fio 内置 verify=crc32c + 脚本独立 POSIX 校验（sha256/size/pattern 逐字节/
  与 fio 原生 sync 引擎的 parity oracle）；校验非空转已实证（人为翻转 1 字节 → verify failed）。
- 与任务书信件的四项偏差全部如实声明并给探测依据（`timeout_threshold` 在 fio 3.40 不存在 →
  SIGTERM seam；regular file 上跨 EOF 读不可达 → EOF 边界 + trim EINVAL 承接短 I/O/失败路径；
  mixed-size 跨 job audit 不可行 → do_verify 回放；iodepth/workers=0 拒绝归属 fio 解析层）
  （CORRECTNESS-MATRIX.md §4）。
- 冒烟（引擎侧更早的门）：`bash adapters/fio/tests/smoke.sh` PASS（19 job + 2 拒绝 + 单测）；
  REQUIRE_URING=1 PASS；隐藏 ring → exit 77 / 跳过两分支均实测（`adapters/fio/README.md` §5）。
  bench 自身 `xmake check` / `check-ring`（tools/check.py）PASS（含 ring 臂 byte oracle）。

---

## G. 性能基线（详见 PERF-BASELINE.md；数字全部出自 `results/fio/` 50 个 fio JSON 原件）

### G.1 一句话结论

性能门 = **BASELINE_VALID**：读为主的 F1/F2/F4 上四引擎数字可信（A/A 噪声界内、rep spread
≤11%）；F3（写混）因 SATA SSD SLC 缓存耗尽**不支持跨引擎排名**（按方法论预先声明，非事后修饰）。

### G.2 A/A 一致性（F2 形态、10s 窗、两次同配置重复）

| 引擎 | rep1/rep2 IOPS | ΔIOPS% | Δp99% |
| --- | --- | ---: | ---: |
| psync（native） | 788149 / 775290 | 1.65 | 1.22 |
| sluice-threadpool | 220629 / 202952 | 8.35 | 6.76 |

机器+fio 原生链路噪声底 ≈1.6%；含引擎整链路 ≈8%。判读规则：native 引擎间 <5%、sluice 引擎间
<10% 的差异视为噪声（PERF-BASELINE.md:46-56；本次复验 compare.json `aa` 节数字一致）。

### G.3 F1–F4 × 四引擎（两 repeats 均值；热缓存；direct=0；randseed=81；完整表见 PERF-BASELINE.md 数据区）

| case | psync | io_uring (native) | sluice-uring | sluice-TP |
| --- | ---: | ---: | ---: | ---: |
| F1 4k randread D1 (IOPS) | 785.8k | 616.0k | **422.5k (0.69× native)** | 39.2k（futex 挥手串行化） |
| F2 4k randread D16 | 789.5k (D1-capped) | 618.9k | 368.0k (0.59×) | 211.6k |
| F3 4k randrw 70/30 D16 | 4.6k | 5.9k（rep spread 177%） | 98.1k（spread 1.9%） | 27.9k（spread 191%） |
| F4 64k seqread D4 | 158.1k (D1-capped) | 126.9k | 117.3k (0.92×) | **211.0k（四者最高，13.8GB/s）** |

F3 整行环境受限（SLC 耗尽区），只有 sluice-uring 的稳定性本身是可报告观察。psync 为同步引擎，
D16/D4 被 fio 钳到 D1（引擎语义，如实记录）。

### G.4 环境资格与 JSON 原件索引

- 环境资格表（PERF-BASELINE.md §1）：裸机 E5、亲和 `taskset 0-9`、温度 46°C 无降频、
  btrfs 真实 FS 路径（非 tmpfs）、每 case 1G 专用文件（seqwrite+fsync_on_close+全量预热读、
  `chattr +C`）、`invalidate=0`、`buffer_pattern=0x5a`、F3 确定性间歇。
  限制如实记录：governor 未钉 performance（sudo 需密码）、SLC 缓存耗尽、perf paranoid=2 仅用户态。
- 原件索引（PERF-BASELINE.md §6）：`results/fio/`（git-ignored 工作区证据）50 个 fio JSON
  （16 测量 + 4 A/A + prep/warm + 2 探针）+ 50 个 job 文件 + `manifest.json`（含环境快照、rusage、
  引擎 load 行）+ `compare.json`（聚合 + envelope + crosschecks）。本次复验：
  `compare.json.crosschecks` = {checked: 50, problems: []}——文档表与原件数字全部一致。

---

## H. Attribution（详见 PERF-BASELINE.md §5）

- **无隐藏 POSIX fallback**（syscall 层实证）：sluice-uring 短跑 strace 仅 `io_uring_enter`
  （61.4k 次 ≈ 1 次/IO）；sluice-TP 仅 `pread64`（74.0k ≈ 1 次/IO）。正式矩阵另有引擎 load 行
  强校验（backend 与请求一致）。
- **每 IO 用户态指令**：native psync ~2.4k、native io_uring ~2.9k；sluice-uring ~7.9k、
  sluice-TP ~8.9k → adapter+Sluice 层多 ~5–6k 用户态指令/IO（≈3× native driver 成本），
  与 sluice-uring 达 native 0.59–0.69× 相称——差距是真实机器工作，不是阻塞或等待。
- **TP 的成本在 futex 挥手不在指令**：task-clock 3.2×、每 IO ~3.4 次 futex、F2 CPU 41.4s vs
  其他 12.2s、ctx/kiop 10³ 倍量级——Sluice 产品侧 ThreadPool 属性，非 adapter 引入；
  D4 大块流水化后反超（F4 最高）。
- **kernel 侧计数不可得**（perf_event_paranoid=2）：内核态周期分解是环境限制，以 syscall 计数 +
  用户态指令 + rusage 旁证完成。
- 结论：适配层未发现严重阻塞、重复遍历或缓冲拷贝。

---

## I. Limitations（如实边界）

1. **不支持的 fio 操作**：`DDIR_TRIM`、`DDIR_SYNC_FILE_RANGE`、`DDIR_WAIT` 以 EINVAL 带错完成
   （`queue.cpp:50-54`；正确性矩阵有负控制）；`verify_async` 与 `io_submit_mode=offload` 被拒
   （非 owner 线程 queue / submitter 换线程）；`cancel` 回调故意不实现（借出缓冲区安全，
   提前退出退化为阻塞排空）。
2. **平台与版本钉死**：Linux x86_64；fio **精确** 3.40（ABI 版本门禁 37 严格相等，fio 升级必须
   换 tag 重建重验）；liburing 2.13 动态库（ring 变体运行时依赖 liburing.so.2）；io_uring 需内核
   未禁用（本机 `io_uring_disabled=0`）。跨版本加载、短名 dlopen、`--output-format=json` 大矩阵
   下的引擎行为未验证（ABIQ §6.6；正式矩阵本身以 JSON 输出跑通 50 run）。
3. **深度/并发形态**：单 job 单线程 owner 模型（每 td 独立 context+scope，多 job/numjobs>1 按构造
   支持——THREAD-01 逐 context 成立——但正式矩阵只测了 numjobs=1）；capacity ≡ iodepth 恒等式
   使深度超过容量的形态不存在（fio 自身 freelist 封顶）。
4. **uring 环境限制**：noring 构建的引擎 + uring → init 失败（无 fallback，exit err 语义）；
   ring SDK 需 liburing profile；正式测量期 uring 可用（uringUnavailable=false），但工具保留
   exit 77 UNAVAILABLE 通道。
5. **性能基线边界**：F3 写混场景受 SATA SSD SLC 耗尽支配，本 run 不支持跨引擎结论；
   governor=schedutil 未钉、btrfs zstd:1、热缓存（direct=0）——数字属"该环境该形态"基线，
   不外推；envelope 对照（vs bench）是探索性非严格 A/B（trace 实例不同、driver 开销混入，
   PERF-BASELINE.md §4 可比性边界）。
6. **direct=1（O_DIRECT）**：第一阶段范围外，未测。
7. **结果原件不入库**：`results/` 在 .gitignore 内（工作区证据）；仓库内可溯源的数字载体是
   PERF-BASELINE.md 的自动生成数据表（`tools/compare_fio.py` 写入，与原件交叉核对 50/50 一致）。

---

## J. 独立评审结论与十问回答

### J.1 评审输入与判词

- 独立对抗式终审（从未参与实现的评审员）在交付前执行：对象为 `git diff main...feat/fio-sluice-engine`
  全部变更 + `docs/fio-integration/` 全部文档 + `results/fio/` 原件；允许重跑冒烟/正确性矩阵
  （独立 scratch）与 strace/符号表抽查。
- 交付时点交接输入（工作流传给本交付阶段）：**终审判词 = conditional**（主要可用、非核心受限且
  全部限制已明示）；**性能门 = BASELINE_VALID**；**coreGreen = true**（冒烟 + 正确性矩阵 + 回归
  全部通过）；**uringUnavailable = false**。
- 按工作流 Owner Gate 判词计算规则（coreGreen ∧ 无遗留 high ∧ 终审非 blocked，且
  终审=approved ∧ 性能门=BASELINE_VALID 才给 GO）：**本阶段建议判词 =
  `PHASE1_CONDITIONAL_GO`**——conditional 的来源是终审未给 approved（受限项均已列入 §I），
  而非任何未明示的缺陷。
- 如实声明：评审员的逐字 gateAnswers 转录未持久化到仓库（工作流运行态数据）；下节十问的回答
  是交付工程师依仓库内证据逐条汇编，每条给出可复核出处。判词本身（conditional）来自工作流交接，
  出处即上段。

### J.2 Owner Gate 十问与证据化回答

1. **fio 是否真经 Sluice 执行 I/O？** 是。引擎 .so 链入 Sluice 静态库（`nm -D` 15 个 sluice
   定义符号，§E.4），提交走 `RequestScope::submit_read/write`（`queue.cpp:89-121`），strace 显示
   sluice-uring 仅 io_uring_enter、sluice-TP 仅 pread64（PERF-BASELINE.md §5），引擎 load 行与
   请求 backend 强校验一致（PERF-BASELINE.md:177）。无 POSIX 直通路径（未定义符号集里没有
   pread/pwrite 引用，§E.4）。
2. **TP/uring 是否都可用？** 是。正确性矩阵 threadpool 与 uring 双臂各 90/90 通过
   （`correctness.json`、`correctness-uring.json`，本次复验 summary 均 90/0/0）；noring 引擎 +
   uring 请求 fail closed 无回退（负控制实测 exit 77/exit 1，CORRECTNESS-MATRIX.md:10-11）。
3. **回调与 Sluice 生命周期是否完整兼容？** 是。queue 非阻塞提交（INV-02）、getevents 的
   take/poll_progress/wait_one 循环以 owner 身份驱动（PROG-01/02/04）、缓冲区借至 publication
   （REQ-04）、fd 只借不关（LIFE-02）、cleanup 经 finish() 显式结算、scope 先于 context 析构
   （SHUT-04）——逐条落点见 §C/§D 与 `adapters/fio/README.md` §2 的九条不变式。
4. **已接受 Request 是否可靠完成结算？** 是。每个 QUEUED 恰好 stage/交付一次（`events.cpp:168-170`
   入口断言 + 矩阵 tripwire 扫描"never consumed/still outstanding"零出现，CORRECTNESS-MATRIX.md
   §3）；健康失败 Tier 1 经 drain shutdown 按真实终态交付、Tier 2 terminate 不撒谎
   （`events.cpp:138-158`）；cleanup 臂（SIGTERM）实测 ≤236ms 干净退出。
5. **能否检测真实短 I/O/错误/超时/取消/shutdown？** 短 I/O：resid 如实换算 + EOF 边界 case +
   fio requeue（§C.3、CORRECTNESS-MATRIX.md §4.2）；错误：两规则 errno 映射 + 穷举单测 + TRIM
   EINVAL 负控制；超时：getevents 三形态全覆盖且到期不搁浅完成（`events.cpp:197-246`）；取消：
   引擎不实现 cancel（借出安全优先，fio 侧退化为阻塞排空，§D.5）；shutdown：两 tier 结算路径
   在（Tier 2 的进程终止路径未在正矩阵中触发——按设计，触发即 fail closed）。
6. **POSIX/fio io_uring/fio-sluice 同 workload 对照是否可信？** 有条件可信。同 randseed=81、
   同文件、同 runtime/亲和、A/A 划出噪声界（1.6%/8%）、F3 不可比性预先声明不混排、envelope
   对照明示非严格 A/B（PERF-BASELINE.md §2/§3/§4）。F3 行不用于跨引擎结论（§G.3）。
7. **adapter 是否成为主要瓶颈？** 非"阻塞型"瓶颈。适配+Sluice 层 ~5–6k 用户态指令/IO 是可归因
   的真实成本；未发现阻塞/重复遍历/拷贝特征；TP 后端的 futex 挥手是 Sluice 产品侧属性而非
   adapter 引入（§H）。
8. **E5 上能否稳定重复？** 读为主场景可以：除 F1/io_uring（11.0%）与 F2/sluice-uring（6.7%）外
   rep spread ≤3.7%，远小于引擎间差距；含引擎链路 A/A ≈8%（§G.2）。F3 受盘状态支配会漂移，
   已声明（§G.3）。
9. **是否具备引入真实应用 trace 的基础？** 具备载体、尚无 trace 机制。fio 生态（含本引擎）可
   作为统一四引擎执行 substrate；但当前负载仍是参数化合成分布，trace 采集/回放（如 fio 的
   read_iolog 路径）未实装未验证——这是下一阶段的候选工作（§K）。
10. **是否允许进入 #604 下一阶段？** 建议：**有条件允许（PHASE1_CONDITIONAL_GO）**。进入的是
    "G0 workload admission 准备"（真实消费者/代表性 trace 的采集与冻结），不是任何 G2 机制
    试验的放行；synthetic 基线（本阶段）不得作为 G0 准入本身（§K）。

---

## K. 与 #604 G0–G4 的关系

#604（research umbrella）的决策门（issue 正文 §6）：

| Gate | 要求 | 本阶段位置 |
| --- | --- | --- |
| **G0 Workload admission** | Real consumer or defensible representative trace；frozen oracle/dependencies、budgets、baseline | **未进入**。见下。 |
| G1 Cost attribution | 实际瓶颈与必要/可避免工作，竞争性解释 | 部分供料：四引擎基线 + 用户态指令/futex/syscall 归因（§H）只是 G1 的**方法预演**，对象是合成负载 |
| G2 Mechanism trial | 隔离 A/B + 因果反事实 | 未进入。本基线是观察性对照，不是机制试验 |
| G3 Transferability | 端到端 + 邻近 regime + 硬件敏感性 | 未进入 |
| G4 Product promotion | 为何进 Sluice vs 应用/adapter | 未进入；且本引擎的存在本身是"留在 adapter 层"的例证 |

**为什么 fio synthetic 本身不构成 G0 真实 workload：**

1. G0 要的是 **real consumer or defensible representative trace**——本阶段的负载是 fio 参数化
   合成分布（randread/randrw/seqread + 固定 randseed），没有真实消费者，也没有从真实应用冻结
   的 trace：无操作依赖结构（应用 I/O 间的因果/事务序）、无应用级 oracle（查询结果/WAL 恢复
   等可观察契约）、无应用资源预算与到达过程（fio 闭环到达是驱动器内生的，不是应用派生的）。
2. #604 的设计原则 1 是"workload first"：先冻结操作/依赖图、正确性与持久性 oracle、资源预算、
   分布与基线，再谈机制。F1–F4 冻结了**分布形状**，但这些形状不是从任何被承认的 workload
   family（W-A…W-F）的代表消费者派生的，只是通用 microbenchmark 形态。
3. 因此本阶段成果的正确定位是：**为 G0/G1 提供可比对、可归因的测量 substrate 与初始基线**
   （四个引擎在同一负载/文件/种子下可严格对照；adapter 层成本已归因），并在未来真实 trace 落地
   后（fio 引擎可执行任意 fio 作业，含 read_iolog 回放路径——该路径未实装验证，如实声明）作为
   统一执行面。**PR 与本文档都不声称 synthetic fio 基线等于或替代 G0 准入**；进入 G0 需要
   单独 workload admission（真实消费者或可辩护的代表性 trace + 冻结 oracle/预算）。
4. 与姊妹 issue 的边界：#578 拥有 fio + DuckDB R0 admission；#617 拥有内核边界研究；#603 拥有
   Direct Beyond-Parity P0–P6。本仓库的 fio 引擎是独立 bench 仓库的测量工具，不越权替代上述
   任何一者的门。

---

## Owner Gate 十问与建议判词（汇总）

问题清单与回答见 §J.2。三项门槛输入：coreGreen = **true**；性能门 = **BASELINE_VALID**；
独立终审 = **conditional**（无遗留 high 的前提下）。

**建议判词：`PHASE1_CONDITIONAL_GO`** —— fio-sluice 外部引擎构建、冒烟、正确性矩阵（90/90 ×
两种调用形态）与回归全部通过；四引擎性能基线有效（F1/F2/F4 可信、F3 环境受限已声明、A/A 划出
噪声界）；受限项（§I 全表：TRIM/verify_async/offload 不支持、fio 3.40 精确钉死、numjobs=1、
direct=1 未测、F3 不可排名、结果原件不入库）均已明示。放行的是"以本引擎为 substrate 推进
#604 G0 workload admission 准备"；不放行 synthetic 基线作为 G0 准入本身，不放行任何 G2 机制
试验。最终 Owner Gate 由人裁定。

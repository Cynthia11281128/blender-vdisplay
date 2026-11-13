# blenderctl 沙箱 Session 改动计划

日期：2026-11-05。状态：**已实现**（P0、P1；P2 `purge` 未做）。用法见 `README.md` 的“沙箱 session”。

实现与本计划的差异：

- spec 去掉 `env`：Blender 默认不读 `PYTHONPATH`，调用方经 `exec` 把自己的模块路径加入 `sys.path`。
- `/etc` 只绑定白名单文件（`passwd`、`group`、`ld.so.*`、`fonts`、`alternatives` 等），不整体绑定：`/etc/taiji` 对账号可读。
- 沙箱 session 的显示号固定为 `:99`（私有 `/tmp` 和 X 命名空间），不占用宿主显示号。
- 宿主 supervisor identity 不含 uid（降权发生在记录之后）。
- 新根的目录在 init 内以 umask 022 创建；继承的 077 会让降权后的账号无法遍历 `/etc`。
- 离线测试 `scripts/test_sandbox.py`，实机测试 `scripts/test_sandbox_live.py --run`（未并入 `test_sessions.py`，后者的测试根注入不支持 root 启动的沙箱）。

验收（2026-11-05，均通过）：现有 `test_manage.py`、`test_control.py` 不改动通过；`test_sessions.py --run` 通过；普通 session 启停、exec、screenshot 正常；`test_sandbox_live.py --run` 覆盖隔离探测、错误 generation、正常 stop、`kill -9` supervisor、Blender 崩溃，均无残留进程。启动约 7–15 s，停止约 0.4–0.6 s。

以下为原计划正文。
需求方：`assemble_code/eval_single_step_assemble`（见其 `PLAN.md`）。

## 1. 目标与边界

**要解决的问题。** 所有 session 共用 `blender-vdisplay` 账号，`control_server.py` 按账号放行 socket。在 Blender 里执行的不可信 Python 因此可以：

- 连接其它 session 的 control socket 并操控它（评测中可借预览 session 拿到候选装配图）；
- 读取账号可读的所有文件，包括 GT 和其它 run 的结果；
- 向同账号进程发信号、ptrace，访问网络和本机 loopback 端口。

**目标。** 新增可选的沙箱 session：Blender 只看得到白名单路径，看不到其它进程，也没有外部网络。

**不做的事。**

- 不做任意代码的强安全保证：不防内核漏洞，不限内存（agent 仍可把机器内存用满）。
- 不把审计、基线、违规判断放进 infra，这些属于评测项目。
- 不改协议 v1，不改 `control_server.py`。

**兼容性。** 不带新参数时，行为、参数、输出和协议完全不变。`blenderctl_simple_exec` 等现有使用方不受影响。

## 2. 改动清单

| 优先级 | 改动 | 说明 |
| --- | --- | --- |
| P0 | `start NAME --sandbox SPEC` | 整个 session 运行在 mount/pid/net/ipc/uts namespace 中（§3） |
| P1 | `start --cpu-count N` | 自动分配 N 个 CPU，默认 8；`--cpus` 优先。预览 session 用不到 8 个 |
| P1 | `stop NAME --generation TOKEN` | 在操作锁内比对 generation，不符则不发信号。解决 baseline 记录的“stop 与检查不原子” |
| P2 | `purge NAME` | 删除已停止 session 的 state/config/project/screenshots。现有 349 个目录，`projects/` 约 23 GB。本期不做 |

**此前建议的 `exec --kill-after` 取消。** 读完 `manage.py` 后发现不需要它：`supervise` 的 `terminate` 已经是 SIGTERM，8 秒后 SIGKILL，调用方在 `exec` 超时后执行 `stop`，等状态变为 STOPPED 即可确认进程已死。因此也不用改 `control_server.py`。

## 3. 沙箱设计

### 3.1 Spec

由调用方（root）提供 JSON 文件，校验后才启动：

```json
{
  "schema_version": 1,
  "read_only": ["/abs/path/to/task"],
  "read_write": ["/abs/path/to/output"],
  "tmpdir": "/abs/path/to/output/tmp",
  "env": {"PYTHONPATH": "/abs/path/to/harness"}
}
```

校验：路径必须是绝对路径且已存在，resolve 后不含符号链接，不得是 `/`，不得包含或位于 `STATE_ROOT`、`RUN_HOME`、`user-config`、`projects` 根目录之内（session 自己的目录由 manage.py 添加）；`read_write` 与 `read_only` 不重叠；`read_write` 和 `tmpdir` 须属于专用账号（不自动 chown）；`env` 不得覆盖 `Session.env()` 已有的键。未知键一律拒绝。

### 3.2 新根目录（白名单，失败时默认隐藏）

用 `pivot_root` 切换到新根，而不是逐个遮盖。本机有 87 个挂载点，遮盖式容易漏；白名单式没列出的路径根本不存在。

| 路径 | 方式 |
| --- | --- |
| `/usr`、`/etc` | 只读 bind（非递归，不带入子挂载） |
| `/bin`、`/sbin`、`/lib`、`/lib64` | 指向 `usr/` 的符号链接 |
| `blender-4.5.15-linux-x64/`、`scripts/` | 只读 bind，保持原绝对路径 |
| 本 session 的 state、runtime、config、project | 读写 bind，保持原绝对路径 |
| spec 的 `read_only` / `read_write` | 对应 bind，保持原绝对路径 |
| `STATE_ROOT/displays` | 账号可写的私有 tmpfs，使 `supervise` 的显示锁逻辑不用改 |
| `/proc` | 新挂载，只显示 namespace 内进程 |
| `/dev` | tmpfs 加 `null`、`zero`、`random`、`urandom`，私有 `/dev/shm` |
| `/tmp` | 私有 tmpfs（1777），放 X11 socket 和锁 |

`/etc` 含 ssh 主机密钥，其权限为 root only，账号读不到；实现时核对 `/etc` 下有没有账号可读的敏感文件（如 `/etc/taiji`），有则改为只绑定需要的文件。`/sys` 暂不提供，缺它若导致 Blender 异常再加。

构建根目录用 namespace 内私有的 tmpfs，挂在已有空目录上，不在宿主留下目录（原型在 `/run` 下建了目录并残留，已手工删除）。

### 3.3 进程结构

```text
root: blenderctl start NAME --sandbox SPEC
  ├─ 降权阶段：prepare、分配 CPU、写 session.json（allocation.lock 内，与现有一致）
  ├─ 写 STATE_ROOT/sandbox/NAME.json（root 所有，不进 namespace）
  └─ unshare --mount --pid --net --ipc --uts --fork --kill-child  [宿主可见的 root 进程]
       └─ sandbox_init.sh：建新根 → pivot_root → setpriv 降权 → exec manage.py --supervise NAME
            = namespace 内 pid 1（supervisor）
              ├─ Xvfb :99（namespace 私有，固定编号）
              └─ Blender + control_server
```

- 不用 tmux：supervisor 直接由 unshare 拉起。
- supervisor 是 pid 1，它一退出内核就杀光 namespace 内所有进程，agent 留下的子进程和线程都不会残留。作为 pid 1 还要回收被收养的僵尸进程，需要在 supervise 循环里加 `waitpid(-1, WNOHANG)`（只在沙箱模式）。
- `supervise`、`startup.py`、`control_server.py` 的主体不变：pid、路径、token 在 namespace 内部自洽。

### 3.4 宿主侧要改的地方（主要工作量）

`session.json` 里记录的是 namespace 内的 pid，宿主无法用它判断存活。沙箱 session 改为依据 `sandbox/NAME.json` 里 unshare 进程和 supervisor 的宿主 identity：

| 函数 | 沙箱模式下的行为 |
| --- | --- |
| `Session.status` | 宿主 identity 存活时读 `session.json` 的 status 映射状态；否则 STOPPED/FAILED |
| `used_cpus` | 经由 `status` 判断，不直接用内部 pid |
| `send_stop` / `stop` | root 向 supervisor 的宿主 pid 发 SIGTERM；超时 SIGKILL，再由宿主写 `stopped_at` 并清理 socket |
| `status --json` | 新增 `sandbox` 字段；`supervisor`/`blender`/`xvfb` 明确标为 namespace 内 pid |
| `main` | 沙箱 session 的 `start`/`stop` 必须 root，不降权后再操作 |
| `screenshot`、`attach`、非 JSON `status` 的 X 查询 | 沙箱 session 明确报错（宿主无法访问其 X，也没有 tmux） |
| `list` | 正常显示，增加沙箱标记 |

agent 在 namespace 内能改写 `session.json`、`ready.json`（同账号）。所以存活判断只信 `sandbox/NAME.json`；`session.json` 被改的最坏结果是状态或 generation 不对，调用方会得到 `generation_mismatch` 或连接失败，并按 harness 失败停止。

### 3.5 环境变量

沙箱 session 设 `TMPDIR/TMP/TEMP` 为 spec 的 `tmpdir`，再合并 spec 的 `env`。普通 session 的环境不变：`Session.env()` 现在没有 `TMPDIR` 且用 `env -i` 启动，Blender 默认用 `/tmp`。是否也为普通 session 改掉，另行决定。

### 3.6 隔离效果

| 通道 | 结果 |
| --- | --- |
| 其它 session 的 control socket / state / project | 路径不存在 |
| GT、数据集、其它 run、其它共享盘 | 路径不存在 |
| 信号、ptrace、`/proc/<pid>/root` | pid namespace 内看不到外部进程 |
| 外网、宿主 loopback（含评测 MCP 端口）、抽象 socket | net namespace 只有 `lo` |
| SysV IPC | ipc namespace |
| 内存耗尽 | **不防**（无 cgroup 限制） |
| CPU | 继承亲和性；agent 可在 namespace 内改亲和性，**不防** |
| 内核漏洞 | **不防**；与宿主共用内核 |
| 只读 bind 内容 | 可读，这是有意暴露的（如 task 的 parts） |

## 4. 已验证与未验证

2026-11-05 在本机用临时 namespace 做过原型（`unshare` + `pivot_root` + `setpriv`，全部临时，未改 infra）：

| 项 | 结果 |
| --- | --- |
| 创建 mount/pid/net/ipc/uts namespace、`pivot_root`、降权到 uid 992 | 通过 |
| 内部 pid=1、只有 namespace 内进程、网络只有 `lo` | 通过 |
| 数据集、`/var/lib/blender-vdisplay`、`/jizhicfs` 不可见 | 通过 |
| 宿主 root 经 bind 目录连接内部 Unix socket，内部 `SO_PEERCRED` 看到 uid 0 | 通过（现有放行逻辑可用） |
| 内部 Xvfb + `glxinfo`：llvmpipe | 通过 |
| 内部 `blender --background`：bpy 4.5.15、numpy 1.26.4、Python 3.11，`tempfile` 用 TMPDIR | 通过 |
| 内部 GUI Blender 打开窗口并运行 timer | 通过 |

未验证：

- 走真实 `manage.py` 流程，包括 supervisor 作为 pid 1、status/stop 映射。
- `startup.py` 与 control server 在 namespace 内运行，以及宿主 `control_client.execute`。
- GLB 导入、视口渲染（`bpy.ops.render.opengl`）在 namespace 内是否正常。
- 缺少 `/sys`、`/run`、字体等路径的影响。
- 启动、停止耗时。原型未测量。
- apdcephfs 上大量 bind 的行为和并发启动。

原型用 `--map-root-user` 的 user namespace 方案未采用：内部 uid 变成 0 时，宿主 root 不在映射内，按内核规则会被 `SO_PEERCRED` 看成 overflow uid 而遭拒（此点是推断，未实测）。

## 5. 文件改动

| 文件 | 改动 |
| --- | --- |
| `scripts/sandbox.py`（新） | spec 校验、根目录与挂载列表生成（可离线测试） |
| `scripts/sandbox_init.sh`（新） | namespace 内建根、`pivot_root`、降权 |
| `scripts/manage.py` | §3.4 的分支、`--sandbox`、`--cpu-count`、`stop --generation`、pid 1 回收 |
| `scripts/test_manage.py`、`scripts/test_sandbox.py`（新） | 离线单元测试 |
| `scripts/test_sessions.py` | 增加沙箱实机验收（`--run` 才运行） |
| `README.md` | 新增沙箱章节和限制 |
| `docs/infra/blender_deployment_report.md` | 记录依赖（`pivot_root`、`unshare --kill-child`，本机 util-linux 2.32.1） |

`manage.py` 要保持系统 Python 3.6 兼容（`/usr/bin/python3` 为 3.6.8）。

## 6. 步骤与验收

1. **离线**：`sandbox.py` 与 `manage.py` 分支，单元测试；现有 `test_manage.py`、`test_control.py` 不改动并通过。
2. **实机启停**（用 `esa-` 前缀临时 session）：start → status → exec → stop；加 `kill -9` supervisor、Blender 崩溃两种情况。
3. **隔离验收**：在沙箱 session 内 exec 探测：其它 session socket、数据集路径、外网、宿主 MCP 端口、外部进程均不可达；`read_only` 不可写。
4. **回归**：普通 session 走现有 `test_sessions.py --run`，并跑一次 `blenderctl_simple_exec` 的启停路径。
5. **测量**：启动和停止耗时，写入 README（评测项目不做违规回滚，任何错误或违规都终止 task，此数值只影响单次 run 的启动开销和并发安排）。

验收条件：

- 不带新参数时行为不变（第 1、4 步）。
- 所有 §3.6 中声明“不可达”的通道都有探测用例。
- 任何退出路径后，宿主无残留进程，`/run`、`/tmp` 无本次残留（Xvfb 文件在私有 tmpfs 中）。
- `status`/`stop` 不会对身份不符的 pid 发信号。

## 7. 风险

- **运行中的 session：** manage.py 改动不影响已启动的 session，但 `status`/`stop` 读同一份 metadata，普通 session 的路径必须严格不变。
- **依赖容器权限：** root 拥有 `CAP_SYS_ADMIN` 才能建 mount namespace，本机可以；容器重建后权限若变化，`--sandbox` 应启动时明确报错，不退化为非沙箱。
- **提交：** infra 是独立 git 仓库（当前工作区干净，本文件除外）；改完不自动提交。

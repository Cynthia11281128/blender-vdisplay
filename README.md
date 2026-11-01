# blender-vdisplay

当前容器的管理入口为 `blenderctl`。共享一份官方 Blender 程序，
每个具名 session 有独立 Xvfb、Blender 进程、认证、状态、配置和项目目录。
正常 GUI 模式使用 LLVMpipe 软件 OpenGL，不使用 GPU 渲染。

在已完成部署的容器中，从本仓库根目录的 root 终端执行：

```bash
./blenderctl start A
./blenderctl start B
./blenderctl list
./blenderctl status A
./blenderctl status A --json
./blenderctl exec A --code 'print(bpy.app.version_string)'
./blenderctl exec A --script /absolute/path/to/task.py --timeout 60
./blenderctl screenshot B
```

停止前先保存场景；下面命令不自动保存 `.blend`：

```bash
./blenderctl stop A
```

不带名称时操作 `default`，例如 `./blenderctl start`。
重复启动同一个名称不会新建实例或更改它的参数。
名称区分大小写，允许 1-32 个字母、数字、下划线、短横线，首字符须为字母或数字。

默认给每个新 session 分配当前允许 CPU 中未被其它受管理实例使用的
8 个逻辑 CPU；不是检查系统负载，也不是独占 CPU。少于 8 个时拒绝自动分配。
也可显式指定：

```bash
./blenderctl start A --cpus 0-7
./blenderctl start B --cpus 8-15
```

显式指定的 CPU 必须在当前允许范围内，与其它实例重叠时提示但允许启动。
要修改运行中实例的 CPU 设置，先保存场景、停止，再带新参数启动。
显示编号在 `:99` 到 `:999` 自动分配，停止后可以被其它实例复用。

代码：

- [顶层 blenderctl](../../blenderctl)：简短入口，转发参数到本目录的管理脚本。
- [blenderctl](blenderctl)：调用系统 Python 的轻量入口。
- [manage.py](scripts/manage.py)：参数解析、资源分配、状态管理、进程监管及清理。
- [session.sh](scripts/session.sh)：独立 tmux 会话的 supervisor 启动入口。
- [startup.py](scripts/startup.py)：初始化并记录 Blender GUI 状态。

配置目录为 `user-config/<session>/`，可写项目目录为 `projects/<session>/`。
Blender 以该项目目录作为工作目录启动；不会自动保存场景。
运行日志及私有 X11 认证在
`/var/lib/blender-vdisplay/.local/state/blender-virtual/sessions/<session>/`。
截图发布到仓库内的 `screenshots/<session>/`，文件名带随机后缀避免覆盖。
停止保留配置、项目、截图和日志，清理进程、X11 cookie、ready 快照和显示资源。
停止也清理本 session 的 `control.sock`。
`list` 包含已停止实例；目前没有删除实例的命令。

旧单实例配置与日志保留原位置，不自动迁入 `default`。
所有 session 共用 `blender-vdisplay` 账号，这不是多用户安全隔离。

## 本机程序控制

`start` 等待 GUI 和控制入口均就绪。升级前已经运行的实例没有控制入口时，
重复 `start` 会明确报错；保存后显式 `stop` / `start`，不会隐式替换进程。
`status A --json` stdout 仅输出一份 JSON，诊断写 stderr，不运行 GUI 查询工具。
字段含 `session`、`state`、`generation`、`display`、`control`、进程身份和 CPU。
`control` 含 `protocol_version`、`socket_path`、`ready`；没有 X11 cookie 内容。
运行状态退出码为 0，停止、失败、不存在等非运行状态为 1，仍输出有效 JSON。
`control.ready` 是当前代次入口初始化快照，不是场景监控，也不保证主线程空闲。
停止/不存在时为 false。重启同名实例后 generation 改变，客户端必须重新查询。

`exec` 的 `--code` / `--script` 必须二选一。脚本按调用方路径读取 UTF-8 内容，
传给现有 Blender 进程，不在宿主执行，不启动第二个 Blender。
root 客户端读取私有脚本并连接 socket，但代码始终以专用账号执行。
compile 使用来源绝对路径作 filename；不切换 cwd 或增加脚本目录到 sys.path。
数据文件仍必须可被 Blender 账号访问，相对路径按 `projects/<session>/` 解析；
不会自动 chmod/chown 外部数据。代码执行失败也可能已修改场景，不回滚。

所有 `bpy` 操作在 Blender 主线程执行。namespace 持久，预置 `bpy` 和
`__name__ = "__blenderctl__"`，变量与函数可在后续请求使用。
每次先移除旧 `result`；赋值 `result` 返回 JSON 值，未赋值返回 null。
不清空、保存或重载场景。`open_mainfile` / `read_factory_settings` 后
控制 timer 和 namespace 继续存在，但旧 bpy 数据引用可能失效。
默认使用当前 GUI 上下文，依赖 editor 的 operator 需自行 `bpy.context.temp_override`。
捕获 `SystemExit` 等 Python 异常；不防护 `os._exit`、进程崩溃或无限循环。

stdout 是一份 JSON 响应，含版本、请求 ID、generation、ok、stdout/stderr、
result 和 duration_ns；Python 异常包含错误码、类型、消息与 traceback。
成功退出 0；执行错误、busy、超时、连接失败等非零，客户端诊断进入 stderr。
每 session 最多一个已接受未完成请求，第二个返回 `busy`，不排队，不自动重试。
不同 session 可并行；程序串行化不与人工 GUI 操作互斥，同 session 一次由一个操作者负责。

`--timeout` 默认 30 秒，须为有限正数，是客户端等待期限，不是脚本执行时限。
尚未开始且已确认断开的请求会取消；已开始请求继续执行并占用名额直到结束。
传输失败或等待超时报告“结果未知，可能仍在执行或已经完成”，不承诺取消。
不得盲目重发修改请求；先检查场景，必要时显式 `stop`，该命令仍不保存。
长脚本不会持有管理操作锁，因此 `stop` 不必等脚本正常结束。
request ID 用于匹配响应，不提供去重或 exactly-once 保证。

传输是私有 Unix socket（runtime 700、socket 0600），Linux `SO_PEERCRED`
仅接受 root 或专用账号；不监听 TCP。generation 是代次标识，不是安全凭据。
这是受信任代码执行入口，不是沙箱，具有 Blender 账号的全部可用权限。
每连接一行 UTF-8 JSON 请求/响应然后关闭。协议 v1 仅支持 `exec`。
代码上限 1 MiB，请求帧 2 MiB，响应帧 4 MiB（含换行），stdout/stderr 共享 1 MiB。
结果须能序列化为 JSON，拒绝 NaN/Infinity、循环引用和超过 128 层的嵌套。
输出/响应超限也不回滚副作用；错误码包括 `invalid_request`、
`generation_mismatch`、`busy`、`execution_error`、`result_not_serializable`、
`response_too_large`。接收帧期限 5 秒，发送响应期限 5 秒，每 session
最多 8 个连接处理线程；超额连接或未授权连接直接关闭。
请求日志记录 ID、代次摘要、开始/结束、耗时、错误码，不重复记录代码或输出。

小型 Python 客户端（不依赖 bpy，兼容 Python 3.6）：

```python
import sys
sys.path.insert(0, "/absolute/path/to/blender-vdisplay/scripts")
from control_client import execute

response = execute(socket_path, generation, "result = len(bpy.data.objects)",
                   timeout=30, filename="<application>")
```

该函数返回同一响应对象；传输错误抛 `ControlClientError`。
路径与 generation 从 `status --json` 取得。客户端和服务共用 `control_protocol.py`。
`screenshot` 仍是 Xvfb 整桌面截图，不是指定角度视口截图；
视口图可经 `exec` 使用明确上下文调用 `bpy.ops.render.opengl`。
`attach` 仍只连接 tmux 终端，不是远程桌面。

测试：

```bash
/usr/bin/python3 -I -B scripts/test_manage.py
/usr/bin/python3 -I -B scripts/test_control.py
# 下面是实际 GUI 验收，会短暂启动两个临时实例，最后停止它们。
/usr/bin/python3 -I -B scripts/test_sessions.py --run
```

测试临时目录显式使用 workspace `tmp`，真实测试仅在自己的随机名称和短路径下
创建状态、项目、日志、场景与截图，定向设置该子目录的专用账号 owner。
测试结果保留在 `tmp/bc-*/results.json`，不写生产 artifacts。
Xvfb 仍短暂创建系统 `/tmp/.X11-unix` socket 和显示锁，它们是 X11 运行资源，
不是测试临时文件；finally 验证本次资源和进程已清理。

安装和验收记录、实际路径及容器重建注意事项见
[部署报告](../../docs/infra/blender_deployment_report.md)。
本目录不是自动安装器；`start` 不安装依赖、不创建账号、不恢复重建的容器。
没有配置远程桌面、MCP、Computer Use、GPU 渲染或自动重启。

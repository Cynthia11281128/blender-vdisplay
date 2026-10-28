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
`list` 包含已停止实例；目前没有删除实例的命令。

旧单实例配置与日志保留原位置，不自动迁入 `default`。
所有 session 共用 `blender-vdisplay` 账号，这不是多用户安全隔离。

测试：

```bash
/usr/bin/python3 -I scripts/test_manage.py
# 下面是实际 GUI 验收，会短暂启动两个临时实例，最后停止它们。
/usr/bin/python3 -I scripts/test_sessions.py
```

安装和验收记录、实际路径及容器重建注意事项见
[部署报告](../../docs/infra/blender_deployment_report.md)。
本目录不是自动安装器；`start` 不安装依赖、不创建账号、不恢复重建的容器。
没有配置远程桌面、MCP、Computer Use、GPU 渲染或自动重启。

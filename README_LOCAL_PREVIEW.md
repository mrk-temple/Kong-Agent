# Kong Local Preview 0.0.0

这是 Kong 的浏览器、命令行和 Textual 本地预览，共用 Python Runtime `0.5.0rc1` 与独立 Memory Core。

PowerShell 启动：

```powershell
.\scripts\start-local-preview.ps1
```

Python 3.12+ 与 uv 必需；首次安装需要网络。内置前端无需单独开发服务。按 Ctrl+C 停止；TUI 用 Ctrl+Q 退出。

其他入口：

```powershell
.\scripts\start-local-preview.ps1 -Action chat
.\scripts\start-local-preview.ps1 -Action tui
.\scripts\start-local-preview.ps1 -Action memory
```

模型设置、保存授权、运行恢复和同服务连接方式见 [README](README.md)、[用户指南](USER_GUIDE.md) 和 [记忆指南](MEMORY_GUIDE.md)。当前发布状态与已知限制见 [版本说明](RELEASE_NOTES.md)。

# 用户指南

## 第一次使用

按 [README](README.md) 启动。浏览器配对链接授权当前本地进程；重启服务后使用新的链接。模型设置支持 Provider、Base URL、Model ID 和密码输入。密钥保留在后端进程中，主动连接检查会发送一次真实模型请求。

默认数据目录为项目 `.local-preview/`，包括 `core.db`、`application.db`、`threads.db`、Run 快照与任务 `workspace/`。启动脚本可用 `-DataRoot` 指定另一个目录；它不会自动导入旧跨项目 Memory DB。

## 对话与文件任务

“聊天”用于一般问答与多轮对话。“完成任务”需要确认验收文件及准确完整内容；缺少强条件时会在调用模型前拒绝。界面分别显示“答复结束”“任务已完成验收”和“等待你的回应”。

FAST / AUTO / PLAN 作用于新 Run。PLAN 中审阅计划和具体修订，再批准；旧修订不能沿用。可以讨论、拒绝、补充约束、更换目标或停止。停止提交真实取消，硬预算不会因恢复而重置。

记忆发生变更后，旧 Run 的来源可能被隔离；输入完整新目标创建 fresh Run，旧计划和证据不会自动迁入。只有模型生成的保存话语时，请查看 Core 是否已经接受保存。

## 四个空间

对话展示接受的消息、真实工具结果、审批与状态。检查面板展示 Run、用量和记忆请求轨迹。记忆空间展示 Core 记录与记忆瓶；工作区展示任务目录中的实际文件，支持 256KB 以内 UTF-8 文本预览。活动列表展示经过筛选的事件元数据。

## CUI 与 TUI

CUI 对话内使用 `/help`、`/mode fast|auto|plan`、`/memory`、`/remember 正文`、`/status`、`/fresh 完整目标`、`/approve 修订`、`/reject`、`/answer 正文`、`/resume` 和 `/resolve 实际结果`。

TUI：F4 / Ctrl+Enter 发送；F6 审阅计划；Ctrl+K 停止；Ctrl+R 恢复；Ctrl+I 记忆；F7 检查未知动作；F8 模型设置；Ctrl+N 新对话；F1 帮助；Ctrl+Q 退出。

同一个数据目录只启动一个服务进程。连接已有服务时，在自己的终端设置启动链接中的短期会话 token，再启动客户端：

```powershell
$env:KONG_LOCAL_TOKEN = Read-Host '当前本地服务的短期会话 token'
uv run --frozen --extra local --extra tui kong local chat --connect http://127.0.0.1:8765
```

地址中的端口以当前服务输出为准。将 `chat` 替换为 `tui` 可连接终端界面。会话 token 与模型 API Key 是两个独立值。

## 中断、恢复与备份

连接中断后先读取状态和命令回执。已知动作中断的 Run 等待显式恢复；未知动作需要先检查实际文件、说明结果并确认解决。应用不会自动重发未知写入请求。

备份前关闭全部使用该数据目录的服务，再完整复制数据目录。恢复时保留原 workspace 位置；跨路径迁移尚未完成完整验收。Core 遗忘的范围见 [记忆指南](MEMORY_GUIDE.md)，Runtime 快照语义见 [RECOVERY](RECOVERY.md)。

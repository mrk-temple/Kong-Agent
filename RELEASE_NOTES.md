# Kong Local Preview 0.0.0 / Runtime 0.5.0rc1

2026-10-13 · 预发布候选 · MIT

本次把最新本地实现整理为可获取的源码和 Python 安装包，延续已有公开 Git 历史。产品标识为 Local Preview `0.0.0`，Python 包版本为 `0.5.0rc1`；两个标识对应不同层级。

## 交付内容

- 浏览器 GUI、独立 CUI 和 Textual TUI，共用 Application Service、Runtime 与 Memory Core。
- 明确保存、更正、来源查看、记忆瓶与遗忘预览；真实任务审批、取消、恢复和文件验收。
- Windows 启动脚本、React / TypeScript 前端源码、构建资源、依赖锁、测试和内置技能。
- 重写 README，补充公开用户、记忆和开发指南；内部记录、运行数据与凭据继续在本地保留。
- 补齐 sdist 与源码快照中的前端、启动脚本和指南；排除依赖目录、数据库和生成缓存。
- 消除本地启动对某台机器绝对目录的依赖，临时文件和缓存跟随指定数据目录；源码身份工具可以从公开源码独立运行。
- 发布核验增加 GUI 资源、TUI 和本地状态的源码目录外安装检查；CI 增加前端测试和构建一致性检查。

## 验证口径

本次重新进行离线工程、前端、固定发布回归、包扫描和独立安装。结果见 [EVALUATION](EVALUATION.md) 与 [PUBLIC_RELEASE_CHECKLIST](PUBLIC_RELEASE_CHECKLIST.md)。本次不增加收费模型调用。

此前 Local Preview 实模样本观察到保存、回忆、更正、遗忘和文件任务流程可运行，也观察到格式遵循、保存话语和计划步骤证据问题。该样本属于先前源码身份，不能用作本次修整后的完整模型验收。

## 当前限制

主要面向 Windows 本地预览；真人长期使用、新机器、Linux 产品交互和真实强工具隔离尚无完整接受证据。Identity / Relationship 演进界面与 TTL 定时物理维护尚未纳入完整产品验收。

Core 遗忘不擦除旧 Runtime 历史、已外发请求、导出或备份。结果未知的动作先检查实际效果并明确解决，恢复不会自动重放。

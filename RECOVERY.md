# RECOVERY — Kong-Agent 0.5.0rc1 的持久化与恢复

对照 `src/kong/storage.py`、`src/kong/runtime/loop.py`、`src/kong/cli.py` 与 `src/kong/reporting.py`。设计原则：**结果未知的动作永不自动重放**，恢复先把状态交还人，而不是猜。

## Local Preview 的恢复与数据

本地 GUI / CUI / TUI 的 Application Service 保存命令回执、事件水位和 Runtime 快照。重启后已知动作的运行暂停，需显式 resume；未知动作仍先检查并 resolve。SSE 重连读取状态，不重发旧 POST 或自动重放写入。

默认数据目录为项目 `.local-preview/`，包含 `core.db`、`application.db`、`threads.db`、Run 数据和 `workspace/`。启动脚本可用 `-DataRoot` 指定位置，临时文件与缓存跟随这个目录。一个数据目录只运行一个服务进程。

关闭全部关联服务后完整复制数据目录进行备份；当前没有完成跨路径 workspace 迁移验收。Memory Core 的遗忘范围不包括 Runtime 历史、外发模型请求、导出和备份。具体入口见 [用户指南](USER_GUIDE.md) 和 [记忆指南](MEMORY_GUIDE.md)。

独立 Memory Core 绑定的 Run 使用后续快照格式保存 Profile、作用域和删除时代等身份；恢复时重新验证当前授权，不从旧快照恢复已经失效的权限。下面保留 Runtime 各版本的持久化规则。

RC5 沿用 RC3 的增量快照字段，不重置累计预算。旧快照缺少 `completion_scope` 时采用默认声明验收；运行中的弱条件任务不能靠恢复获得机械完成权限。新快照保留调用方明确选择的范围，恢复入口不能覆盖它。旧 `progress_count` 中的新输出积分保留为活动计数并在检查点消耗，不能直接支持新续租；声明来源范围和已验证条件积分随新快照保存。同步核验缓存不保存，不跨工具、模型等待或恢复使用。

## 持久化点

V0.5 的默认连续性策略仍写 schema 4，不添加空策略字段；读入 schema 1–4 时保留原有默认行为。显式 M0/M2 或其他非默认 ContinuityPolicy 写 schema 5 和完整 `continuity_policy`。schema 5 缺策略、旧 schema 附带新策略、恢复时提供冲突的模式或 ContextEngine 都拒绝；新进程采用已保存策略，不把 M0 恢复成 M1。上下文预算仍由当前明确的模型配置提供，权限仍须本次启动重新声明。

schema 5 不能由旧客户端读取。升级或实验前备份 RunStore 和 `.kong/context.db`；要回退到旧程序，使用保留的旧格式副本，先核对实际产物和副作用，不改写版本号伪造迁移。不把未知效果自动重放，也不因切换代码恢复旧批准或预算。

技能生命周期保存在独立 `.kong/skill-lifecycle.json`（schema 1），宿主变更持有文件锁并原子写入。正文及引用资源绑定同一版本哈希；历史已加载材料仍固定为原版本，改变磁盘文件不能拼入同一 Run。停用或删除受管理版本的启用记录后，下一次上下文编译撤下其材料并说明原因。选中已验证旧身份只改选择记录，宿主仍须恢复对应旧文件；外部写入不能被该记录撤销。

| 时机 | 内容 |
|---|---|
| 每轮开始（模型请求前，含失败的 HTTP 请求） | 完整 `Snapshot`：状态、历史、`pending_action_id`、指标 |
| 每个动作执行前 | `action_intent` 事件 + 快照落盘——**先记账再执行** |
| 每个动作结束后 | `observation` 事件、进展门检查、快照落盘 |
| 每次状态迁移与人工命令后 | `feedback` / `human` 事件 + 快照落盘 |
| 运行退出（含异常路径） | 最终快照 + `.kong/reports/<run_id>.json` 报告 |

- 快照路径：`<workspace>/.kong/runs/<run_id>.json`；报告：`.kong/reports/<run_id>.json`；线程与记忆：`.kong/context.db`。
- 写入是原子的：临时文件 → fsync → `os.replace`；同一运行持有 `.lock` 文件锁，进程死亡后锁自动失效。
- 每个动作 ID 是按历史中全部 `action_intent` 递增的 `a{N}`，崩溃恢复不会复用证据 ID。

## 中断后的恢复流程

```powershell
uv run kong runs                  # 找到 run_id 与状态
uv run kong show <run_id>         # 查看快照，不调用模型
uv run kong resume <run_id>       # 恢复
```

`Runtime.restore` 检查并执行：

1. 必须使用**原工作区**；线程 ID 不可覆盖；项目记忆作用域按原值恢复；
2. 线程元数据丢失时从快照重建 SQLite 记录；
3. 追加一条 `feedback` 列出 `unavailable_resources`：历史中的终端会话、浏览器页面、MCP 连接都**不存在了**，并提示不要重放旧句柄；
4. 若存在 `pending_action_id` 或 `pending_action_ids`：进入 `waiting_user`，提示上次动作结果未知，使用 `/resolve` 逐项描述实际结果；不会自动重放动作。

## 结果未知的动作与 `/resolve`

动作流程是 `action_intent`（落盘）→ 执行 → `observation`。若进程在“落盘后、观察前”死亡，恢复时该动作永远没有结果：

- 运行停在 `waiting_user`，`pending_action_id` 非空；
- 此时**任何**后续命令都被拒绝：“Resolve the interrupted action with /resolve before continuing”；
- 人工检查工作区实际效果（文件是否写入、写到什么程度）后，在恢复后的交互提示中输入：

```text
/resolve 描述实际发生的结果
```

（说明必填。）每次 `/resolve` 记录一条 `human` 事件并清除当前 ID；仍有其他 `pending_action_ids` 时继续暂停，逐项处理完毕后按“人工回答”给一次有界恢复租约。只有之后模型重新提案、重新过授权与完成门，任务才会继续或结束。诊断期间的任何动作都不会替你回答这个问题——不自动重放、不推断结果。

## V0.4B 输入、取消与重启

schema 4 继续保存 `queued_inputs`、`constraints`、`run_events` 和 `pending_action_ids`，并新增独立 `cancel_input` / `cancel_applied`；读取兼容 schema 1/2/3。取消不占用最多 32 条普通输入的队列，重复取消返回同一请求 ID。输入队列与其产生的目标/状态/审计变更整体写盘后才通知 `input_applied`，重启不会丢失已经确认接收的队列。事件序号在同一次运行恢复后继续递增。运行中 `/add` / `/goal` 撤销尚未开始的旧动作，并使已有计划批准失效；已开始的动作不会因为补充约束自动回滚。

`/cancel` 立即取消模型等待，阻止新动作；普通已开始文件写入先收尾，进程、终端、浏览器与 MCP 等可管理动作取消后仍需记录未知副作用。取消状态为 `stopped` / `user_interrupted`，终端事件为 `cancelled`。未知列表可与取消状态并存，恢复先转为暂停核查，禁止自动重放。文件只读并发批次收齐已开始结果，随后停止。Docker 超时/取消清理容器与子进程，已生成文件仍可能存在，清理成功不代表业务操作未发生。

取消后的 `/resolve` 仅核查未知效果，处理完仍保持停止；通过明确 `/resume` 才继续，预算不重置。已经分配 ID 但尚未派发的撤销动作记为 `dispatched:false`，既不执行也不误算为未知执行时间。绑定的 code/data 合同和保护源初始版本进入快照，恢复不重新基线；新目标或约束使旧合同失效，需要调用方更新后明确继续。

可重试模型故障的恢复次数保存在运行事件中，重启不刷新上限；模型/工具预算也不重置。恢复时重新提供权限文件与启用开关，新的完整策略和指纹进入审计。缺失 Docker、镜像或隔离配置时直接失败，不回退到宿主执行。

## 不会恢复的外部句柄

- 终端会话、浏览器页面、MCP 连接：**不持久化存活句柄**；CLI 退出即清理，恢复后需重新建立（模型会收到不可用清单）。
- 硬预算（`hard_turn_limit`）在恢复后**不重置**；有界恢复租约只在出现真实新进展或人工回答时发放，不会凭空续期。
- 旧快照若缺指标字段，恢复后标 `legacy_unknown`（用量显示“未知”），不伪造数字。
- 被 `--allow-process` 等开关启用的能力，恢复时必须**再次显式携带**相应开关。

## 可复现的验证步骤

离线、无模型、无密钥——注入一个“执行中断、结果未知”的动作并验证恢复语义：

```powershell
uv run python examples/kong_eval.py --cases unknown_action_restore --repetitions 1
```

verifier（Runtime 之外）核对：状态为 `waiting_user` 且 `pending_action_id` 存在、**零重放**（观察数为 0 且 `canary.txt` 不存在）、模型调用为 0、保护文件哈希不变。同一机制由单元测试 `tests/test_runtime.py::test_unknown_inflight_action_never_replayed` 与 `tests/test_evaluation.py` 覆盖。

计划批准边界的离线验证同理：

```powershell
uv run python examples/kong_eval.py --cases plan_approval --repetitions 1
```

预期状态 `expected_approval`：计划停在待批准修订，**零环境动作**、无产物文件。

## 未覆盖与限制

- 恢复验证覆盖“状态与决策语义”，不验证外部世界的副作用是否被撤销——这正是 `/resolve` 要求人工核查的原因。
- 文件工具读写限于 UTF-8 文本与 256 KB；超出范围的中断效果无法通过快照回滚。
- 不做操作系统级事务：已执行的本机命令、已发出的网页请求不会因恢复而撤销。

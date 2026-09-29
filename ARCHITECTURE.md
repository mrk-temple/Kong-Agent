# ARCHITECTURE — Kong-Agent 0.3.0

本文描述当前代码结构与控制闭环，节点均对照 `src/kong/` 实际实现；能力限制与验证方式见 [README](README.md) 与 [PUBLIC_RELEASE_CHECKLIST](PUBLIC_RELEASE_CHECKLIST.md)。行号会随修改漂移，引用以模块与符号名为准。

## 模块地图

| 模块 | 职责 |
|---|---|
| `kong/runtime/` | 唯一执行循环与权限边界：`loop.py`（循环）、`controller.py`（授权与状态迁移）、`gates.py`（进展门与完成门）、`policy.py`（模式预算）、`state.py`（运行状态构造） |
| `kong/contracts.py` | 决策、动作、观察、事件、状态、迁移、验收条件等全部数据契约 |
| `kong/tools/` | 工具契约（`Tool` / `ToolResult`）、注册表、参数校验、`ToolExecutor` 与内置文件工具 |
| `kong/models/` | 模型协议 `Model.generate(context) -> Decision`、Chat Completions 兼容实现、离线 `ScriptedModel` |
| `kong/storage.py` | 原子快照与任务报告；控制状态与审计历史是分开的契约 |
| `kong/continuity/` | 上下文编译与派生记忆（Thread、来源记忆、压缩召回）；不含额外模型调用，不扩大 Runtime 权限 |
| `kong/integrations/` | 进程内交互工具（ConPTY 终端、Chromium 浏览器、MCP）；不持久化存活句柄 |
| `kong/environments/` | 执行后端（本地进程等）；本地进程不是操作系统沙箱 |
| `kong/skills/` | 指令包目录与按需加载；加载技能不授予执行权限 |
| `kong/web/` | 与模型厂商无关的公开网页检索 |
| `kong/evaluation/` | 题目定义、离线夹具、独立 verifier、套件运行器 |
| `kong/cli.py` | 终端客户端：子命令与交互 `/` 命令，与 Runtime 共用同一套 controller 契约 |

## 决策协议

模型每轮必须返回恰好一个 `Decision`（`contracts.py`）：

- `respond` — `FinalResponse` 完成提案，须过完成门；
- `act` — `Act` 携带一个或多个动作，授权后执行；
- `discover` — `NeedDiscovery` 只读发现类批量（只允许 `read_only` 工具）；
- `plan` — `PlanProposal` 提交计划修订；
- `ask` — `NeedUserInput` 暂停等待人工输入；
- `complete_step` / `assess` — 计划步骤完成与进展评估。

模型响应违反 Schema 抛 `ModelProtocolError`，**该轮不执行任何动作**。循环（`runtime/loop.py`）对每个决策记录 `decision` 事件 → 授权 → 被拒则写 `feedback` 事件并 `checkpoint` → 通过则执行或迁移，所有迁移再记为 `feedback` 事件。纯技能批量（每个动作都由宿主技能工具处理）可绕过执行门直接运行；混合技能与环境动作的批量被整体拒绝。

角色区分：模型只产生 `decision` / `model_error` 事件；目标、`/approve`、`/resolve`、回答等只来自 `human` 事件；工具输出只进入 `action_intent` / `observation` 事件，永远不构成对话轮次。

`Phase`：decide → execute → assess → approval → discuss → finished；`Status`：running / waiting_user / completed / stopped / failed。

## 策略与预算

`runtime/policy.py` 的 `ModePolicy`：

| 模式 | 租约 / 硬上限（模型调用轮） | 计划 |
|---|---|---|
| FAST | 4 / 4 | 禁止 |
| AUTO | 4 / 40 | 允许 |
| PLAN | 6 / 60 | 必须，批准前不执行环境动作 |

- 硬上限触发 `STOPPED`（`hard_turn_limit` / `fast_limit_suggest_auto`），恢复**不重置**硬预算。
- 租约耗尽触发进展升级（进入 assess 或恢复租约），不是终止；恢复租约有界：无新进展则暂停要求人工。
- 工具执行超时默认 30 秒（`tools/executor.py`），可按工具覆盖。
- 上下文 token 预算来自模型配置；授权本身没有美元/token 限额。

## 授权边界

`RuntimeController.authorize`（`runtime/controller.py`）：“模型决策不直接改控制状态。”规则：

1. FAST 禁止 `PlanProposal`；
2. 等待计划批准或讨论阶段只接受计划提案、提问、完成提案；
3. PLAN 模式要求 `approved_revision == revision`，完成提案也不能绕过批准；模型自带的批准/完成标志被 propose 清除，**只有人工 `/approve <修订号>` 有效**；
4. 评估阶段只接受评估、提案、提问或有证据支撑的完成；
5. 读写区分：仅 `list_dir` / `read_file` / `search_files` 标记 `read_only`，发现类批量强制只读；普通 `act` 的写入边界是工作区路径规则 + 计划批准，不是操作系统沙箱。

拒绝执行时：记录含原因的 `feedback` 事件，消耗一轮，不产生副作用。人工 `/reject` 终止（STOPPED / user_rejected）。

## 完成门与证据

`CompletionGate.check`（`runtime/gates.py`）在每次 `FinalResponse` 时检查：

1. 环境任务必须至少有一条 `Observation`；
2. `evidence_ids` 必须是成功观察 ID 的非空子集，未知 ID 拒绝，且最后一条观察必须成功；
3. 若有计划：修订已批准、全部步骤完成；
4. 验收条件逐项核验：`evidence`（引用有效观察）、`tool_success`、`tool_result`（最新匹配观察的类型化结果子集 / 文本包含，较新失败覆盖旧成功）、`file_exists` / `file_contains`（工作区确定性检查，≤1 MB）。

通过 → `COMPLETED`、`final_output`、`stop_reason=goal_completed`；不通过 → `completion_rejected` 反馈事件，循环继续，模型须补做或改述。

**“完成”不等于外部成功**：报告（`reporting.py`）明确写 `scope_note`——只检查显式条件与已记录证据；未声明的要求、视觉质量、工具外写入不自动验证；未配置条件时附警告。Runtime 之外的独立核验在 `evaluation/verifiers.py`。

## 存储与证据流

```
目标/命令 → decision 事件 ──授权── action_intent（先落盘）→ 执行 → Observation → feedback/迁移
                │                                                          │
                └────────────── Completion Gate ←── respond ←──────────────┘
```

- **快照**：`.kong/runs/<run_id>.json`，`Snapshot` 含状态、历史、`pending_action_id`、线程与指标；写入为临时文件 + fsync + `os.replace` 原子替换；每轮开始前（含失败的 HTTP 请求）、每个动作意图与观察、每次迁移和人工命令之后都会保存；每运行一个 `.lock` 文件锁。
- **报告**：`.kong/reports/<run_id>.json`，运行结束写入：状态、声明、验收逐项结果、产物与存在性、未验证项、失败工具与模型错误分类、资源句柄与恢复警告、指标。
- **线程与记忆**：`.kong/context.db`（SQLite，仅线程元数据与记忆包）；全局记忆默认 `~/.kong/memory.db`。
- **可观察材料**：`run_id` / `thread_id`、`kong show` / `summary` / `runs` / `threads` / `context` 不调用模型即可读取快照、报告与实际上下文。

当前指标（`RunMetrics`）：活跃秒数、模型秒数、模型调用数、用量返回次数、token 计数（供应商未返回时标“未知”）。**未测量**：每个工具的延迟、美元成本、缓存命中率；token 在供应商不返回 usage 时不可得。

## 确定性评测

`kong/evaluation/`：12 题定义在 `specs.py`（`score_scope` 区分 model 与 mechanism），每题独立工作区、固定超时与轮次上限；`fixtures.py` 提供固定决策模型与模拟工具；`verifiers.py` 在 Runtime 之外核验产物值、保护文件哈希与状态；`runner.py` 保留逐次结果、失败、环境错误与用量。真实模型试次必须显式 `--live`。详细口径见 [EVALUATION](EVALUATION.md)。

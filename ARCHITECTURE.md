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
4. 验收条件逐项核验：`evidence`（引用有效观察）、`tool_success`、`tool_result`（最新匹配观察的类型化结果子集 / 文本包含，较新失败覆盖旧成功）、工作区文件检查（`file_exists` ≤100 MB；文本与 JSON 条件 ≤1 MB）。

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

当前指标（`RunMetrics`）：活跃秒数、模型秒数、模型调用数、用量返回次数、token 计数（供应商未返回时标“未知”）；另有 `tool_calls`、`tool_seconds`、`tool_wall_seconds`、`tool_timings` 和 `tool_timing_complete`。逐次记录 action ID、工具/操作、单调时钟耗时、成功/取消/未知结果；动作结果事件附 `elapsed_seconds`。验证失败和取消也记消耗时间，计时不能改变校验与控制结果；非法 operation 只标 invalid 后交原执行器判错。

`tool_seconds` 累计所有调用（并发可重叠），`tool_wall_seconds` 是批次调度/计时检查点/执行/取消收尾的实际等待，二者不可混用。单工具 elapsed 覆盖执行器校验、授权、工具运行和清理，不含计时前检查点写盘。摘要派生 `runtime_overhead_seconds = active_seconds - model_seconds - tool_wall_seconds`，覆盖上下文、产物哈希、其他持久化与重试等待；不是纯 CPU 时间。活跃时长不含暂停等人和最后报告写盘，完整用户可见任务时间由外部计时器另测。旧历史或崩溃期间的缺失耗时不补造，标完整性 false，派生余量为 null。**未测量**：美元成本、缓存命中率、供应商内部排队/预填充/推理时间。

## 确定性评测

V0.4A 候选增加 `file_equals`（文本含空白完全一致）与 `file_json`（键集合及递归值类型完全一致，拒绝重复键及非标准数值）。文件验收记录实际读取字节的 SHA-256，并检查读取前后的文件大小和修改时间；后续修改需重新核验。这些规则只验证显式合同，不保证事实语义真实，也不防御刻意伪造文件元数据的并发写入。`tool_success`、`tool_result`、`evidence` 默认 current，受验收本地文件需与观察的 SHA-256 版本一致；event 只证明历史操作，不能同时声明当前产物路径。远端写回执不能借本地哈希通过 current；泛用 MCP 暂无资源回读适配器。

协议诊断仅保留白名单字段路径和具名错误码；模型提出的记忆条目保存来源片段并进入候选，人工确认/规则原话与语义真实性分开处理。旧无支持信息记忆仍可读取，但未确认条目不直接注入。V0.4B 快照写 schema 3，兼容读取 schema 1/2；旧无版本核验不能充当当前证明。

模型故障记录固定类别、可重试标记与 HTTP 状态码，不保存服务响应正文。账户、认证、权限和配置错误会停止当前真实评测套件。V0.4B 对超时、连接失败、限流与服务错误进行可见的有界恢复，默认全运行最多 2 次、上限 3 次，等待至多 5 秒且输入可打断；每次请求占用原调用预算。协议格式修复仍单独受连续失败和预算约束。工具未知结果不得自动重试；不切换供应商、模型或计费路径。

## V0.4B 运行交互与事件接口

`runtime/interaction.py` 定义 `RunEvent` v1 和 `RunInput`。运行事件由 `on_event(kind, {**data, "event": serialized_event})` 通知，也保存到快照独立的 `run_events` 列表；CLI `--events-json` 仅输出其中 `event` 对象。它们不进入决策/记忆来源历史，不改变历史索引。字段为 `version`、递增 `sequence`、`run_id`、`thread_id`、`turn`、`status`、UTC `timestamp`、固定 `kind` 与 `data`。写盘先于通知，消费者异常不能决定执行状态。结束事件和最终报告使用同一 Runtime 状态；报告含 `last_event_sequence`。

主要事件：`permissions` / `isolation`；`model_waiting` / `model_output` / `model_finished` / `model_error` / `model_retry`；`input_queued` / `input_applied` / `decision_discarded` / `batch_revoked`；`action_started` / `action_finished` / `action_unknown`；`transition`；`completed` / `paused` / `cancelled` / `stopped` / `failed`。SSE 接收进度只含长度，不向终端展示未校验 JSON 或推理正文。完整响应须有正常完成标记，经过合同校验才交给控制器；流有总时限和大小限制。

`Runtime.submit_input(kind, text)` 在拥有 Runtime 的事件循环调用，种类为 `constraint` / `goal` / `cancel`，队列最多 32 条、文本最多 16000 字符。接收与生效分别记事件。生效点是请求前、上下文准备后、模型返回后、每个动作开始前及已开始动作收尾后。新目标撤销旧目标的验收条件，旧目标、旧计划与旧条件保留在人工审计事件；补充约束保留目标与条件。已有计划失效，只有新提案或提问获准；需人工重新批准，旧目标证据不能证明新目标完成。

独立只读 `read_file`、`list_dir`、`search_files` 在 `parallel_safe` 标记下并发，连续批次上限默认 3、允许 1..8；重复调用、写入、终端、浏览器和 MCP 不参与。先保存全部已开始动作的唯一意图，再执行；结果按意图顺序落盘，失败仍收齐其他已启动结果。取消停止下一批，已启动文件读取收尾，结果不漏记。取消或超时导致副作用未知时保留未决 ID，恢复逐项核查。

## V0.4B 权限与 Docker 边界

`tools/permissions.py` 是宿主所有的操作/资源策略：`allowed_tools`、各工具的 `operations`、工作区内 `read_roots` / `write_roots`、`mcp_servers` 与各服务器 `mcp_tools`。模型不能扩大授权。未设置策略时保留原工作区能力，正常任务无需反复批准；启动/恢复都记录完整策略及指纹，变更可对照审计。递归检索和列目录在每个解析后的子路径重新核查读权限，按调用创建独立资源视图；`patch_file` 同时要求读写权限。浏览器截图使用写入根目录。MCP 远端方法内部实际资源范围依赖服务端；浏览器远端网络没有域名级隔离保证。

资源受限策略不能用宿主进程/终端绕过文件工具。`--sandbox docker` 仅隔离 `process_exec`：挂载一个已存在的专用工作区子目录（禁止整个工作区、保留凭据文件及链接），读取权限覆盖完整挂载；写入权限不覆盖完整挂载则只读。镜像必须已安装，运行固定 image ID，失败关闭执行。容器以 uid/gid 65534 运行，网络 `none`、根文件系统只读、所有 capability 移除、禁止提权，设置进程数 64、内存 512 MB、CPU 1；只有受限临时 `/tmp` 与专用挂载可写，不传入宿主密钥、daemon socket、host PID/network。子进程处于相同边界。容器创建/取消竞态等待归属确认后清理，超时及退出强制清理，清理未确认则标记未知并暂停。

模型 API 在宿主，不在该容器网络限制内。Docker daemon、所选镜像和宿主内核可信；不宣称抵御容器/内核漏洞或恶意宿主并发替换挂载文件。文件工具仍是应用层路径校验。隔离模式拒绝终端、浏览器、MCP 与实时网页组合。参考 [Docker 运行约束](https://docs.docker.com/engine/containers/run/) 与 [Docker 安全边界](https://docs.docker.com/engine/security/)；实际越界探针在 `tests/test_docker_backend.py`。

`kong/evaluation/`：12 题定义在 `specs.py`（`score_scope` 区分 model 与 mechanism），每题独立工作区、固定超时与轮次上限；`fixtures.py` 提供固定决策模型与模拟工具；`verifiers.py` 在 Runtime 之外核验产物值、保护文件哈希与状态；`runner.py` 保留逐次结果、失败、环境错误与用量。真实模型试次必须显式 `--live`。详细口径见 [EVALUATION](EVALUATION.md)。

# Kong-Agent

一个面向个人本地任务的单 Agent Runtime：模型只提出决策，授权、执行、证据与验收由 Runtime 完成。

当前版本 **0.4.0rc3**，是待冻结验收的本地候选。需要 Python 3.12+；版本结论以该源码身份的实际回归与完整批次为准。

当前开发分支包含未发布的 V0.4A 正确性候选。工程回归已通过，真实任务质量门槛仍未通过；具体测量口径、失败与限制见 [EVALUATION](EVALUATION.md)。
支持 Chat Completions 兼容模型接口，模型通过结构化 JSON 提出决策。

**交付状态：本候选仅本地提交；公开发布需另行确认许可、版权与使用授权，并检查允许清单快照。内部 `docs/` 不随候选包交付。**

## 30 秒了解

| 问题 | 答案 |
|---|---|
| 是什么 | 本地单 Agent Runtime：接收目标，驱动模型决策，调用工具推进工作，保存执行证据、显式验收结果和可恢复的任务状态。 |
| 与普通聊天机器人的区别 | 聊天模型直接生成回复或动作；Kong 的模型每轮只返回一个结构化决策（`Decision` 联合类型：respond / act / plan / ask 等），由 Runtime 决定是否授权、执行并记录证据。模型不能自批计划，也不能自判完成。 |
| 模型与 Runtime 的权限边界 | 授权集中在 `runtime/controller.py`：FAST 禁止提计划，PLAN 模式在人工 `/approve` 指定修订前禁止任何环境动作，完成必须通过显式验收门。工具默认只在工作区内操作文件；本机命令、终端、浏览器需显式开关，使用当前用户权限，**不是操作系统沙箱**。 |
| 工具执行 | `ToolExecutor` 校验参数并限时执行；每次动作**先落盘** `action_intent` 再执行，结果以 `Observation`（成功/失败、输出、哈希）写入历史，失败会截断同批后续动作。 |
| 崩溃恢复 | 状态原子写入 `.kong/runs/`；恢复时**不自动重放**结果未知的动作，先停在等待人工状态，核查实际效果后用 `/resolve` 说明再继续。终端、页面、MCP 连接等外部句柄不恢复。详见 [RECOVERY](RECOVERY.md)。 |
| 确定性评测 | Kong-Eval 12 题：每题独立工作区，由 Runtime **之外**的 verifier 核验产物、保护文件哈希与状态；固定决策与机制题可完全离线复现。真实模型试次必须显式 `--live`，与工程测试、固定流程分开记录，不把小样本自测写成通用准确率。 |

## 控制闭环

```mermaid
flowchart TD
    U["用户目标 / 人工命令"] --> D["模型决策 Decision JSON"]
    D --> A{"Runtime 授权<br/>controller.authorize"}
    A -->|"拒绝"| FB["feedback 事件 + checkpoint"]
    FB --> D
    A -->|"计划提案"| P["APPROVAL 停在批准边界<br/>等待人工 /approve 指定修订"]
    P -->|"批准后重新决策"| D
    P -->|"/reject"| ST["STOPPED"]
    A -->|"执行动作"| T["ToolExecutor 执行工具<br/>action_intent 先落盘"]
    T --> E["证据 Observation 写入历史<br/>成功/失败、输出哈希"]
    E --> D
    D -->|"respond 完成提案"| G{"Completion Gate<br/>显式条件 + 已记录证据"}
    G -->|"未通过：继续"| D
    G -->|"通过"| FIN["Finish：COMPLETED<br/>final_output + 报告"]
    A -->|"需要人工输入 / 动作结果未知"| R["WAITING_USER<br/>人工回答或 /resolve"]
    R --> D
```

每个节点对应实际代码：决策处理在 `runtime/loop.py`，授权在 `runtime/controller.py`，完成门在 `runtime/gates.py`，
状态与证据流在 `storage.py` 与 `reporting.py`；模块级说明见 [ARCHITECTURE](ARCHITECTURE.md)。
图中不存在沙箱或自动成功判定——“完成”只表示显式条件与已记录证据通过。

## 五分钟离线体验

默认环境任务须有覆盖交付物的声明条件：代码/数据使用 `--completion-contract`，或提供精确值/结果 `--criteria`。文件存在、包含某个词、普通工具成功不足以确认业务完成；缺少条件时保存产物并进入 `waiting_user / acceptance_criteria_required`，工具重试不能增加验收权限。普通无工具对话仍可直接结束。

调用方明确只做机械控制检查时可用 `run --completion-scope mechanical`，API 使用 `Runtime.new(..., completion_scope="mechanical")`；此时输出和报告明确标识业务完成未确认。完整检查较多来源文件时，可重复指定 `--progress-path`（API `progress_paths`），范围内的首次读取可以支持有界续租。任意新输出、错误写入和范围外读取仅计活动次数；完整自然语言正确性不由这些指标推导。

在源码目录执行（需先安装 Python 3.12+ 和 uv）：

```powershell
uv sync
uv run kong demo --auto-approve
```

依赖安装需要网络；安装后演示不调用模型或外部服务，无需密钥。它在独立的
`.kong/demos/<id>/` 中创建文件、读取验证并通过内容验收，不覆盖项目文件。
这是固定决策演示，用于检查安装与控制流程，不能代表真实模型能力。
去掉 `--auto-approve` 可体验人工审阅计划，再输入 `/approve 1`。

## 三个可复现的离线演示

三项都不调用模型、不需要密钥，可在干净工作区独立复现（前两项与 `tests/test_cli.py`、第三项与 `tests/test_cli.py` / `tests/test_evaluation.py` 的 CI 覆盖相同机制）。

**1. 正常工具任务**——固定决策走完 提案 → 执行 → 证据 → 验收：

```powershell
uv run kong demo --auto-approve
```

预期：退出码 0；摘要行 `验收：1/1 个显式条件通过`；产物 `.kong/demos/<id>/kong-demo.txt` 存在；报告 `.kong/reports/<run_id>.json`。
失败条件：退出码非 0、验收非 1/1、产物缺失。清理：删除 `.kong/demos/` 与 `.kong/reports/` 下对应条目。

**2. PLAN 批准边界**——批准前无任何写入，批准指定修订后才执行：

```powershell
'/quit' | uv run kong demo # 停在批准边界；输出含“恢复命令：kong resume <run_id>”
'/approve 1' | uv run kong resume <run_id> # 换成上一步的 run_id
```

预期：第一步 `验收：0/1`、`产物：…（当前不存在）`、状态 `waiting_user`；第二步 `completed`、`验收：1/1`、文件内容与计划验收条件一致。
失败条件：第一步出现产物文件（批准前写入即违规）、第二步未完成或批准被模型自行放行。清理：同上。

**3. 结果未知的中断与 `/resolve`**——注入执行结果未知的动作，恢复时不自动重放：

```powershell
uv run python examples/interrupted_action_demo.py
uv run python examples/kong_eval.py --cases unknown_action_restore --repetitions 1
```

预期：脚本打印 JSON 证据并退出 0——暂停于 `waiting_user`、`pending_action_id` 存在、零观察/零模型调用/`canary.txt` 未创建，未解决前其他命令被拒绝，`/resolve` 后重新提案执行、动作 ID 不复用（`a1`,`a2`）、保护文件哈希不变、`goal_completed`。eval 用例报告 `status: success, model_calls: 0`。
失败条件：任何一项检查为 false 或退出码非 0。清理：删除脚本默认创建的 `.kong/demos/interrupted-*`。


## 连接模型

```powershell
Copy-Item kong.example.toml kong.local.toml
```

编辑对应 profile 的 `base_url`、`model`、`api_key_env`。配置文件中的模型 ID 仅是示例，
请填写服务实际支持的模型。程序会在接口根路径后追加 `/chat/completions`。
密钥通过环境变量读取，程序不会自动加载 `.env` 文件。
所需变量名见 [`.env.example`](.env.example)；该文件只是空值模板，不会自动导入。

PowerShell 7 隐藏输入密钥后启动：

```powershell
$env:KONG_API_KEY = Read-Host "模型 API Key" -MaskInput
uv run kong --profile relay run "读取 pyproject.toml，说明项目依赖"
```

本地免鉴权服务可使用 `local` profile，无需密钥；服务需预先运行并加载模型。
模型须遵守 JSON 决策协议；`json_mode = false` 仅关闭传输参数，不放松决策校验。
使用云端模型时，被选入上下文的文件内容会发送到所配置服务。

## 当前能力

| 能力 | 启用方式与范围 |
|---|---|
| 文件操作 | 默认启用；工作区内列目录、文本读取、搜索、写入与精确替换 |
| 本机命令 | `--allow-process`；有超时和输出限制，可选 Docker 专用目录隔离 |
| 交互终端 | `--allow-terminal`；Windows ConPTY，支持多轮输入与读取 |
| 浏览器 | `--allow-browser`；独立 Chromium、DOM 操作、截图 |
| MCP | `--allow-mcp`；显式配置的 stdio / Streamable HTTP 服务 |
| 公开网页研究 | `--web live`；搜索、抓取与来源快照回读，搜索需单独配置服务 |
| 技能 | 随包提供工作区分析、代码、Word、PDF、表格、幻灯片、技能编写、研究共八个技能 |
| 连续性 | 同 Thread 对话、带来源记忆、任务摘要、暂停与恢复 |
| 运行交互 | `--stream` 流式接收进度；`/add`、`/goal`、`/cancel`；版本化事件与输入队列 |
| 权限与并发 | `--permissions` 操作/资源策略；`--read-concurrency 1..8` 独立文件读取并发 |

可选组件：

```powershell
uv sync --extra test --extra skills --extra integrations
uv run python -m playwright install chromium
uv run kong --allow-process --allow-terminal --allow-browser --allow-mcp doctor
```

启用开关放在子命令前，仅作用于本次启动。`doctor` 默认检查本地环境，不调用模型或搜索服务；
`doctor --online` 才进行可能计费的联网检查。MCP 服务器与网页研究的配置方式见
[配置示例](kong.example.toml)。

## 执行与核验

```powershell
uv run kong --allow-process run "读取工作区订单，汇总为 report.csv，核对总额后报告"
uv run kong run "整理项目资料，先提交方案" --mode plan
uv run kong runs
uv run kong summary <run_id>
uv run kong --allow-process resume <run_id>
```

FAST / AUTO / PLAN 共用执行循环，模型调用上限分别为 4 / 40 / 60；PLAN 批准前不执行工具。
可以用 `--criteria` 提供文件内容、工具操作与结果等显式验收条件。工具调用成功不等于目标正确，
最终产物仍应按任务要求独立检查；`completed` 只表示 Runtime 的显式验收门通过。

模型或网络异常会保存并暂停；中断后结果未知的动作不会自动重放。恢复前需检查实际状态，
必要时用 `/resolve` 说明。恢复任务不会复活旧终端、浏览器页面或 MCP 连接；
流程与命令见 [RECOVERY](RECOVERY.md)，快照与报告字段见 [ARCHITECTURE](ARCHITECTURE.md)。

## V0.4B 本地候选：运行中纠偏

```powershell
uv run kong --stream --read-concurrency 3 run "读取资料并生成汇总"
# 运行期间输入：
/add 汇总只包含已支付订单
/goal 改为先检查数据缺失情况，提交新方案
/cancel
```

`/add` 补充约束，`/goal` 更换目标并撤销旧验收条件；两者在模型决策返回后、下一动作开始前生效，撤销未执行的旧批次。已有计划需重新提案和批准。`/cancel` 停止新动作，已开始的普通写入先收尾；进程等可管理动作会取消并清理。结果未知的动作保留记录，恢复需逐项 `/resolve`。关闭标准输入不会自动取消任务。

`--events-json` 输出版本 1 的 JSON 行事件；`--stream` 只显示接收进度，完整响应结束且 JSON 合同通过校验后才执行动作。可重试的模型网络故障默认最多恢复 2 次（`--model-retries 0..3`），消耗原模型预算，恢复后不重置。认证、账户和配置错误直接暂停；工具副作用不自动重试。

权限文件示例（保存为本地 `permissions.json`，需预先建立 `job` 目录）：

```json
{"read_roots":["job"],"write_roots":["job"],"operations":{"browser":["snapshot"]},"mcp_servers":["local_demo"],"mcp_tools":{"local_demo":["add"]}}
```

```powershell
uv run kong --permissions permissions.json run "汇总 job 内资料"
uv run kong --permissions permissions.json --allow-process --sandbox docker --sandbox-root job --sandbox-image python:3.12-slim --web disabled run "在 job 内运行 Python 数据处理并核验"
```

Docker 必须预先运行且本地已安装所指定可信 Linux 镜像；不会自动拉取镜像或回退宿主。模型请求在宿主进行，`process_exec` 的工作负载只有专用目录挂载、无网络、只读容器根目录及受限子进程。Docker 模式禁用终端、浏览器、MCP 和实时网页工具。文件工具由应用层权限检查约束；这些检查不防御恶意宿主并发修改。具体范围见 [ARCHITECTURE](ARCHITECTURE.md)。

本轮指定 MiMo 模型时，可在本地配置添加以下 profile，并在当前会话设置 `MIMO_API_KEY`：

```toml
[profiles.mimo]
base_url = "https://api.xiaomimimo.com/v1"
model = "mimo-v2.6-flash"
api_key_env = "MIMO_API_KEY"
json_mode = true
stream = true
thinking_mode = "disabled"
timeout = 90
max_tokens = 4096
context_window_tokens = 65536
```

使用 `--config kong.local.toml --profile mimo` 选择该配置。`thinking_mode` 与旧 `enable_thinking` 参数互斥。供应商、模型与计费入口必须显式选择，故障不会触发替换。

## 已知边界

- 本机命令、终端和浏览器具有当前用户的权限，**不是操作系统或网络沙箱**；文件工具的路径限制不约束任意程序。
- 文件工具主要处理 UTF-8 文本；读取/修改最多 256 KB。技能提供流程与基础检查，不保证 OCR、视觉排版或公式重算。
- 浏览器不继承个人登录态；没有浏览器视觉理解、专用 iframe 定位或上传下载管理。
- 流式响应只用于接收进度，不支持供应商原生工具调用；上下文预算使用估算值，报告用量仅来自实际返回值（缺失时显示“未知”）。
- 不提供多 Agent、网页产品界面、云同步、后台调度或长期无人值守可靠性承诺。

## 如何验证

```powershell
uv sync --extra test --extra skills --extra integrations
uv run pytest -q
uvx ruff@0.16.9 check .
uv run python examples/release_regression.py
uv run python examples/kong_eval.py --list
uv run python examples/kong_eval.py --repetitions 3
uv build
uv run python examples/verify_release_install.py dist/kong_agent-0.4.0rc1-py3-none-any.whl
```

工程测试、固定模型流程测试和真实模型任务结果分开记录。本地 V0.4B 候选完整工程回归为
**379 passed / 2 skipped**（包含已安装镜像的真实 Docker 测试；两项跳过因主机无符号链接权限）。未配置 `KONG_TEST_DOCKER_IMAGE` 时另跳过两项真实 Docker 检查。历史 V0.3 基线 253/1 与收尾记录 226/1、
隔离基线 238/1 与阶段收尾 251/1、252/1 属旧结果，均不是任务成功率。CI 工作流见
[`.github/workflows/ci.yml`](.github/workflows/ci.yml)。评测任务、分母、判分与结果边界见
[EVALUATION](EVALUATION.md)（题目定义在 [`src/kong/evaluation/specs.py`](src/kong/evaluation/specs.py)）。

`python examples/interactive_comparison.py --output <新目录>` 记录相同 6 个读取任务的串行/并发结果与运行中追加约束。它使用固定模拟延迟，只衡量调度机制；真实模型质量与人工纠偏负担须另测。`python examples/runtime_timing.py --output <新目录> --repetitions 3 --docker-image <已安装可信镜像>` 用固定决策实际执行文件与进程任务，分开测量工具调用、隔离启动/清理和 Runtime 其余耗时，排除供应商等待。任务报告新增 context/hash/snapshot/retry/validation/tool wait 归因、字节与事件计数；嵌套耗时不能相加，恢复历史只报告可测部分。唯一成功输出带来的 activity credit 不等于 verified goal progress。本候选未发布。

许可证确定前，发布整理脚本仅生成待审阅候选文件，不向远端上传：

```powershell
uv run python examples/prepare_public_release.py
```

发布范围、扫描证据与剩余门槛见 [PUBLIC_RELEASE_CHECKLIST](PUBLIC_RELEASE_CHECKLIST.md)；
版本变化见 [CHANGELOG](CHANGELOG.md)。

## 调用方声明完成合同

`kong run "任务目标" --completion-contract contract.json` 可加载版本 1 或 2 的 `code` 或 `data` 合同。版本 1 的原有语义与绑定身份保持兼容。

版本 2 增加 `resource_closures: [{"id":"terminal","tool":"terminal"},{"id":"browser","tool":"browser"}]`：所声明工具在本 Run 实际打开的每个资源都必须有成功关闭收据，提交时须引用相应真实 action ID。未知打开/关闭结果仍须人工核对，不自动关闭或重放。关闭动作之后再完整回读最终文本/JSON 产物，避免以旧证据提交。

二进制产物可用 `format:"file"`、`binary_prefix_hex:"89504e470d0a1a0a"`、`require_readback:false` 声明 PNG 文件头；这仅验证当前字节前缀，不证明图像内容或完整格式。合同还必须包含最终完整回读或实际最终核验命令作为证据锚点。调用方负责提供充分条件，模型不能为自己的输出补造验收权限。
合同由调用方提供，模型无法改写。它补充现有 `--criteria`，把最终文件、保护源、真实命令退出码/输出、JSON 字段类型/允许值、CSV 列名/行数/精确有序行，以及有限 CSV→JSON 汇总规则变为确定性检查。自然语言目标不会自动转为完整合同。

例如代码修复合同：

```json
{"version":1,"family":"code","protected_paths":["check.py"],"artifacts":[{"id":"code","path":"calc.py","format":"file","require_readback":false}],"commands":[{"id":"tests","argv":["python","check.py"],"stdout_contains":["checks passed"],"fresh_python":true}]}
```

命令通过实际工具执行，Gate 只检查已记录的结果；不会替模型运行检查。命令必须覆盖最终版本，之后修改文件需要重新核验。数据合同支持 UTF-8 `text`/`json`/`csv` 产物，必须有当前完整读回；合同文件验证上限 1 MB，文件工具读回仍受其 256 KB 上限限制。JSON 比较区分类型并拒绝重复键和非标准数值。`csv_rows` 是调用方声明的有序字符串对象列表，按解析后的列和值精确比较，允许标准换行差异；它不推断分组业务规则。

Python 代码检查需同时在合同和 `process_exec` 动作中声明 `fresh_python:true`，使用 `python` 别名。它为本次导入指定新的缓存范围且禁用字节码写入，避免同秒同大小修改复用旧 `.pyc`。这不是隔离授权；普通进程默认行为保持原样。

保护源初始版本进入快照，恢复不重新基线。运行中替换目标或追加约束会使已绑定合同失效并暂停；调用方通过 `Runtime.set_completion_contract` 更新后明确继续，新保护路径需在新的 Run 开始时声明。未声明任务要求不被自动核验，已有不带合同的 Run 保持原语义。

显式代码/数据入口使用 `--task-family code` 或 `--task-family data`，必须同时提供同类合同；缺少合同、family 不符或只有文件存在性的数据合同在调用模型前拒绝。可从 [代码合同](examples/contracts/code_repair.json) 或 [数据汇总合同](examples/contracts/data_summary.json) 开始，按实际目标审阅路径、字段、汇总规则与检查命令。示例不会创建输入或执行检查：

```powershell
kong --allow-process run "修复 calc.py 并通过检查" --task-family code --completion-contract examples/contracts/code_repair.json
kong run "汇总 rows.csv 中 active 记录" --task-family data --completion-contract examples/contracts/data_summary.json
```

运行时会将当前未满足条件、具体命令失败原因和候选证据置于收尾上下文，指导模型先检查、后最终回读、再提交；没有隐式动作或自动完成。`patch_file` 接受 `read_file` 的换行视图，仍要求唯一字面匹配，保留替换范围外的字节。

摘要新增 `verification` 与 `closure`：区分合同/值条件/机械检查，记录首次观测到产物条件通过后的调用数、耗时、失效及拒绝修正。只证明声明的条件；字段类型或命令成功不能覆盖未声明的业务要求。旧运行缺少检查点时保持未知，活动积分和续租策略保持原样。

## 文档导航

| 文档 | 内容 |
|---|---|
| [ARCHITECTURE](ARCHITECTURE.md) | 模块地图、决策协议、策略与预算、授权边界、完成门与证据流 |
| [RECOVERY](RECOVERY.md) | 持久化点、未知结果动作、`/resolve`、不可恢复的外部句柄 |
| [EVALUATION](EVALUATION.md) | 12 题与 model/mechanism 分母、任务契约、判分口径、已记录基线与边界 |
| [PUBLIC_RELEASE_CHECKLIST](PUBLIC_RELEASE_CHECKLIST.md) | 公开发布门槛与已验证证据 |
| [CHANGELOG](CHANGELOG.md) | 版本变化与各阶段验证记录 |
| [kong.example.toml](kong.example.toml) / [`.env.example`](.env.example) | 配置与环境变量模板 |

内部开发文档保留在私有仓库的 `docs/`，不进入公开工程库，本页不链接它们。

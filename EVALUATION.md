# EVALUATION — Kong-Eval 评测口径与结果记录

Kong-Eval 是一组固定、可独立核验的小任务，用来回答“这台 Runtime 在**受约束契约**下做成了什么、失败在哪里”，不提供通用 Agent 排行榜，也不把 Runtime 的 `completed` 当作外部成功。任务定义与分母以 [`src/kong/evaluation/specs.py`](src/kong/evaluation/specs.py) 为准，本文解释契约、核验方式与已记录结果的边界。

## 三类结果，分开记录

| 类别 | 是什么 | 入口 |
|---|---|---|
| 工程测试 | 代码级断言，无模型 | `uv run pytest -q`（当前基线 252 passed / 1 skipped） |
| 固定决策流程测试 | 离线夹具驱动，验证评测流程与 Runtime 机制，**不测模型能力** | `uv run python examples/kong_eval.py --repetitions 3`（默认离线）、`examples/release_regression.py` |
| 真实模型试次 | 显式 `--live` 才发起真实请求，可能计费 | `uv run python examples/kong_eval.py --live --config kong.local.toml --profile relay --repetitions 3` |

离线模式对文件、数据处理与修复题实际执行程序；终端、MCP、浏览器使用**明确标识的替身**（`fixture_simulations` 标记），真实交互的工程回归另由 `release_regression.py` 覆盖。程序不自动批准，失败后不开新局替换结果；协议纠错增加的调用数仍算同一次有预算的运行。

## 12 题与分母

每题每次重复都是独立工作区、独立任务存储、独立记忆与工具实例，结束后关闭资源。预算：每试次最多 24 次模型调用、180 秒，另有约 10 秒的独立核验与清理时间。

| ID | 类别 | 分母 | 核验内容 |
|---|---|---|---|
| `data_paid_product` | data | model | 只统计已付款商品；精确值、Decimal 金额、排序、原始输入哈希、程序真实执行 |
| `data_region` | data | model | 地区分组汇总，同上 |
| `data_refunds` | data | model | 销售减退款净收入，同上 |
| `repair_total` | repair | model | 修复求和函数；保护 `check.py`，并用**未提供给模型的输入**测试 |
| `source_tomllib` | source_research | model | 读取固定本地快照，输出类型严格（string/boolean）的 JSON；不测互联网搜索 |
| `terminal_prompt` | terminal | model | 真实会话输入、文件精确值、同会话退出码与关闭 |
| `mcp_add` | mcp | model | 发现并调用配置的服务，核对真实返回值、产物与连接关闭 |
| `browser_form` | browser | model | 本地服务 URL、同页填表点击、DOM、后端文件、PNG 签名与关闭 |
| `tool_error_recovery` | tool_error | model | 保留的读取失败后完成恢复、回读正确产物 |
| `unknown_action_restore` | unknown_action | **mechanism** | 注入未决动作并恢复：零重放、零模型调用、等待人工核验 |
| `plan_approval` | approval | model | PLAN 模式提交计划后**等待明确批准**，零环境动作；等待本身是正确结果 |
| `insufficient_evidence` | evidence | model | 读材料后按**指定句式**请求证据；只测受约束的证据请求协议，不评判任意自然语言诚实性 |

分母规则：**model = 11 题 × 重复次数**（环境错误单列排除并披露数量）；**mechanism = `unknown_action_restore` × 重复次数**，不进入模型分母。`plan_approval` 的 `expected_approval` 计为通过。

## 任务契约、隔离与核验

- 每次 suite 用新 UUID；manifest 记录完整目标、输入、允许工具、成功条件、预算、模型设置与比较标识；源码另有 SHA-256 指纹，同版本号改动可区分。
- 外部 verifier 在 **Runtime 之外**运行：核对实际文件值与类型、受保护输入哈希、动作与顺序、终端/页面身份；修复题的核验在独立进程执行，过滤凭据环境变量，限制时间、输出与子进程生命周期。
- 文件缺失、解析失败或契约不满足即判失败；损坏产物不算“缺少环境依赖”。期望的等待批准与证据请求不会因“没有产物”判失败。
- 这不是对抗恶意模型的沙箱；本机程序仍具当前用户权限。

## 判分状态与指标

| 状态 | 含义 |
|---|---|
| `success` | 外部核验满足该题契约（含正确等待批准或阻止未知动作重放） |
| `expected_approval` | PLAN 题确实停在批准边界且未执行任何环境动作 |
| `failed` | 产物、动作、状态或约束检查不通过 |
| `environment_error` | 缺依赖、模型服务不可达或评测器异常——单列披露，绝不默默算成功 |
| `timeout` | 达到该次时间预算 |

- `model_score`：排除环境错误后的模型契约通过比例（同时披露排除数）。
- `model_pass_rate_all_attempts`：环境错误也计入分母，避免隐藏环境成本。
- `error_completion_rate`：Runtime 声明完成但外部核验失败的比例；无声明时为 null。
- `human_intervention`：未做观测实验，记 `measured: false` / null；“没有人工救场”不等于“测出负担为零”。
- 用量只累计服务实际返回的 token（缺失为 null，`usage_complete` 标记完整性），不估算费用。

## 结果留存与导出

```text
.kong/kong-eval/<suite_id>/
  manifest.json            完整契约与指纹
  summary.json             汇总（每题结束原子更新）
  cases/<case>/trial-NN/   initial_manifest.json、result.json、workspace/
```

强制终止保留已保存结果；以 `recorded / planned` 判断完整性，部分完成不算整套成功。

```powershell
uv run python examples/export_eval_report.py .kong/kong-eval/<suite_id> <新文件.json>
```

导出保留**全部**已记录试次与逐次判分、失败、用量、产物哈希，去除绝对路径、原始对话与自由文本错误；哈希只证明身份，不代替语义正确性——复现靠重跑公开契约与独立 verifier。2026-09-29 对一个新离线 suite 实测导出：2/2 试次、无路径或用户名泄漏、字段与 runner 输出一致。

## 已记录的真实模型基线（2026-09-23）

模型：SiliconFlow `deepseek-ai/DeepSeek-V4-Flash`（relay profile），12 题 × 3 次 = **36 次独立试次**，全部原始结果保留、失败未被重跑覆盖。2026-09-29 已将下列数字与脱敏导出 JSON 逐项核对一致（36/36 试次、分母、调用数与 token 总和）：

- 模型题 **31 / 33**（0 环境错误排除）；机制题 3/3，模型调用为 0。
- Runtime 完成声明 27 次，外部核验发现的错误完成 **0** 次。
- `plan_approval` 两次失败（连续 `Invalid Kong decision JSON (value_error)`，未形成待批准计划）；两次均为零环境动作、保护文件未变——授权边界仍有效，但计划提交流程不稳定，失败保留为回归依据。日志只有协议错误类别，无字段级定位，不能归因于模型能力缺陷。
- 146 次模型调用，146 次返回用量：输入 876487、输出 27415、合计 903902 tokens（服务实际计数，未估算费用）。
- 人工介入负担未测量；本轮无自动批准、无人工救场。

## 这些结果不能说明什么

- 任务小、输入固定、规则明确，重复仅 3 次：**不能**外推为通用任务成功率、生产可靠性或跨领域表现。
- 来源题是本地快照归纳，未测开放网络研究；证据不足题是指定句式协议，未测任意事实判断；无 AI Judge、无多模型排名。
- 远端同名模型可能随时间变化，跨期结果不保证可比；新契约或改判分时保留新指纹，不与旧分数混为同一基线。
- 工程测试通过、固定流程通过、真实模型得分互相独立，任一项都不能替另一项背书。

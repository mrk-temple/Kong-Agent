# Public release checklist — Kong-Agent 0.3.0

状态：**阶段 1–5 工程内容完成；作者已决定公开（2026-09-29：MIT、同仓库允许清单全新历史、公开核对后立即打 v0.3.0）。本清单所在快照推送后，远端比对与可见性切换按下方门槛逐项执行；托管 CI 首跑由本次推送触发。** 此清单记录已验证项目与剩余门槛，不能单独作为发布批准。责任人：仓库维护者。

## 已验证的基线与发现

- 审计基线：`f5779f12596dcfafbeefaae98893c826ed4ed513`，Windows 11 / Python 3.12.14 / uv 0.12.13。依赖锁文件 SHA-256：`fee2dd9a27d32d66a0d4fab2d4c8289dcc2867ac991f8c5cd5bf8816646c2f88`。
- 原始提交在隔离目录中重新执行 pytest：**238 passed / 1 skipped**，64.93 秒。跳过原因是主机无符号链接权限；不是完整跨平台认证，也不是模型任务成功率。
- **阶段 1 最终当前代码完整测试：251 passed / 1 skipped**，77.18 秒；唯一跳过为 Windows 主机无符号链接权限。证据：`.kong/public-release-audit/20260928-stage1/final-verified-pytest.log`。
- 发布与审计工具的反例检查覆盖历史删除/复用 blob 的凭据文件名、归档空目录、错误 tar 顶层、秘密路径脱敏、根配置与嵌套配置区分、候选哈希与内部资料排除。独立审查发现的路径遗漏已修复；**定向测试 15 passed**，不再重跑。
- 私有 GitHub 仓库当前有 151 个文件，其中 20 个位于 `docs/`。直接删除目录后把原仓库改为公开仍会暴露旧历史，因此保留私有开发仓库，公开仓库从审核后的快照建立新历史。
- 审计发现旧 sdist 含 21 个 `docs/` 文件和 `DESIGN.md`；发布快照允许清单与打包配置已收紧。旧文件继续留在本机，不作为新公开候选使用。
- 本地 `api.txt` 含 3 个已知凭据值，仅记录命中位置；忽略规则已排除该文件。扫描报告不含匹配值或原始日志正文。未发现这些值出现在所扫描的历史对象中。

## 安全与内容范围

- [x] 检查 `.gitignore` 对 `api.txt`、`kong.local.toml`、`.env*`、`.kong/`、虚拟环境、缓存和构建目录的排除；`.env.example` 是空值模板，允许发布。
- [x] 新增 `.env.example`，并在 README 说明 Kong 不会自动加载 `.env`；使用环境变量提供凭据。
- [x] 对工作区第一方文件和日志做已知秘密值及常见凭据模式扫描；展开 ZIP、wheel、tar 和 Office 压缩文件的一层成员。报告明确记录依赖缓存、审计输出、链接和大小限制等排除范围。
- [x] 获取远端 refs 后扫描所有本地 refs 可达的 blob、commit、tree 条目名称与 tag，涵盖删除过的历史文件、复用内容的不同文件名和提交消息；核对远端当前提交与私有状态。2026-09-29 复核覆盖 8 个 refs、4 个 commit、70 个 tree、211 个 blob，凭据命中 0。
- [x] 审核当前已知凭据的工作区命中：扫描 2,903 个文件和 3,163 个归档文件成员，仅 `api.txt` 命中；未在扫描过的 TOML、env 模板、日志、测试或源码发现匹配。目录项名称也接受检查；模式扫描不是未知秘密格式的完整保证。
- [x] 公开快照仅允许 `src/kong/`、`tests/`、`examples/`、`.github/workflows/` 与显式根目录文件；进一步排除嵌套内部文档、日志、凭据、数据库和缓存。
- [x] 公开根目录文档允许 `README.md`、`ARCHITECTURE.md`、`EVALUATION.md`、`RECOVERY.md`、`CHANGELOG.md`、本清单，以及作者后续确定的 `LICENSE` / `NOTICE`；未生成的文件不代表已完成。
- [x] `docs/`、`DESIGN.md`、`UPGRADE_PLAN.md` 与内部演进计划排除于快照和新发行包；内置技能的 `SKILL.md` 和 `references/` 保留。
- [x] 2026-09-29 用最终代码重建并扫描候选：构建目录 `dist/stage1-final-20260929`，候选 `dist/stage1-final-candidate-20260929`。候选含 **134** 个源码文件（ZIP 134 项）；wheel **93** 项、sdist **135** 项，扫描命中均为 0；sdist 与候选仅差构建元数据，无 `docs/` 或内部计划，8 个内置技能的 `SKILL.md` 与 `references/` 共 16 个技能文件保留。`published=false`，阻断项为版权立场未定。**该候选早于阶段 2 的 CI 工作流与 Ruff 修复，正式发布前（阶段 5）必须重新构建与复扫。**
- [x] 2026-09-29 发布当日以定稿内容（含 LICENSE、README/CHANGELOG/本清单更新）再次扫描并核对：构建 `dist/stage5-public-20260929/`，候选 `dist/stage5-candidate-public-20260929/`（**140** 个源码文件，0 命中，3 个已知值参与比对）。本项执行发生在该候选生成之后、公开快照推送之前；扫描不通过则中止发布。

## 阶段 2：CI 与静态检查（2026-09-29，本地证据）

- [x] `.github/workflows/ci.yml` 已创建：Python 3.12、`uv sync --frozen --all-extras`、Playwright Chromium 安装、`pytest -q -r s`；Windows 与 Linux 独立矩阵项、`fail-fast: false`；lint 作业固定 `ruff@0.16.9`。工作流不使用任何模型密钥、不执行 `--live`，`permissions: contents: read`。
- [x] 拟采用的 CI 命令已在本机完整演练：主 `.venv` frozen sync 零变更；**独立干净 venv 从锁文件重装后 251 passed / 1 skipped（69.42 秒，Python 3.12.14）**；完整记录见 `.kong/public-release-audit/20260929-stage2/verification-notes.md`。
- [x] 失败诊断如实留存：阶段 2 期间浏览器测试曾因本机 Playwright 缓存目录整体消失而失败（1 failed / 250 passed / 1 skipped），与 lint 修改无关；`cdn.playwright.dev` 直连下载 205 MB 包在约 15 MB 处断连，改用 npmmirror 镜像恢复后该组 10 项全部通过、全量回到 251/1。镜像仅是本机网络变通，GitHub 托管 runner 不需要。
- [x] Ruff 已锁定 `required-version = "==0.16.9"`，启用范围为最小规则集 `E4/E7/E9/F`，仅对 `examples/kong_eval.py` 排除 `E402`（sys.path 引导必须先于导入）。存量 **28 项全部以非语义方式修复**，当前 `ruff check .` 全绿。
- [x] 未采用的更宽默认规则集已记录待分阶段评估：ruff 0.16.9 默认族在修复后仍有 120 项（I001 63、BLE001 18、FURB167 15、TRY004 7、PLW1510 6 及 SIM/RUF/ASYNC/B 零星项）；不静默启用、也不视为通过。
- [ ] 首次推送到 GitHub 后核对托管 CI 实际通过/跳过/失败，并与本机结果解释地对应；Linux 矩阵项本机从未运行，预期 5 个 Windows 专属测试跳过、符号链接测试在 Linux 应真实执行——任何差异按事实记录，不改 Runtime 语义“修绿”。

## 阶段 3–4：文档与离线演示（2026-09-29，本地证据）

- [x] README 首屏重构为“30 秒了解”表格（是什么 / 与聊天机器人区别 / 权限边界 / 工具执行 / 崩溃恢复 / 确定性评测）+ Mermaid 控制闭环图；图中每个节点对照实际代码并注明“完成≠外部成功、无沙箱”。
- [x] 新增 `ARCHITECTURE.md`（模块地图、决策协议、模式预算、授权规则、完成门与证据、存储与可观察材料、未测量指标）与 `RECOVERY.md`（持久化点、恢复流程、未知动作与 `/resolve`、不可恢复句柄、可复现验证步骤）；引用以模块/符号为准，不依赖行号。
- [x] 根目录文档导航完成；README 已清除全部指向内部 `docs/` 与 `DESIGN.md` 的链接（仅保留“docs/ 不进入公开库”的说明性文字）。CHANGELOG 增补“0.3.0 工程化升级”条目，区分 251/1（当时）、238/1、226/1 与任务成功率。
- [x] 演示 1：`kong demo --auto-approve` 实测退出码 0、验收 1/1、产物与报告生成。
- [x] 演示 2：`'/quit' | kong demo` 停在批准边界（`waiting_user`、0/1、产物“当前不存在”）→ `'/approve 1' | kong resume <run_id>` 后 `completed`、1/1、产物内容正确；两步均实测退出码 0。
- [x] 演示 3：新增公开离线驱动 `examples/interrupted_action_demo.py`，实测 9 项检查全过（暂停态、零重放、零模型调用、未解决前拒绝其他命令、`/resolve` 后完成、动作 ID 不复用、保护文件不变）；eval 用例 `unknown_action_restore`（success、0 模型调用）与 `plan_approval`（expected_approval）同轮实测通过。
- [x] CI 覆盖无头确定性部分：新增 `test_interrupted_action_demo_script_all_checks`；演示 1/2 已有 `test_cli.py` 对应测试。**完整基线更新为 252 passed / 1 skipped**（主 venv 与独立干净 venv 各一次，58.93s / 58.65s）；Ruff 全绿。
- [ ] `EVALUATION.md` 尚未创建（阶段 5）；README 因此不链接该文件，评测口径暂以 `src/kong/evaluation/specs.py` 与本清单说明。

## 阶段 5：评测整理与复现检查（2026-09-29，本地证据）

- [x] `EVALUATION.md` 完成：12 题清单与 **model（11 题）/ mechanism（1 题）** 分母、任务契约与隔离（独立工作区/存储/记忆/工具、24 调用 180 秒预算、保护哈希、隐藏输入、独立进程核验与凭据过滤）、判分状态与指标定义、三类结果区分、未测量项明确列出；README 与 ARCHITECTURE 已链接。
- [x] 导出报告与代码一致性复核：历史脱敏基线 JSON 与内部记录逐项吻合（36/36 试次、model 31/33、mechanism 3/3、完成声明 27/错误完成 0、plan_approval 2 次失败均 `expected_approval` 检查未过、146 次调用、token 876487/27415/903902 精确一致、环境错误 0）；并用新离线 suite 实测 `export_eval_report.py`：2/2 试次保留、字段与 runner 一致、无绝对路径或用户名泄漏。
- [x] 离线复现重跑：`pytest -q -r s` → 阶段 4 后 **252 passed / 1 skipped**（主 venv 与干净 venv 各一次），审计排除修复加入回归测试后最终 **253 passed / 1 skipped**（57.69 秒）；`release_regression.py` → 7/7 passed（data/research/repair/terminal/mcp/browser/recovery），退出码 0；`kong_eval.py --repetitions 3`（离线）→ **36/36 记录、0 failed、0 环境错误、fixture_harness_passes 36**，退出码 0；三个离线演示见阶段 3–4。
- [x] 构建与安装：`uv build`（`dist/stage5-final-20260929/`）+ `verify_release_install.py` 源码树外安装 **9/9 检查通过**（版本、导入来源、demo、摘要、extras、doctor、8+ 技能目录），退出码 0。
- [x] 最终安全审计：新增文件全量扫描后，命中 6 处中 5 处为本机排练虚拟环境（`.kong/ci-rehearsal-venv`）的第三方包模式误报、1 处为 `api.txt` 已知凭据；已按“虚拟环境排除”既定范围为审计脚本补 `pyvenv.cfg` 标记排除（含回归测试，定向 16 passed）。复扫：**文件 3779、工作区命中 1（仅 api.txt）、历史 0**。两份报告都保留，不覆盖前次记录。
- [x] 候选重建（最终）：`dist/stage5-candidate-final-20260929/`，**139** 个源码文件（较阶段 1 +5：ci.yml、ARCHITECTURE、RECOVERY、EVALUATION、演示脚本），source/ZIP/manifest 一致，sdist 140 项仅多 `PKG-INFO`，三包均无 docs/内部计划、各含 8 SKILL.md + 8 references，扫描 0 命中、3 个已知值参与比对，`published=false`，且包含审计脚本的 `pyvenv.cfg` 排除修复。wheel/sdist/ZIP 的 SHA-256 与对应构建目录以候选 `manifest.json` 和本机交接记录为准（写入本清单会改变清单自身哈希，故不在此处复述）。此前的 `dist/stage5-candidate-20260929/` 生成于审计脚本修复前，仅作过程证据。**发布当日复扫与公开快照以 `dist/stage5-candidate-public-20260929/`（140 文件，含 LICENSE）为准，见下方“正式公开门槛”。**
- [ ] 正式公开操作（独立公开仓库生成、可见性、命名、`v0.3.0` 标签）：**待作者决定版权立场与仓库命名后执行**；本阶段未推送、未改可见性、未打标签。

历史扫描范围是**当前已获取 refs 可达对象**，不包括 GitHub 缓存、他人 fork、不可获取 refs 或不可达对象。历史对象按存储字节扫描；本次历史路径清单没有压缩归档。完整 refs、命中分类、扫描数量和排除范围留在本机审计报告，公开摘要不复制内部日志。

## 工程、演示与评测

- [x] GitHub Actions 工作流与 Ruff 静态检查：见“阶段 2”，本地证据已保存；托管 CI 首跑待推送验证。
- [x] README、ARCHITECTURE、RECOVERY、CHANGELOG 公开导航完成并清除内部 `docs/` 链接；`EVALUATION.md` 见阶段 5 待办。
- [x] 正常工具任务、PLAN 批准、interrupted action / resolve 三个离线演示可复现，命令/预期/失败条件/清理已写入 README；证据见“阶段 3–4”。
- [x] Kong-Eval 12 题、外部 verifier、隔离、隐藏断言与哈希/值检查的可审查说明见 `EVALUATION.md`；固定决策流程、机制题、工程测试与真实模型尝试已区分。
- [x] 报告保留失败和环境错误（基线 2 次 PLAN 失败原样保留）；小样本自测未写成一般准确率，未测指标（人工介入负担、费用估算）标为未测。
- [x] 完成审定版本的构建及源码目录外安装验证（阶段 5 节，9/9）；命令与报告对应同一版本 0.3.0。

## 正式公开门槛

- [x] 版权立场（2026-09-29 作者决定）：采用 **MIT 许可证**；`LICENSE` 已在允许清单与 sdist 清单内，README 状态行同步。
- [x] 公开仓库名称：沿用 `Kong-Agent`。作者选择**同一仓库 force-push 全新历史**方案：以允许清单单一初始提交替换远端 main，含 `docs/` 的原远端历史在私有状态下被替换、从未公开，仅由本地归档分支与既有分支保留。
- [x] 公开快照来自允许清单（本清单所在提交，140 个文件）；不携带私有 `.git` 旧分支、旧标签或其他内部 refs。
- [ ] 推送后以全新克隆比对远端全部文件与候选清单哈希，并确认远端 refs 仅含全新 main（无旧历史可达）。
- [ ] 作者在网页将可见性改为 public；随后 `v0.3.0` 标签指向完成上述核对的提交（作者已决定公开核对后立即打标签）。
- [ ] PyPI 发布不在本轮范围；未执行任何 PyPI 上传。

## 复核命令与证据

在有依赖和 Git 的开发工作区执行；为每次报告选择一个新路径，保留前次失败记录。仅在本机需要比对已知凭据时显式传入 `--secret-file <本机凭据文件>`；不上传该文件。

```powershell
git fetch origin
uv run python examples/audit_release_safety.py --output .kong/public-release-audit/<run-id>/safety.json
uv run pytest -q -r s
uvx ruff@0.16.9 check .
uv build --out-dir dist/<run-id>
uv run python examples/prepare_public_release.py --output dist/<run-id>-candidate --artifact dist/<run-id>/kong_agent-0.3.0-py3-none-any.whl --artifact dist/<run-id>/kong_agent-0.3.0.tar.gz
```

审计脚本退出成功表示报告生成成功；命中项需要人工分类，不能把退出码 0 当作无秘密。候选打包脚本遇到秘密、内部文件或不允许的成员会拒绝产生新的候选。候选报告仍标记为待审阅，不代表已获发布授权。

本阶段本机证据目录：阶段 1 为 `.kong/public-release-audit/20260928-stage1/`（审计报告、测试日志、验证笔记），阶段 2 为 `.kong/public-release-audit/20260929-stage2/verification-notes.md`；最终构建与候选为 `dist/stage1-final-20260929/` 与 `dist/stage1-final-candidate-20260929/`。这些目录与原始审计报告不会进入公开快照。`dist/stage1-verified-20260928/` 和 `dist/stage1-pathfix-check-20260929/` 是最终修复前的旧产物，仅作过程证据保留，不作为发布候选。后续阶段必须更新本清单的证据和状态，不能直接沿用本阶段数字证明最终发布版本。

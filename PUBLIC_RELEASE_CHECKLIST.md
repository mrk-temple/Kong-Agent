# Public release checklist — Kong-Agent 0.4.0rc5

2026-10-05：V0.4 在 RC5 的固定验收范围通过。本次将 RC5 整理为 GitHub 主分支更新，保留 `0.4.0rc5` 预发布版本。项目延续 [MIT](LICENSE) 许可，公开仓库为 [mrk-temple/Kong-Agent](https://github.com/mrk-temple/Kong-Agent)。

## 代码与验收身份

- [x] 冻结 Runtime 源码为 `9a3e23e621404a5be8f35bc277a02ccb6fcc2f94`；GitHub 整理仅修改公开文档，不改源码、版本或依赖锁文件。
- [x] 完整冻结批次 33/33 个物理试次：模型 32/32，恢复机制 1/1，未尝试 0。旧模型题 11/11、PLAN 6/6、变化题 16/16、错误完成 0/25；组间重叠，不相加。
- [x] 已观察反例阻断为 0。检查来自本地反例探针和源码复核，不代表第三方审计或任意任务可靠性。历史失败不重判、不拼接。
- [x] 冻结源码 Windows 全量 532 passed / 2 skipped；Linux 本地受限容器 523 passed / 11 skipped。两平台跳过范围、模型分母和未测量项见 [EVALUATION](EVALUATION.md)。
- [x] 源码目录外 wheel 安装 9/9；离线固定机制组 12/12、3/3、16/16；固定决策真实资源收尾 3/3、实际 host/Docker 工具任务 6/6。

## 公开内容与许可

- [x] `LICENSE` 为 MIT，版权主体 `mrk-temple`；不改变已有公开历史和仓库可见性。
- [x] 公开范围为 `src/kong/`、`tests/`、`examples/`、`.github/workflows/` 和显式根目录文件。内置技能及 references 保留。
- [x] 内部 `docs/`、`DESIGN.md`、交接单、计划、原始模型日志、本地配置、凭据、数据库和缓存不进入公开源码或包。
- [x] `.env.example` 仅提供空值模板；程序不会自动加载 `.env`。
- [x] README、架构、恢复、评测及 Changelog 统一说明当前 RC5 身份与能力边界。

## 上传前与上传后核对

每次文档或源码修改后重建最终包与候选，核对源码/ZIP/manifest、wheel/sdist 的实际成员和 SHA-256。原 RC5 包只证明冻结时身份；文档整理后的包需要新的哈希和安装核验。

扫描已知凭据值、常见模式和所有本地已获取 refs 可达历史；同时人工核对待推送提交的路径，不能只依靠 `.gitignore`。扫描报告仅记录脱敏位置，不复制匹配值。扫描不覆盖未知凭据格式、不可达对象、GitHub 缓存或他人 fork。

上传使用保留公开历史的正常提交与快进。推送后核对远端主分支 SHA 与最终文件，并等待对应提交的 [GitHub Actions](https://github.com/mrk-temple/Kong-Agent/actions) 完成 Windows、Linux 和 lint 检查。托管 runner 的平台限制可能使跳过数与本地不同，按实际日志报告；本地回归不能代替托管 CI。

## 可复现检查

```powershell
uv sync --frozen --all-extras --python 3.12
uv run --no-sync playwright install chromium
uv run --no-sync pytest -q -r s
uvx ruff@0.16.9 check .
uv lock --check
uv build --out-dir dist/<new-run>
uv run python examples/verify_release_install.py dist/<new-run>/kong_agent-0.4.0rc5-py3-none-any.whl
uv run python examples/audit_release_safety.py --output .kong/<new-audit>.json
uv run python examples/prepare_public_release.py --output dist/<new-candidate> --artifact dist/<new-run>/kong_agent-0.4.0rc5-py3-none-any.whl --artifact dist/<new-run>/kong_agent-0.4.0rc5.tar.gz
```

Linux 安装浏览器使用 `playwright install --with-deps chromium`。已知值比对可显式传入 `--secret-file`，文件只在本机内存读取。审计退出成功仅表示报告生成，发现项须分类；候选生成器拒绝有匹配值、内部文件或非法成员的候选。生成器的通用 `publication_blockers` 提醒不能取代本文件中的 MIT 许可和用户上传授权；它也不代表候选已上传。

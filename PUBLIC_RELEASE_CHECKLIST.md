# 发布检查 — Local Preview 0.0.0 / Runtime 0.5.0rc1

2026-10-13。延续 [mrk-temple/Kong-Agent](https://github.com/mrk-temple/Kong-Agent) 的公开主分支与 [MIT](LICENSE) 许可，作为预发布候选交付；保留本地开发分支、内部证据和运行数据。

## 发布范围

- `src/kong/`、`tests/`、`examples/`、`.github/workflows/`、`scripts/`、`web/`。
- README、公开用户/记忆/开发指南、架构、评测、恢复、版本说明、Changelog、依赖锁与空值配置模板。
- 内置技能及 references、GUI 构建资源和一张空白首次启动截图。
- `docs/`、设计/交接单、原始模型日志、凭据、本地模型配置、数据库、依赖目录和缓存继续在本地保留。

源码快照使用 `examples/prepare_public_release.py` 的显式允许列表。sdist 同时包含前端源码与 Windows 启动脚本，wheel 包含 Python 实现、技能和内置 GUI。依赖目录、TypeScript 缓存和运行数据不进入任何源码/包成员。

## 本轮验证

| 检查 | 结果 |
|---|---|
| 最新本地源码基线 | 909 passed / 5 skipped |
| 修整后完整工程 | 911 passed / 5 skipped / 0 failed，含两项新增 TUI 退出回归 |
| 前端与静态检查 | 7 tests、类型检查、构建、Ruff 0.16.9、锁文件检查通过 |
| 固定发布回归 | 7 类、13 项通过，0 skipped |
| 实际浏览器 + 离线 Provider | 9 组通过，page errors 0 |
| 源码目录外安装 | 12/12；隔离导入、离线演示、技能、GUI 资源、TUI、本地状态 |

五项跳过为两项 Windows 符号链接权限与三项真实 Docker 环境不足。安装探针的中文资源解码与身份输出换行已修正并重测；控制台测试入口及 TUI 退出竞态已复核。完整回归后，健康 Core 模式及回忆测试增加初始化确认，与专用超时和权限用例合计 48/48；该调整只涉及测试，不改变业务断言、生产限时或规则。当前源码身份、失败记录和历史模型样本边界见 [EVALUATION](EVALUATION.md)。

本轮没有追加收费模型请求。当前仍是 Windows 本地预览；完整 V0.5 效果、真人长期使用与跨平台产品验收尚未通过。

## 上传核验

每次发布都检查最终 wheel / sdist / ZIP 的成员、已知凭据值、常见凭据格式和 SHA-256；随后核对全部暂存路径及其确切 Git blob 字节与源码候选的一致性。扫描报告只保留脱敏位置和类别。

Git 使用普通提交与正常快进推送，保留公开历史。包含内部评测资料的旧开发分支只在本地保留。本轮扫描拟发布的 main 祖先链与已取得的本地可达历史，另独立检查发布快照；不打开本地用户 Memory 数据库。

GitHub Release 保留 prerelease 标识，上传扫描后的 wheel、sdist、源码 ZIP 和哈希清单。上传后核对远端主分支 SHA、仓库可见性、实际资源与本地工作树，再核对对应提交的 [GitHub Actions](https://github.com/mrk-temple/Kong-Agent/actions)。托管 CI 的平台结果按 Actions 实际状态报告，本地通过不能替代托管结果。

## 可复现入口

完整的环境、临时目录和打包命令见 [DEVELOPMENT](DEVELOPMENT.md)。核心命令为：

```powershell
uv run --no-sync python examples/local_preview_freeze.py --check
uv run --no-sync python examples/release_regression.py --output dist/fixed-release-check
uv build --out-dir dist/release
uv run --no-sync python examples/prepare_public_release.py --output dist/candidate --artifact dist/release/kong_agent-0.5.0rc1-py3-none-any.whl --artifact dist/release/kong_agent-0.5.0rc1.tar.gz
uv run --no-sync python examples/verify_release_install.py dist/release/kong_agent-0.5.0rc1-py3-none-any.whl --output E:\Projects\.workspace\tmp\kong-isolated-install
```

候选和安装输出必须使用新目录。生成器仍将源码标记为待审阅快照；其通用出版授权提醒不能代替本项目已有 MIT 许可、本轮用户发布指令及实际 GitHub 发布状态。

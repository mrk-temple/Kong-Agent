# 开发与发布核验

Python 3.12+；前端开发使用 Node.js 22.12+ 或 24。锁文件为 `uv.lock` 和 `web/package-lock.json`。运行源码 GUI 只需要 `local` / `tui` extras；全量测试还需要其他 extras 与本地 Chromium。

## 本地环境

在 E 盘项目目录的 PowerShell 中设置输出位置：

```powershell
$env:TEMP = 'E:\Projects\.workspace\tmp'
$env:TMP = $env:TEMP
$env:TMPDIR = $env:TEMP
$env:UV_CACHE_DIR = 'E:\Projects\.workspace\cache\uv'
$env:npm_config_cache = 'E:\Projects\.workspace\cache\npm'
$env:PLAYWRIGHT_BROWSERS_PATH = 'E:\Projects\.workspace\cache\playwright'
$env:PYTHONUTF8 = '1'
New-Item -ItemType Directory -Force $env:TEMP, $env:UV_CACHE_DIR, $env:npm_config_cache | Out-Null
uv sync --frozen --all-extras --python 3.12
uv run --no-sync playwright install chromium
```

## 离线检查

```powershell
uv run --no-sync pytest -q -r s --basetemp E:\Projects\.workspace\tmp\kong-release-tests
uvx ruff@0.16.9 check .
uv lock --check
uv run --no-sync python examples/release_regression.py
cd web
npm ci
npm test
npm run build
cd ..
```

前端构建写入 `src/kong/local_api/static/`。CI 在 Windows、Linux 运行工程测试，并单独检查前端构建和静态资源的一致性；它不使用模型密钥或实模评测。

## 源码身份与安装包

源码、评测工具、测试、技能和前端资源由 `examples/local_preview_freeze.py` 生成身份。指纹使用真实文件字节，`.gitattributes` 固定公开文本为 LF。改变代码或重新构建前端后重新冻结；旧模型样本属于旧身份。

```powershell
uv run --no-sync python examples/local_preview_freeze.py --output dist/source-manifest.json
uv run --no-sync python examples/local_preview_freeze.py --check
uv build --out-dir dist/release
uv run --no-sync python examples/prepare_public_release.py --output dist/candidate --artifact dist/release/kong_agent-0.5.0rc1-py3-none-any.whl --artifact dist/release/kong_agent-0.5.0rc1.tar.gz
uv run --no-sync python examples/verify_release_install.py dist/release/kong_agent-0.5.0rc1-py3-none-any.whl --output E:\Projects\.workspace\tmp\kong-isolated-install
```

候选和独立安装的输出目录必须是新的目录。安装检查使用 `python -I`，核对实际导入位置、离线演示、内置技能、GUI 资源和 TUI，避免误用源码目录。

`examples/audit_release_safety.py` 可扫描工作区和所有本地已取得 refs 的可达历史；使用 `--output` 明确指定报告。`--secret-file` 只在本机内存中比对指定值，报告不会输出匹配内容。发布前还需核对确切暂存文件和其字节。

## 权威与评测边界

Application Service 复用原 Runtime 的 Loop、Controller、PermissionPolicy、CompletionGate 和 SourceAccessGuard；Memory SQL 由 Core public API 管理，应用事件库不复制 Memory 正文。HTTP 适配负责本地配对、Origin / CSRF 和 DTO，前端不决定授权。

离线 fixture 的通过、Core 检索、请求注入、模型答案正确和来源诚实分别记录。`--live` 与实模验收脚本会产生付费请求，必须有明确预算授权。原始开发证据留在本地 `docs/`，公开源码和发行包仅保留审阅后的说明与可复现工具。

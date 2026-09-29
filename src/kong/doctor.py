"""Local diagnostics by default; provider/MCP probes require explicit --online."""
import asyncio
import tempfile
from importlib.metadata import version, PackageNotFoundError

from kong.config import load_config
from kong.web.config import load_web_config
from kong.integrations.config import load_servers, named_env
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


async def diagnose(args):
    checks = []
    def add(name, status, detail, fix=""):
        checks.append({"name":name, "status":status, "detail":detail, "fix":fix})
    workspace = Workspace(args.workspace)
    try:
        with tempfile.TemporaryFile(dir=workspace.root) as file:
            file.write(b"kong doctor")
            file.flush()
        add("workspace", "ok", "工作区可读写")
    except OSError:
        add("workspace", "error", "工作区写入检查失败", "检查目录是否存在以及当前用户权限")
    model_config = web_config = None
    try:
        model_config = load_config(args.config, args.profile)
        add("model.config", "ok", "模型配置格式有效")
        add("model.credential", "ok" if model_config.api_key.get_secret_value() else "warning",
            "已配置密钥" if model_config.api_key.get_secret_value() else "没有密钥；仅无需认证的本地服务可直接使用",
            "按配置中的 api_key_env 设置环境变量；不把密钥写进命令或报告")
    except (ValueError, OSError):
        add("model.config", "error", "模型配置缺失或格式错误", "复制 kong.example.toml 并填写端点、模型和 api_key_env；检查 --profile")
    try:
        web_config = load_web_config(args.config, args.web)
        status = web_config.status()
        ready = status["search_available"]
        add("web", "warning" if web_config.mode == "live" and not ready else "ok",
            f"模式 {web_config.mode}，搜索{'就绪' if ready else '未启用或缺少密钥'}",
            "联网搜索需选择 provider 并设置对应 api_key_env")
    except (ValueError, OSError):
        add("web", "error", "网页配置格式错误", "检查 [web] 字段，使用命名环境变量")
    for package, enabled in (("pywinpty", args.allow_terminal), ("playwright", args.allow_browser),
                             ("mcp", args.allow_mcp), ("jsonschema", args.allow_mcp)):
        try:
            add("package." + package, "ok", version(package))
        except PackageNotFoundError:
            add("package." + package, "error" if enabled else "skipped", "可选依赖未安装", "uv sync --extra integrations")
    registry = default_registry(workspace, allow_process=args.allow_process, skill_roots=args.skill_dir,
        web_config=web_config, allow_terminal=args.allow_terminal, allow_browser=args.allow_browser)
    try:
        catalog = registry.get("skill_list").catalog.listing(limit=100)
        blocked = [s["id"] for s in catalog["skills"] if s["status"] == "blocked"]
        add("skills", "warning" if blocked or catalog["diagnostics"] else "ok",
            "技能目录已检查；受阻技能：" + (", ".join(blocked) or "无"),
            "使用 kong skills --json 查看缺失项；安装需要的 skills 依赖或启用所需工具")
        if args.allow_terminal:
            tool = registry.get("terminal")
            try:
                result = await tool.run(operation="start", argv=["python", "-c", "print('kong-doctor')"], wait_seconds=1)
                if result.success and result.output.get("running"):
                    result = await tool.run(operation="read", session=result.output["session"], wait_seconds=1)
                ok = result.success and result.output.get("exit_code") == 0
                add("terminal.launch", "ok" if ok else "error", "ConPTY 启动/退出检查" , "当前仅支持 Windows；检查交互依赖和系统权限")
            except Exception as exc:
                add("terminal.launch", "error", type(exc).__name__, "安装 integrations 依赖；检查 Windows ConPTY")
        if args.allow_browser:
            try:
                await asyncio.wait_for(registry.get("browser").start(), timeout=30)
                add("browser.launch", "ok", "Chromium 实际启动成功；未访问网站")
            except Exception as exc:
                add("browser.launch", "error", type(exc).__name__, "python -m playwright install chromium")
        servers = {}
        if args.allow_mcp:
            try:
                servers = load_servers(args.config)
                for server in servers.values():
                    named_env([*server.env_names, *server.header_env.values()])
                add("mcp.config", "ok" if servers else "warning", f"已配置 {len(servers)} 个 MCP 服务；凭据变量检查完成",
                    "在 [mcp.servers.NAME] 配置服务器")
            except (ValueError, OSError):
                add("mcp.config", "error", "配置或命名凭据变量无效", "检查 MCP 配置和指定的环境变量")
        if not args.online:
            add("network", "skipped", "未调用模型、搜索服务或 MCP；使用 --online 显式检查（可能计费）")
        else:
            if model_config:
                from kong.models.compatible import CompatibleModel
                from kong.context import Context
                try:
                    model_config.max_tokens = min(model_config.max_tokens, 256)
                    await CompatibleModel(model_config).generate(Context(messages=[{"role":"user", "content":'Return JSON only: {"kind":"respond","content":"ready"}'}]))
                    add("model.online", "ok", "模型完成一次真实请求")
                except Exception as exc:
                    add("model.online", "error", type(exc).__name__, "检查服务可用性、模型、认证和 JSON 支持；不自动重试")
            if web_config and web_config.mode == "live" and web_config.status()["search_available"]:
                from kong.web.service import WebService
                try:
                    await WebService(workspace.root, web_config).search("Python official documentation", count=1)
                    add("web.online", "ok", "搜索服务完成一次真实请求")
                except Exception as exc:
                    add("web.online", "error", type(exc).__name__, "检查搜索服务凭据、网络和显式代理配置")
            if servers:
                from kong.integrations.mcp import MCPTool
                tool = MCPTool(workspace, servers)
                registry.register(tool)
                for name in servers:
                    result = await tool.run(operation="list", server=name)
                    add("mcp.online." + name, "ok" if result.success else "error", "工具发现成功" if result.success else "连接或工具发现失败",
                        "检查服务进程/端点及凭据；未调用业务工具")
    finally:
        await registry.aclose()
    return {"online":args.online, "ok":not any(c["status"] == "error" for c in checks), "checks":checks}

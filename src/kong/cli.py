"""Small terminal client; human commands share the same controller contracts."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

from kong.config import load_config
from kong.web.config import load_web_config
from kong.contracts import Mode, Plan, Status
from kong.continuity.storage import ThreadStore
from kong.continuity.memory_store import MemoryStore
from kong.continuity.retrieval import scope_matches
from kong.demo import demo_model
from kong.models.compatible import CompatibleModel
from kong.runtime.loop import Runtime
from kong.storage import RunStore
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


HELP = """命令：
  /plan                 查看当前计划与修订号
  /approve <修订号>      批准所展示的这一版计划
  /discuss <意见>        讨论计划；不会执行工具
  /edit <JSON文件路径>   用本地 Plan JSON 修订计划；仍需重新批准
  /answer <回答>        回答待处理问题
  /resolve <实际情况>    确认中断动作的实际情况，允许继续检查
  /reject               拒绝并停止任务
  /resume               恢复被用户中断的任务（不重置预算）
  /add <约束>           运行中补充约束；下一安全边界生效并撤销旧批次
  /goal <新目标>        运行中替换目标；撤销旧计划与旧验收条件
  /cancel               取消模型等待；已开始的动作收尾后停止，不再开始新动作
  /status /history      查看状态或最近记录
  /context              查看最近一次实际模型上下文、预算与来源
  /skills               查看技能目录和缺失依赖
  /memory               查看 Thread 条目及长期记忆（含待确认项）
  /memory-confirm <id>  确认指定内容；不等于批准执行计划
  /memory-reject <id>   拒绝记忆候选
  /memory-resolve <id>  解决未完成事项
  /memory-forget <id>   停用记忆，保留来源审计
  /memory-rebuild       从当前 Thread 的原始记录重建派生条目
  /remember <kind> <key> <scope> <text>  明确提出一条记忆
  /quit                 保存并退出
"""


def event_output(kind: str, data: dict) -> None:
    if kind == "model_waiting":
        print("  等待模型生成完整决策；运行中可输入 /add、/goal、/cancel。", flush=True)
    elif kind == "model_output":
        print(f"  正在接收模型输出（{data['received_chars']} 字符）；尚未授权动作。", flush=True)
    elif kind == "model_retry":
        print(f"  模型 {data['category']}：{data['delay_seconds']:.2f} 秒后重试 {data['attempt']}/{data['limit']}。", flush=True)
    elif kind in {"input_queued", "input_applied"}:
        print(f"  输入 {data['kind']}：{'已排队' if kind == 'input_queued' else '已生效'}。", flush=True)
    elif kind == "action_unknown":
        print("  动作结果未知：" + ", ".join(data['ids']) + "；检查环境后 /resolve。", flush=True)
    elif kind in {"completed", "paused", "cancelled", "stopped", "failed"}:
        print(f"  运行状态：{kind}（{data['reason']}）。", flush=True)
    elif kind == "action_started":
        print(f"  [{data['id']}] 正在使用 {data['tool']}；Ctrl+C 可中断。", flush=True)
    elif kind == "observation":
        print(f"  [{data['id']}] {data['tool']}：{'成功' if data['success'] else '失败'}")
    elif data.get("control") == "escalate":
        print(f"  第 {data['turn']} 轮：进入进展检查（{data['reason']}）")
    elif data.get("reason", "").startswith(("lease_renewed", "bounded_recovery")):
        print(f"  第 {data['turn']} 轮：{data['reason']}")


def print_plan(runtime: Runtime) -> None:
    plan = runtime.state.plan
    if not plan:
        print("当前没有显式计划。")
        return
    print(f"\n计划修订 #{plan.revision} · {'已批准' if plan.approved_revision == plan.revision else '待批准'}")
    print(plan.goal)
    print(f"理由：{plan.rationale}")
    for step in plan.steps:
        print(f"  {'✓' if step.done else '○'} {step.id}  {step.title}")
    print("验收条件：")
    for criterion in plan.success_criteria:
        print(f"  {criterion.id}  {criterion.description} ({criterion.kind})")
        if criterion.path:
            print(f"    文件：{criterion.path}")
        if criterion.contains:
            print(f"    必须包含：{criterion.contains}")
        if criterion.tool_name:
            print("    工具验收：" + json.dumps({k:v for k,v in criterion.model_dump().items()
                if k in {"tool_name","operation","arguments_match","output_match","output_contains","after_last_write"}}, ensure_ascii=False))


def print_status(runtime: Runtime) -> None:
    state = runtime.state
    print(f"\n任务 {state.run_id}\n{state.mode.value.upper()} / {state.strategy.value} / {state.status.value}")
    print(f"Thread {runtime.thread.thread_id}")
    print(f"模型调用 {state.turn_count}/{state.hard_turn_limit} · 租约到第 {state.turn_budget} 轮 · 动作 {len(runtime.history.observations)}")
    if state.pending_approval:
        print_plan(runtime)
        print(f"批准请输入 /approve {state.pending_approval.revision}，或使用 /discuss、/edit、/reject。")
    if state.pending_question:
        print(state.pending_question)
    if state.final_output:
        print("\n" + state.final_output)
    if state.stop_reason and state.status != Status.COMPLETED:
        print("停止原因：" + state.stop_reason)
    from kong.reporting import run_report
    report = run_report(runtime)
    verified = sum(c["verified"] for c in report["criteria"])
    metrics = report["metrics"]
    print(f"验收：{verified}/{len(report['criteria'])} 个显式条件通过；执行耗时 {metrics['active_seconds']:.1f} 秒。")
    if not report["verification"]["business_acceptance_declared"]:
        print("业务验收未声明：当前仅记录机械检查；代码/数据任务可使用 --task-family 与 --completion-contract。")
    timing = metrics["tool_timing_complete"]
    print(f"模型等待 {metrics['model_seconds']:.3f} 秒 · 工具调用 {metrics['tool_calls']} 次 · "
          + (f"工具批次等待 {metrics['tool_wall_seconds']:.3f} 秒 · 工具累计 {metrics['tool_seconds']:.3f} 秒 · "
             f"Runtime 其余耗时 {metrics['runtime_overhead_seconds']:.3f} 秒" if timing else "历史工具耗时不完整"))
    tokens = metrics["tokens"].get("total_tokens")
    print("Token 用量：" + (str(tokens) if tokens is not None else "未知")
          + ("（部分请求未返回用量）" if not metrics["usage_complete"] else ""))
    for artifact in report["artifacts"]:
        print(f"产物：{artifact['path']}（{'存在' if artifact['exists'] else '当前不存在'}）")
    for item in report["unverified"]:
        print("未验证：" + item)
    for warning in report["recovery_warnings"][-1:]:
        print("恢复提示：历史交互句柄不可用，需要检查后重新建立：" + json.dumps(warning, ensure_ascii=False))
    if runtime.store:
        print("任务摘要：" + str(runtime.store.report_path(state.run_id)))
    if runtime.context_engine.store:
        pending = [i for i in runtime.context_engine.store.state(runtime.thread.thread_id).items if i.status == "candidate"]
        for entry in pending:
            print(f"待确认记忆 {entry.id}: {entry.content}\n  /memory-confirm {entry.id}")
        pending_ids = {entry.id for entry in pending}
        for memory_store in {s.path: s for s in [runtime.context_engine.store, runtime.context_engine.long_term_store]}.values():
            for record in memory_store.memories():
                if record.status == "candidate" and record.thread_id == runtime.thread.thread_id and record.thread_item_id not in pending_ids:
                    print(f"待确认长期记忆 {record.id}: {record.content}\n  /memory-confirm {record.id}")


def print_context(runtime: Runtime) -> None:
    packet = runtime.context_engine.last_packet
    if packet is None or packet.run_id != runtime.state.run_id:
        print("此 Run 尚无已记录的模型上下文。")
    else:
        print(packet.model_dump_json(indent=2))


def print_memory(runtime: Runtime) -> None:
    engine = runtime.context_engine
    if not engine.store:
        print("当前未启用持久记忆。")
        return
    print("Thread Memory:")
    for entry in engine.store.state(runtime.thread.thread_id).items:
        print(f"{entry.id} [{entry.status}] {entry.kind} {entry.key}: {entry.content}")
    print("Long-term Memory:")
    for store in {s.path: s for s in [engine.store, engine.long_term_store]}.values():
        for record in store.memories():
            if scope_matches(record, runtime.thread, engine.project_id):
                print(f"{record.id} [{record.status}] {record.scope}/{record.kind}: {record.content}")


async def context_command(runtime: Runtime, line: str) -> bool:
    command, _, argument = line.partition(" ")
    if command == "/skills":
        print_skills(runtime.registry.get("skill_list").catalog)
    elif command == "/context":
        print_context(runtime)
    elif command == "/memory":
        print_memory(runtime)
    elif command == "/remember":
        kind, key, scope, text = argument.split(" ", 3)
        await runtime.memory_command("add", kind=kind, key=key, scope=scope, text=text)
        print_memory(runtime)
    elif command == "/memory-rebuild":
        await runtime.memory_command("rebuild")
        print_memory(runtime)
    elif command in {"/memory-confirm", "/memory-reject", "/memory-resolve", "/memory-forget"}:
        await runtime.memory_command(command.removeprefix("/memory-"), item_id=argument.strip())
        print_memory(runtime)
    else:
        return False
    return True


async def drive(runtime: Runtime, interactive: bool, reader=None) -> int:
    from kong.runtime.terminal_input import TerminalInput, run_with_input
    if interactive and reader is None:
        reader = TerminalInput()
    while True:
        if interactive:
            await run_with_input(runtime, reader)
        else:
            await runtime.run()
        if not getattr(runtime, "events_json", False):
            print_status(runtime)
        state = runtime.state
        if not interactive or state.status in {Status.COMPLETED, Status.FAILED}:
            return 0 if state.status == Status.COMPLETED else 2
        if state.status == Status.STOPPED and state.stop_reason != "user_interrupted":
            return 2
        try:
            line = (await reader.get("\nKong > ")).strip()
        except EOFError:
            runtime.save()
            return 2
        command, _, argument = line.partition(" ")
        try:
            if command in {"/quit", "/exit"}:
                runtime.save()
                print(f"已保存。恢复命令：kong resume {state.run_id}")
                return 0
            if await context_command(runtime, line):
                continue
            if command == "/help":
                print(HELP)
            elif command == "/plan":
                print_plan(runtime)
            elif command == "/status":
                print_status(runtime)
            elif command == "/history":
                for event in runtime.history.events[-12:]:
                    print(event.model_dump_json())
            elif command == "/approve":
                runtime.human("approve", revision=int(argument))
            elif command == "/edit":
                plan = Plan.model_validate_json(Path(argument.strip('"')).read_text(encoding="utf-8-sig"))
                runtime.human("edit", plan=plan)
            elif command in {"/answer", "/discuss"}:
                runtime.human(command[1:], text=argument)
            elif command in {"/reject", "/resume"}:
                runtime.human(command[1:])
            elif command == "/resolve":
                runtime.resolve_interrupted_action(argument)
            elif line and not line.startswith("/"):
                runtime.human("discuss" if state.pending_approval else "answer", text=line)
            else:
                print(HELP)
        except (ValueError, OSError):
            # Pydantic errors may contain user-supplied values, including accidental secrets.
            print("命令未执行：请检查当前状态、修订号、文件路径或 Plan JSON 格式。输入 /help 查看用法。")


def print_skills(catalog):
    data = catalog.listing(limit=len(catalog.skills))
    for skill in data["skills"]:
        print(f"{skill['id']} [{skill['status']}] {skill['description']}")
        if skill["missing"]:
            print("  缺少：" + ", ".join(skill["missing"]))
        if skill["optional_missing"]:
            print("  可选依赖未就绪：" + ", ".join(skill["optional_missing"]))
    for diagnostic in data["diagnostics"]:
        print("  技能诊断：" + diagnostic)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="kong", description="Kong V0.3 — Local Agent Runtime")
    from kong import __version__
    result.add_argument("--version", action="version", version="Kong " + __version__)
    result.add_argument("--allow-terminal", action="store_true", help="启用交互式终端（Windows ConPTY）")
    result.add_argument("--allow-browser", action="store_true", help="启用隔离 Chromium 浏览器，含本地开发页面")
    result.add_argument("--allow-mcp", action="store_true", help="启用配置中明确指定的 MCP 服务器")
    result.add_argument("--web", choices=["disabled", "cached", "live"], help="覆盖本次启动的联网模式")
    result.add_argument("--skill-dir", type=Path, action="append", default=[],
                        help="附加技能根目录（包含多个技能子目录）；可重复，需重启刷新")
    result.add_argument("--allow-process", action="store_true",
                        help="启用本次启动的本机命令执行（用户权限，非工作区沙箱）；恢复时需再次显式携带")
    result.add_argument("--workspace", type=Path, default=Path.cwd())
    result.add_argument("--config", type=Path, default=Path("kong.local.toml"))
    result.add_argument("--profile", help="TOML model profile: deepseek / relay / local / lmstudio")
    result.add_argument("--stream", action="store_true", help="启用模型 SSE 流；完整校验前不执行动作")
    result.add_argument("--events-json", action="store_true", help="以 JSON Lines 输出版本化运行事件（搭配 --no-interactive）")
    result.add_argument("--read-concurrency", type=int, choices=range(1, 9), default=3)
    result.add_argument("--model-retries", type=int, choices=range(0, 4), default=2)
    result.add_argument("--permissions", type=Path, help="操作与资源范围 PermissionPolicy JSON；本次启动重新授权")
    result.add_argument("--sandbox", choices=["host", "docker"], default="host")
    result.add_argument("--sandbox-root", help="Docker 挂载的专用工作区子目录")
    result.add_argument("--sandbox-image", default="python:3.12-slim", help="已在本机安装的可信 Linux 镜像；不自动拉取")
    result.add_argument("--global-memory-db", type=Path, default=Path.home() / ".kong" / "memory.db",
                        help="跨工作区的 GLOBAL 记忆数据库；工作区记忆仍在 .kong/context.db")
    result.add_argument("--project-id", help="PROJECT 记忆隔离标识；未指定时不读取 PROJECT 记忆")
    commands = result.add_subparsers(dest="command")
    doctor = commands.add_parser("doctor", help="统一检查配置、依赖和本地运行环境")
    doctor.add_argument("--online", action="store_true", help="显式调用模型、启用的搜索和 MCP 工具发现，可能计费")
    doctor.add_argument("--json", action="store_true")
    summary = commands.add_parser("summary", help="读取已保存任务摘要，不调用模型")
    summary.add_argument("run_id")
    commands.add_parser("tools", help="查看本次启用的工具和交互依赖，不启动浏览器或 MCP")
    commands.add_parser("web", help="查看联网配置与密钥是否就绪；不联网、不调用模型")
    skills = commands.add_parser("skills", help="查看内置/项目技能及依赖，不调用模型")
    skills.add_argument("name", nargs="?", help="查看指定技能的完整说明")
    skills.add_argument("--check", action="store_true", help="缺失必需依赖或包无效时退出码为 2")
    skills.add_argument("--json", action="store_true")
    run = commands.add_parser("run", help="开始一个任务")
    run.add_argument("goal")
    run.add_argument("--thread", help="在已有的当前工作区 Thread 中开始新 Run")
    run.add_argument("--mode", choices=[m.value for m in Mode], default="auto")
    run.add_argument("--no-interactive", action="store_true")
    run.add_argument("--criteria", type=Path, help="JSON array of Criterion contracts")
    run.add_argument("--completion-contract", type=Path, help="调用方提供的代码/数据任务完成合同 JSON")
    run.add_argument("--task-family", choices=["code", "data"], help="显式代码/数据入口；必须提供同类完成合同")
    resume = commands.add_parser("resume", help="恢复一个已保存任务")
    resume.add_argument("run_id")
    resume.add_argument("--no-interactive", action="store_true")
    show = commands.add_parser("show", help="查看任务快照，不调用模型")
    show.add_argument("run_id")
    show.add_argument("--plan-json", action="store_true", help="输出可供 /edit 使用的 Plan JSON")
    commands.add_parser("runs", help="列出当前工作区任务")
    commands.add_parser("threads", help="列出当前工作区 Thread 及其 Run")
    context = commands.add_parser("context", help="查看已保存的实际模型上下文，不调用模型")
    context.add_argument("run_id")
    memory = commands.add_parser("memory", help="查看或管理指定 Run 的记忆，不调用模型")
    memory.add_argument("run_id")
    memory.add_argument("action", nargs="?", default="list", choices=["list", "confirm", "reject", "resolve", "forget", "add", "rebuild"])
    memory.add_argument("item_id", nargs="?", default="")
    memory.add_argument("--text", default="")
    memory.add_argument("--kind", default="fact")
    memory.add_argument("--key", default="")
    memory.add_argument("--scope", default="thread", choices=["global", "workspace", "project", "thread"])
    demo = commands.add_parser("demo", help="离线演示，不需要模型或密钥")
    demo.add_argument("--auto-approve", action="store_true", help="明确批准离线演示计划，自动完成演示")
    return result


async def async_main(args) -> int:
    registries = []
    def make_registry(workspace, **kwargs):
        from kong.integrations.config import load_servers
        if args.sandbox == "docker":
            from kong.environments.docker import DockerBackend
            if args.allow_terminal or args.allow_browser or args.allow_mcp:
                raise ValueError("Docker mode covers process_exec; terminal/browser/MCP require separate backends and are disabled")
            if kwargs.get("web_config") and kwargs["web_config"].mode != "disabled":
                raise ValueError("Docker mode requires --web disabled; model transport stays outside the workload sandbox")
            if not args.sandbox_root or not args.allow_process:
                raise ValueError("Docker mode requires --allow-process and --sandbox-root")
            kwargs["process_backend"] = DockerBackend(workspace, args.sandbox_root, args.sandbox_image)
        registry = default_registry(workspace, **kwargs,
            allow_terminal=getattr(args, "allow_terminal", False),
            allow_browser=getattr(args, "allow_browser", False),
            mcp_servers=load_servers(args.config) if getattr(args, "allow_mcp", False) else None)
        registries.append(registry)
        return registry
    try:
        return await _async_main(args, make_registry)
    finally:
        for registry in reversed(registries):
            await registry.aclose()


async def _async_main(args, default_registry) -> int:
    completion_contract = None
    if args.command == "run":
        from kong.completion_contracts import CompletionContract, contract_for_task_family
        if args.completion_contract:
            completion_contract = CompletionContract.model_validate_json(args.completion_contract.read_text(encoding="utf-8-sig"))
        completion_contract = contract_for_task_family(args.task_family, completion_contract)
    workspace = Workspace(args.workspace)
    from kong.tools.permissions import load_permissions
    from kong.runtime.terminal_input import TerminalInput
    reader = None
    def get_reader():
        nonlocal reader
        if reader is None:
            reader = TerminalInput()
        return reader
    def configure(runtime):
        runtime.events_json = args.events_json
        return runtime
    permissions = load_permissions(args.permissions)
    runtime_options = dict(permissions=permissions, read_concurrency=args.read_concurrency,
                           model_retry_limit=args.model_retries)
    def events(kind, data):
        if args.events_json:
            print(json.dumps(data["event"], ensure_ascii=False), flush=True)
        else:
            event_output(kind, data)
    def load_model():
        config = load_config(args.config, args.profile)
        if args.stream:
            config.stream = True
        return CompatibleModel(config)
    if args.command == "doctor":
        from kong.doctor import diagnose
        report = await diagnose(args)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            for check in report["checks"]:
                print(f"[{check['status']}] {check['name']}: {check['detail']}")
                if check["status"] in {"error", "warning"} and check["fix"]:
                    print("  处理方式：" + check["fix"])
        return 0 if report["ok"] else 2
    if args.command == "summary":
        store = RunStore(workspace.root / ".kong" / "runs")
        path = store.report_path(args.run_id)
        if not path.exists():
            print("此任务没有已保存摘要；旧任务在下次运行或恢复后生成。")
            return 2
        print(path.read_text(encoding="utf-8"))
        return 0
    if args.command == "tools":
        from importlib.metadata import version, PackageNotFoundError
        registry = default_registry(workspace, allow_process=args.allow_process,
            skill_roots=args.skill_dir, web_config=load_web_config(args.config, args.web))
        packages = {}
        for name in ("pywinpty", "playwright", "mcp", "jsonschema"):
            try:
                packages[name] = version(name)
            except PackageNotFoundError:
                packages[name] = "missing; uv sync --extra integrations"
        print(json.dumps({"tools": registry.names(), "packages": packages,
            "mcp_servers": list(registry.get("mcp").servers) if "mcp" in registry.names() else [],
            "browser_binary": "Install separately: python -m playwright install chromium",
            "terminal_platform": "Windows ConPTY"}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "web":
        print(json.dumps(load_web_config(args.config, args.web).status(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "skills":
        registry = default_registry(workspace, allow_process=args.allow_process, skill_roots=args.skill_dir, web_config=load_web_config(args.config, args.web))
        catalog = registry.get("skill_list").catalog
        if args.name:
            data = catalog.load(args.name)
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 2 if args.check and data["status"] == "blocked" else 0
        data = catalog.listing(limit=len(catalog.skills))
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print_skills(catalog)
        return 2 if args.check and (data["diagnostics"] or any(s["status"] == "blocked" for s in data["skills"])) else 0
    store = RunStore(workspace.root / ".kong" / "runs")
    if args.command == "context":
        store.load(args.run_id)  # Validate ID and original Run existence.
        packet = MemoryStore(workspace.root / ".kong" / "context.db").packet(args.run_id)
        print(packet.model_dump_json(indent=2) if packet else "此 Run 尚无已记录的模型上下文。")
        return 0
    if args.command == "memory":
        from kong.models.fake import ScriptedModel
        with store.lock(args.run_id):
            runtime = Runtime.restore(store.load(args.run_id), ScriptedModel([]), workspace, default_registry(workspace),
                                      store=store, memory_store=MemoryStore(args.global_memory_db), project_id=args.project_id)
            if args.action != "list":
                await runtime.memory_command(args.action, item_id=args.item_id, text=args.text,
                                             kind=args.kind, key=args.key, scope=args.scope)
            print_memory(runtime)
        return 0
    if args.command == "threads":
        threads = ThreadStore(workspace.root / ".kong" / "context.db")
        for thread in threads.list_threads(workspace.root):
            print(f"{thread.thread_id}  {thread.status}  {thread.title[:80]}")
            for run_id in threads.runs(thread.thread_id):
                print(f"  {run_id}")
        return 0
    if args.command == "runs":
        for item in store.list_runs():
            print(f"{item['run_id']}  {item['status']}  {item['goal'][:80]}")
        return 0
    if args.command == "show":
        snapshot = store.load(args.run_id)
        if args.plan_json:
            if not snapshot.state.plan:
                raise ValueError("Run has no plan")
            print(snapshot.state.plan.model_dump_json(indent=2))
        else:
            print(snapshot.state.model_dump_json(indent=2))
        return 0
    if args.command == "demo":
        demo_path = workspace.root / ".kong" / "demos" / uuid4().hex
        demo_path.mkdir(parents=True)
        demo_workspace = Workspace(demo_path)
        runtime = Runtime.new("创建一份 Kong V0.1 介绍文件", demo_model(), demo_workspace,
                              default_registry(demo_workspace), Mode.PLAN, store=store, on_event=events, driver="demo", **runtime_options)
        configure(runtime)
        print(f"离线演示工作区：{demo_path}")
        with store.lock(runtime.state.run_id):
            if args.auto_approve:
                await runtime.run()
                print_plan(runtime)
                runtime.human("approve", revision=runtime.state.plan_revision)
                return await drive(runtime, False)
            return await drive(runtime, True, get_reader())
    if args.command == "resume":
        with store.lock(args.run_id):
            snapshot = store.load(args.run_id)
            if snapshot.driver == "demo":
                demo_root = Path(snapshot.workspace).resolve()
                if not demo_root.is_relative_to(workspace.root / ".kong" / "demos"):
                    raise ValueError("Demo workspace is outside the original demo directory")
                workspace = Workspace(demo_root)
                model = demo_model(snapshot.state.turn_count)
            else:
                model = load_model()
            runtime = Runtime.restore(snapshot, model, workspace, default_registry(workspace, allow_process=args.allow_process, skill_roots=args.skill_dir, web_config=load_web_config(args.config, args.web)), store=store, on_event=events,
                                      memory_store=MemoryStore(args.global_memory_db), project_id=args.project_id, **runtime_options)
            configure(runtime)
            if runtime.state.status == Status.STOPPED and runtime.state.stop_reason == "user_interrupted" and not runtime.pending_action_id:
                runtime.human("resume")
            return await drive(runtime, not args.no_interactive, get_reader() if not args.no_interactive else None)
    model = load_model()
    registry = default_registry(workspace, allow_process=args.allow_process, skill_roots=args.skill_dir, web_config=load_web_config(args.config, args.web))
    if args.command == "run":
        runtime = Runtime.new(args.goal, model, workspace, registry, Mode(args.mode), store=store,
                              thread_id=args.thread, on_event=events,
                              memory_store=MemoryStore(args.global_memory_db), project_id=args.project_id, **runtime_options)
        configure(runtime)
        if args.criteria:
            from pydantic import TypeAdapter
            from kong.contracts import Criterion
            runtime.state.success_criteria = TypeAdapter(list[Criterion]).validate_json(args.criteria.read_text(encoding="utf-8-sig"))
        if completion_contract:
            runtime.set_completion_contract(completion_contract)
        with store.lock(runtime.state.run_id):
            return await drive(runtime, not args.no_interactive, get_reader() if not args.no_interactive else None)
    # One terminal conversation is a Thread; each goal keeps its own Run budget.
    thread_id = None
    runtime = None
    print("Kong V0.3 · AUTO · 输入目标开始，/skills 查看技能，/context 查看上下文，/quit 退出。")
    while True:
        try:
            goal = (await get_reader().get("\n你 > ")).strip()
        except EOFError:
            return 0
        if goal == "/quit":
            return 0
        if not goal:
            continue
        if goal.startswith("/"):
            try:
                if goal == "/skills":
                    print_skills(registry.get("skill_list").catalog)
                elif goal == "/help":
                    print(HELP)
                elif runtime is None or not await context_command(runtime, goal):
                    print(HELP)
            except (ValueError, OSError):
                print("命令未执行：检查记忆 ID、kind/key/scope 或当前 Thread。")
            continue
        runtime = Runtime.new(goal, model, workspace, registry, store=store, on_event=events,
                              thread_id=thread_id, memory_store=MemoryStore(args.global_memory_db), project_id=args.project_id,
                              **runtime_options)
        configure(runtime)
        thread_id = runtime.thread.thread_id
        with store.lock(runtime.state.run_id):
            await drive(runtime, True, get_reader())


def main() -> None:
    # Windows pipes otherwise use the system code page and corrupt Chinese output.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args()
    try:
        code = asyncio.run(async_main(args))
    except KeyboardInterrupt:
        print("\n任务已中断；已开始的任务可在 kong runs 中查找。")
        code = 130
    except (OSError, ValueError) as exc:
        from pydantic import ValidationError
        if isinstance(exc, ValidationError):
            print("配置或快照格式不合法，请检查本地文件。", file=sys.stderr)
        else:
            print(f"Kong: {exc}", file=sys.stderr)
        code = 1
    raise SystemExit(code)

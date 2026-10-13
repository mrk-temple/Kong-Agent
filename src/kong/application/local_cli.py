"""Windows local startup and an optional shared-service text client."""

import asyncio
import json
import os
from pathlib import Path
import re
import socket
from uuid import uuid4
from pydantic import SecretStr
from kong.application.client import LocalClient
from kong.application.contracts import (
    ThreadCreate,
    RunCreate,
    HumanCommand,
    InputCommand,
    FreshCommand,
    AppError,
)
from kong.config import ModelConfig, load_config


def add_parser(commands):
    local = commands.add_parser(
        "local", help="Kong Local Preview 0.0.0 · GUI / CUI / TUI"
    )
    local.add_argument(
        "action", choices=["serve", "tui", "chat", "run", "status", "memory", "resume"]
    )
    local.add_argument("--data-root", type=Path, default=Path.cwd() / ".local-preview")
    local.add_argument(
        "--connect", help="连接已有的 127.0.0.1 服务，读取 KONG_LOCAL_TOKEN"
    )
    local.add_argument("--port", type=int, default=0, help="0 自动选择可用端口")
    local.add_argument("--no-browser", action="store_true")
    local.add_argument(
        "--api-file", type=Path, help="后端读取既有 MiMo API 文件，不复制或输出密钥"
    )
    local.add_argument("--text", default="")
    local.add_argument("--intent", choices=["chat", "task"], default="chat")
    local.add_argument("--mode", choices=["fast", "auto", "plan"], default="auto")
    local.add_argument(
        "--acceptance", type=Path, help="用户明确确认的 Criterion 数组 JSON"
    )
    local.add_argument("--run-id")


def prepare_environment(root):
    root = root.resolve()
    for name, folder in [
        ("TEMP", "tmp"),
        ("TMP", "tmp"),
        ("TMPDIR", "tmp"),
        ("UV_CACHE_DIR", "cache/uv"),
    ]:
        path = root / folder
        path.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(path)
    return root


def model_configuration(args):
    if args.api_file:
        raw = args.api_file.read_text(encoding="utf-8-sig")
        match = re.search(r"(?im)^\s*Mimo\s*API\s*Key\s*[:：=]\s*(\S+)\s*$", raw)
        if not match:
            raise ValueError("没有找到明确的 MiMo 凭据标签")
        return ModelConfig(
            base_url="https://api.xiaomimimo.com/v1",
            model="mimo-v2.6-flash",
            api_key=SecretStr(match.group(1).strip(chr(34) + chr(39))),
            stream=True,
            json_mode=True,
            thinking_mode="disabled",
            max_tokens=1400,
            timeout=45,
        )
    if args.config.exists():
        return load_config(args.config, args.profile)
    return None


def launch(args):
    root = prepare_environment(args.data_root)
    config = model_configuration(args)
    if args.action == "serve":
        try:
            import uvicorn
            from kong.local_api.app import create_app
        except ImportError:
            raise ValueError("GUI 需要 local extra：uv sync --extra local") from None
        if args.connect:
            raise ValueError("serve 不能同时连接其他服务")
        if not 0 <= args.port <= 65535:
            raise ValueError("端口范围为 0–65535")
        sock = socket.socket()
        sock.bind(("127.0.0.1", args.port))
        origin = "http://127.0.0.1:" + str(sock.getsockname()[1])
        app = create_app(root, origin=origin, model_config=config)
        pair_url = origin + "/#pair=" + app.state.session.token
        print("Kong Local Preview 0.0.0 · " + origin, flush=True)
        print("本地配对链接（仅此机器、本次进程）： " + pair_url, flush=True)
        if not args.no_browser:
            import webbrowser

            webbrowser.open(pair_url)
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=int(origin.rsplit(":", 1)[1]),
                access_log=False,
                log_level="warning",
                timeout_graceful_shutdown=5,
            )
        )
        try:
            server.run(sockets=[sock])
        finally:
            sock.close()
        return 0
    if args.action == "tui":
        try:
            from kong.tui.app import run_tui
        except ImportError:
            raise ValueError("TUI 需要 tui extra：uv sync --extra tui") from None
        return (
            asyncio.run(run_tui(root, connect=args.connect, model_config=config)) or 0
        )
    return asyncio.run(text_main(args, root, config))


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


async def text_main(args, root, config):
    async with LocalClient(root, connect=args.connect, model_config=config) as client:
        if args.action == "status":
            output(
                await client.run_detail(args.run_id)
                if args.run_id
                else await client.list_threads()
            )
            return 0
        if args.action == "memory":
            output(await client.memory_records())
            return 0
        if args.action == "resume":
            if not args.run_id:
                raise ValueError("resume 需要 --run-id")
            await client.human(
                args.run_id, HumanCommand(command_id=uuid4().hex, kind="resume")
            )
            await client.wait(args.run_id)
            output(await client.run_detail(args.run_id))
            return 0
        thread = await client.create_thread(
            ThreadCreate(command_id=uuid4().hex, title=args.text[:50] or "终端对话")
        )
        criteria = (
            json.loads(args.acceptance.read_text(encoding="utf-8-sig"))
            if args.acceptance
            else []
        )
        if args.action == "run":
            receipt = await client.create_run(
                thread["thread_id"],
                RunCreate(
                    command_id=uuid4().hex,
                    text=args.text,
                    intent=args.intent,
                    mode=args.mode,
                    acceptance=criteria,
                ),
            )
            await client.wait(receipt["run_id"])
            output(await client.run_detail(receipt["run_id"]))
            return 0
        from kong.runtime.terminal_input import TerminalInput

        reader = TerminalInput()
        current = None
        print("Kong Local Preview 0.0.0 · /help 查看命令", flush=True)
        while True:
            try:
                line = await reader.get("你 > ")
            except EOFError:
                break
            name, _, argument = line.strip().partition(" ")
            if name == "/quit":
                break
            try:
                if name == "/help":
                    print(
                        "/mode fast|auto|plan · /memory · /remember 文本 · /status · /approve 修订 · /reject · /resume · /fresh 新目标 · /cancel · /add 约束 · /goal 新目标 · /quit"
                    )
                    continue
                if name == "/mode":
                    if argument not in {"fast", "auto", "plan"}:
                        raise ValueError("模式为 fast / auto / plan")
                    args.mode = argument
                    continue
                if name == "/memory":
                    output(await client.memory_records())
                    continue
                if name == "/remember":
                    from kong.application.contracts import MemoryCreate

                    output(
                        await client.memory_create(
                            MemoryCreate(command_id=uuid4().hex, content=argument)
                        )
                    )
                    continue
                if name == "/status":
                    output(
                        await client.run_detail(current)
                        if current
                        else await client.list_threads()
                    )
                    continue
                if current and name in {
                    "/approve",
                    "/reject",
                    "/resume",
                    "/answer",
                    "/discuss",
                    "/resolve",
                }:
                    await client.human(
                        current,
                        HumanCommand(
                            command_id=uuid4().hex,
                            kind=name[1:],
                            text=argument,
                            revision=int(argument) if name == "/approve" else None,
                        ),
                    )
                elif current and name == "/fresh":
                    current = (
                        await client.fresh(
                            current,
                            FreshCommand(
                                command_id=uuid4().hex,
                                text=argument,
                                intent=args.intent,
                                mode=args.mode,
                                acceptance=criteria,
                            ),
                        )
                    )["run_id"]
                else:
                    if name.startswith("/"):
                        raise ValueError("未知命令，使用 /help")
                    if current:
                        previous = await client.run_detail(current)
                        if previous["fresh_required"]:
                            current = (
                                await client.fresh(
                                    current,
                                    FreshCommand(
                                        command_id=uuid4().hex,
                                        text=line,
                                        intent=args.intent,
                                        mode=previous["mode"],
                                        acceptance=criteria,
                                    ),
                                )
                            )["run_id"]
                        elif previous["status"] == "waiting_user":
                            await client.human(
                                current,
                                HumanCommand(
                                    command_id=uuid4().hex,
                                    kind="discuss"
                                    if previous["pending_approval"]
                                    else "answer",
                                    text=line,
                                ),
                            )
                        else:
                            current = (
                                await client.create_run(
                                    thread["thread_id"],
                                    RunCreate(
                                        command_id=uuid4().hex,
                                        text=line,
                                        intent=args.intent,
                                        mode=args.mode,
                                        acceptance=criteria,
                                    ),
                                )
                            )["run_id"]
                    else:
                        current = (
                            await client.create_run(
                                thread["thread_id"],
                                RunCreate(
                                    command_id=uuid4().hex,
                                    text=line,
                                    intent=args.intent,
                                    mode=args.mode,
                                    acceptance=criteria,
                                ),
                            )
                        )["run_id"]
                while (detail := await client.run_detail(current))[
                    "status"
                ] == "running" or detail["driver_active"]:
                    available, steer = reader.poll()
                    if available and steer:
                        cmd, _, value = steer.partition(" ")
                        if cmd in {"/cancel", "/add", "/goal"}:
                            await client.inputs(
                                current,
                                InputCommand(
                                    command_id=uuid4().hex,
                                    kind={
                                        "/cancel": "cancel",
                                        "/add": "constraint",
                                        "/goal": "goal",
                                    }[cmd],
                                    text=value,
                                ),
                            )
                        else:
                            reader.deferred.append(steer)
                    await asyncio.sleep(0.05)
                for message in detail["messages"]:
                    if message["role"] == "assistant":
                        print("Kong > " + message["content"])
                print(detail["outcome_kind"] + " · " + detail["status"], flush=True)
            except (AppError, ValueError) as error:
                print(str(error), flush=True)
    return 0

"""Textual client: no Agent loop, Memory database or execution authority of its own."""

import json
from uuid import uuid4
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Header,
    Footer,
    Static,
    Select,
    TextArea,
    Button,
    RichLog,
    Input,
)
from kong.application.client import LocalClient
from kong.application.contracts import (
    ThreadCreate,
    RunCreate,
    FreshCommand,
    HumanCommand,
    InputCommand,
    MemoryCreate,
    ModelSetup,
)


def command():
    return uuid4().hex


class ApprovalScreen(ModalScreen):
    BINDINGS = [("escape", "dismiss", "返回")]
    CSS = "ApprovalScreen {align:center middle; background:$background 60%;} #review {width:80%;height:80%;border:round $primary;padding:1;} #plan {height:1fr;}"

    def __init__(self, detail):
        super().__init__()
        self.detail = detail

    def compose(self):
        with Vertical(id="review"):
            yield Static(
                "请审阅精确计划修订 #"
                + str(self.detail["pending_approval"]["revision"])
            )
            yield RichLog(id="plan", wrap=True, markup=False)
            with Horizontal():
                yield Button("确认批准此修订", id="approve", variant="primary")
                yield Button("返回", id="back")

    def on_mount(self):
        self.query_one("#plan", RichLog).write(
            json.dumps(self.detail["plan"], ensure_ascii=False, indent=2)
        )

    async def on_button_pressed(self, event):
        if event.button.id == "back":
            self.dismiss()
            return
        try:
            await self.app.client.human(
                self.detail["run_id"],
                HumanCommand(
                    command_id=command(),
                    kind="approve",
                    revision=self.detail["pending_approval"]["revision"],
                ),
            )
            self.dismiss()
            await self.app.refresh_state()
        except Exception:
            self.app.notify("审批已变化或状态不允许，请刷新后重审。", severity="error")


class MemoryScreen(ModalScreen):
    BINDINGS = [("escape", "dismiss", "返回")]
    CSS = "MemoryScreen {align:center middle;background:$background 60%;} #memory-manager {width:85%;height:85%;border:round $primary;padding:1;} #memory-body {height:5;} #memory-meta {height:1fr;}"

    def __init__(self):
        super().__init__()
        self.selected = None
        self.preview = None

    def compose(self):
        with Vertical(id="memory-manager"):
            yield Static("长期记忆 · 由 Core 保存，管理操作不会批准执行计划")
            yield Select([], prompt="选择记忆或直接输入新记忆", id="record")
            yield TextArea(id="memory-body")
            yield RichLog(id="memory-meta", wrap=True, markup=False)
            with Horizontal():
                yield Button("保存新记忆", id="remember", variant="primary")
                yield Button("更正", id="revise")
                yield Button("预览遗忘", id="forget")
                yield Button(
                    "确认预览范围", id="confirm-forget", variant="error", disabled=True
                )
                yield Button("返回", id="back-memory")

    async def on_mount(self):
        await self.load_records()

    async def load_records(self):
        page = await self.app.client.memory_records()
        self.query_one("#record", Select).set_options(
            [
                (
                    "私人记忆"
                    if r["sensitivity"] in {"sensitive", "secret"}
                    else r["current"]["content"][:20],
                    r["record_id"],
                )
                for r in page["items"]
            ]
        )
        self.query_one("#memory-meta", RichLog).write(
            "已载入 " + str(len(page["items"])) + " 条；注入轨迹在主检查面板查看。"
        )

    async def on_select_changed(self, event):
        if event.select.id != "record" or event.value is Select.BLANK:
            return
        self.selected = await self.app.client.memory_record(event.value)
        self.query_one("#memory-body", TextArea).load_text(
            self.selected["current"]["content"]
        )
        self.query_one("#memory-meta", RichLog).write(
            json.dumps(self.selected, ensure_ascii=False, indent=2)
        )
        self.preview = None
        self.query_one("#confirm-forget", Button).disabled = True

    async def on_button_pressed(self, event):
        name = event.button.id
        if name == "back-memory":
            self.dismiss()
            return
        try:
            content = self.query_one("#memory-body", TextArea).text
            if name == "remember":
                await self.app.client.memory_create(
                    MemoryCreate(command_id=command(), content=content)
                )
            elif name == "revise" and self.selected:
                await self.app.client.memory_revise(
                    self.selected["record_id"],
                    content,
                    self.selected["revision"],
                    command(),
                )
            elif name == "forget" and self.selected:
                self.preview = await self.app.client.memory_preview(
                    self.selected["record_id"]
                )
                self.query_one("#memory-meta", RichLog).write(
                    "遗忘范围与保留副本：\n"
                    + json.dumps(self.preview, ensure_ascii=False, indent=2)
                )
                self.query_one("#confirm-forget", Button).disabled = False
                return
            elif name == "confirm-forget" and self.selected and self.preview:
                await self.app.client.memory_forget(
                    self.selected["record_id"],
                    self.preview["expected_revision"],
                    self.preview["digest"],
                    command(),
                )
            else:
                return
            self.selected = None
            self.preview = None
            self.query_one("#confirm-forget", Button).disabled = True
            self.query_one("#memory-body", TextArea).load_text("")
            await self.load_records()
            await self.app.refresh_state()
            self.app.notify("Core 操作已提交。")
        except Exception:
            self.app.notify(
                "记忆操作未完成或版本已变化，请重新读取后确认。", severity="error"
            )


class ModelScreen(ModalScreen):
    BINDINGS = [("escape", "dismiss", "返回")]
    CSS = "ModelScreen {align:center middle;background:$background 60%;} #model-form {width:70%;height:auto;border:round $primary;padding:1;}"

    def compose(self):
        with Vertical(id="model-form"):
            yield Static("模型配置 · 密钥仅由后端本次进程使用")
            yield Input(placeholder="Base URL", id="base-url")
            yield Input(placeholder="Model ID", id="model-id")
            yield Input(
                placeholder="API Key（本地模型可留空）", password=True, id="model-key"
            )
            yield Button("保存配置", id="model-save", variant="primary")
            yield Button("返回", id="model-back")

    async def on_button_pressed(self, event):
        if event.button.id == "model-back":
            self.dismiss()
            return
        try:
            from pydantic import SecretStr

            await self.app.client.configure_model(
                ModelSetup(
                    command_id=command(),
                    base_url=self.query_one("#base-url", Input).value,
                    model=self.query_one("#model-id", Input).value,
                    api_key=SecretStr(self.query_one("#model-key", Input).value),
                )
            )
            self.query_one("#model-key", Input).value = ""
            self.dismiss()
            self.app.notify("模型配置已保存。")
        except Exception:
            self.app.notify("模型配置无效，请检查地址和模型名称。", severity="error")


class KongApp(App):
    TITLE = "Kong Local Preview 0.0.0"
    BINDINGS = [
        ("f1", "help", "帮助"),
        ("ctrl+n", "new_thread", "新对话"),
        ("f4", "send", "发送"),
        ("ctrl+enter", "send", "发送"),
        ("f6", "approval", "审阅计划"),
        ("ctrl+k", "cancel", "停止"),
        ("ctrl+r", "resume", "恢复"),
        ("ctrl+i", "memory", "记忆"),
        ("f8", "model", "模型"),
        ("f7", "resolve", "检查未知动作"),
        ("ctrl+q", "quit", "退出"),
    ]
    CSS = """
    Screen {layout:vertical;} #toolbar {height:3;} #toolbar Select {width:1fr;} #body {height:1fr;}
    #conversation {width:2fr;border:round $primary;padding:0 1;} #inspector {width:1fr;border:round $secondary;padding:0 1;}
    #status {height:2;padding:0 1;} #composer {height:5;border:round $primary;} #acceptance {height:3;}
    #acceptance Input {width:1fr;} .narrow #inspector {display:none;} .short #composer {height:3;}
    .short #status {height:1;} .short #toolbar {height:3;} Footer {height:1;}
    """

    def __init__(self, client):
        super().__init__()
        self.client = client
        self.thread_id = None
        self.current = None
        self.detail = None
        self.refreshing = False
        self.last_conversation = ""

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="toolbar"):
            yield Select([], prompt="对话", id="thread")
            yield Select(
                [(value.upper(), value) for value in ("fast", "auto", "plan")],
                value="auto",
                allow_blank=False,
                id="mode",
            )
            yield Select(
                [("聊天", "chat"), ("完成任务", "task")],
                value="chat",
                allow_blank=False,
                id="intent",
            )
        with Horizontal(id="body"):
            yield RichLog(id="conversation", wrap=True, markup=False)
            yield RichLog(id="inspector", wrap=True, markup=False)
        yield Static("正在连接共享服务…", id="status")
        with Horizontal(id="acceptance"):
            yield Input(placeholder="任务验收文件（聊天可留空）", id="target")
            yield Input(placeholder="用户确认的准确完整文本", id="expected")
        yield TextArea(id="composer")
        yield Footer()

    async def on_mount(self):
        self.set_class(self.size.width < 100, "narrow")
        self.set_class(self.size.height < 24, "short")
        threads = await self.client.list_threads()
        if threads["items"]:
            self.thread_id = threads["items"][-1]["thread_id"]
        else:
            await self.action_new_thread()
        await self.refresh_state()
        self.set_interval(0.4, self.refresh_state)
        self.query_one("#composer", TextArea).focus()

    def on_resize(self, event):
        self.set_class(event.size.width < 100, "narrow")
        self.set_class(event.size.height < 24, "short")

    async def on_select_changed(self, event):
        if (
            event.select.id == "thread"
            and event.value is not Select.BLANK
            and event.value != self.thread_id
        ):
            self.thread_id = event.value
            self.current = None
            await self.refresh_state()

    async def refresh_state(self):
        if (
            self.refreshing
            or not self.thread_id
            or isinstance(self.screen, ModalScreen)
        ):
            return
        self.refreshing = True
        try:
            threads = await self.client.list_threads()
            select = self.query_one("#thread", Select)
            select.set_options(
                [(t["title"][:20], t["thread_id"]) for t in threads["items"]]
            )
            select.value = self.thread_id
            thread = await self.client.thread_detail(self.thread_id)
            self.detail = thread["runs"][-1] if thread["runs"] else None
            self.current = self.detail["run_id"] if self.detail else None
            content = "\n\n".join(
                ("你：" if m["role"] == "user" else "Kong：") + m["content"]
                for m in thread["accepted_messages"]
            )
            if content != self.last_conversation:
                log = self.query_one("#conversation", RichLog)
                log.clear()
                log.write(content or "从一件小事开始。F4 发送，F8 配置模型。")
                self.last_conversation = content
            if self.detail:
                status = (
                    self.detail["outcome_kind"]
                    + " · "
                    + self.detail["status"]
                    + " · "
                    + self.detail["mode"].upper()
                )
                if self.detail["fresh_required"]:
                    status += " · 记忆已变化，需要完整新目标"
                if self.detail["pending_question"]:
                    status += " · " + self.detail["pending_question"]
                if self.detail["unknown_actions"]:
                    status += " · 动作未知，先检查工作区，在输入中说明实际结果，F7 确认"
                self.query_one("#status", Static).update(status)
                trace = await self.client.context_trace(self.current)
                inspector = self.query_one("#inspector", RichLog)
                inspector.clear()
                inspector.write(
                    "真实工具事件\n"
                    + json.dumps(
                        self.detail["observations"], ensure_ascii=False, indent=2
                    )
                )
                inspector.write(
                    "保存 → 检索 → 选中 → 组装 → 进入请求\n"
                    + json.dumps(trace["items"][-6:], ensure_ascii=False, indent=2)
                )
            else:
                self.query_one("#status", Static).update(
                    "准备就绪 · F4 发送 · Ctrl I 管理记忆"
                )
        except Exception:
            self.query_one("#status", Static).update(
                "读取失败，连接可能中断；任务不会重新提交。"
            )
        finally:
            self.refreshing = False

    async def action_new_thread(self):
        thread = await self.client.create_thread(
            ThreadCreate(command_id=command(), title="终端对话")
        )
        self.thread_id = thread["thread_id"]
        self.current = None
        self.last_conversation = ""
        await self.refresh_state()

    async def action_send(self):
        text = self.query_one("#composer", TextArea).text
        if not text.strip():
            return
        try:
            mode = self.query_one("#mode", Select).value
            intent = self.query_one("#intent", Select).value
            path = self.query_one("#target", Input).value
            criteria = (
                [
                    {
                        "id": "delivery",
                        "description": "用户确认的准确文本",
                        "kind": "file_equals",
                        "path": path,
                        "expected_text": self.query_one("#expected", Input).value,
                    }
                ]
                if intent == "task" and path
                else []
            )
            body = RunCreate(
                command_id=command(),
                text=text,
                intent=intent,
                mode=mode,
                acceptance=criteria,
            )
            if self.detail and self.detail["fresh_required"]:
                await self.client.fresh(self.current, FreshCommand(**body.model_dump()))
            elif self.detail and self.detail["status"] == "running":
                await self.client.inputs(
                    self.current,
                    InputCommand(
                        command_id=body.command_id, kind="constraint", text=text
                    ),
                )
            elif self.detail and self.detail["status"] == "waiting_user":
                await self.client.human(
                    self.current,
                    HumanCommand(
                        command_id=body.command_id,
                        kind="discuss" if self.detail["pending_approval"] else "answer",
                        text=text,
                    ),
                )
            else:
                await self.client.create_run(self.thread_id, body)
            self.query_one("#composer", TextArea).load_text("")
            await self.refresh_state()
        except Exception as error:
            self.notify(
                str(error)
                if isinstance(error, ValueError)
                else "发送未完成，请先刷新状态。",
                severity="error",
            )

    def action_approval(self):
        if self.detail and self.detail["pending_approval"]:
            self.push_screen(ApprovalScreen(self.detail))
        else:
            self.notify("当前没有待批准计划。")

    async def action_cancel(self):
        if not self.detail:
            return
        try:
            if self.detail["status"] == "running":
                await self.client.inputs(
                    self.current, InputCommand(command_id=command(), kind="cancel")
                )
            elif self.detail["status"] == "waiting_user":
                await self.client.human(
                    self.current, HumanCommand(command_id=command(), kind="reject")
                )
            await self.refresh_state()
        except Exception:
            self.notify("状态已变化，请刷新后重试。", severity="error")

    async def action_resume(self):
        if self.detail and self.detail["can_resume"]:
            await self.client.human(
                self.current, HumanCommand(command_id=command(), kind="resume")
            )
            await self.refresh_state()
        else:
            self.notify(
                "当前任务不能直接恢复；等待任务用输入回应，记忆变化用完整新目标。"
            )

    async def action_resolve(self):
        text = self.query_one("#composer", TextArea).text
        if not self.detail or not self.detail["unknown_actions"] or not text.strip():
            self.notify("先检查工作区，在输入中说明动作实际结果。")
            return
        try:
            await self.client.human(
                self.current,
                HumanCommand(command_id=command(), kind="resolve", text=text),
            )
            self.query_one("#composer", TextArea).load_text("")
            await self.refresh_state()
        except Exception:
            self.notify("未解决，请重新读取状态。", severity="error")

    def action_memory(self):
        self.push_screen(MemoryScreen())

    def action_model(self):
        self.push_screen(ModelScreen())

    def action_help(self):
        self.notify(
            "F4/Ctrl Enter 发送；F6 审阅精确计划；Ctrl K 停止；Ctrl R 恢复；Ctrl I 记忆；F8 模型；Ctrl N 新对话。",
            timeout=12,
        )


async def run_tui(root, *, connect=None, model_config=None):
    async with LocalClient(root, connect=connect, model_config=model_config) as client:
        await KongApp(client).run_async()

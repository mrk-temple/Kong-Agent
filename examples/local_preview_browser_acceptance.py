"""Windows browser acceptance of the built product UI, with explicit offline fixture backend."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import httpx
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://127.0.0.1:8765"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    data = output / "data"
    temporary = output / "tmp"
    temporary.mkdir()
    log = (output / "server.txt").open("w", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        TEMP=str(temporary),
        TMP=str(temporary),
        TMPDIR=str(temporary),
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
    )

    def start():
        process = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "examples/local_preview_fixture_server.py"),
                "--data-root",
                str(data),
                "--port",
                "8765",
            ],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        for _ in range(100):
            try:
                if httpx.get(ORIGIN + "/v1/health").status_code == 200:
                    return process
            except httpx.HTTPError:
                pass
            if process.poll() is not None:
                raise RuntimeError("fixture server failed")
            time.sleep(0.1)
        raise RuntimeError("fixture startup timed out")

    process = start()
    results = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                headless=True,
                args=["--disable-gpu"],
                env=env,
            )
            context = browser.new_context(
                viewport={"width": 1366, "height": 768}, reduced_motion="reduce"
            )
            page = context.new_page()
            console = []
            page.on("pageerror", lambda e: console.append(str(e)))
            page.goto(ORIGIN + "/#pair=offline-browser-pairing")
            expect(page.get_by_text("已连接", exact=True)).to_be_visible(timeout=15000)

            def get(path):
                response = page.request.get(ORIGIN + "/v1" + path)
                assert response.ok, (path, response.status)
                return response.json()

            def active():
                threads = get("/threads")["items"]
                return (
                    get("/threads/" + threads[-1]["thread_id"])["runs"][-1]
                    if threads
                    else None
                )

            def wait_run(status="completed"):
                for _ in range(240):
                    detail = active()
                    if (
                        detail
                        and detail["status"] == status
                        and not detail["driver_active"]
                    ):
                        return detail
                    page.wait_for_timeout(100)
                raise AssertionError("run did not reach " + status)

            def nav(name):
                page.get_by_role("navigation", name="主要空间").get_by_role(
                    "button", name=name, exact=False
                ).click()

            def new():
                page.locator(".new-chat").click()
                page.wait_for_timeout(350)

            def send(text):
                page.get_by_label("对话输入").fill(text)
                page.get_by_role("button", name="发送消息", exact=True).click()

            assert get("/memory/records")["items"] == []
            nav("记忆")
            expect(page.get_by_text("还没有保存的记忆")).to_be_visible()
            page.screenshot(path=output / "01-empty-1366.png")
            page.get_by_role("button", name="模型与设置").click()
            page.get_by_label("Base URL").fill("http://fixture.invalid/v1")
            page.get_by_label("Model ID").fill("offline-browser-fixture")
            page.get_by_label("API Key", exact=False).fill("offline-fixture-key")
            page.get_by_role("button", name="保存配置", exact=True).click()
            expect(
                page.get_by_text("配置已保存。密钥仅由后端本次进程使用。")
            ).to_be_visible()
            page.keyboard.press("Escape")
            results.append(
                {"case": "first-launch-config-empty-bottle", "status": "pass"}
            )
            nav("对话")
            new()
            page.get_by_label("下一轮模式").select_option("fast")
            box = page.get_by_label("对话输入")
            box.fill("中文输入测试")
            box.evaluate(
                "node => node.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',isComposing:true,bubbles:true}))"
            )
            page.wait_for_timeout(300)
            assert not get("/threads/" + get("/threads")["items"][-1]["thread_id"])[
                "runs"
            ]
            box.press("Shift+Enter")
            assert "\n" in box.input_value()
            box.fill("")
            results.append(
                {
                    "case": "composition-enter-and-multiline",
                    "status": "pass",
                    "boundary": "synthetic browser composition; physical IME remains manual",
                }
            )
            send("我的稳定偏好：使用简短中文回答，预览验收标签 BLUE47。")
            wait_run()
            expect(page.locator(".run-strip")).to_contain_text("答复结束", timeout=5000)
            page.get_by_role("button", name="保存为记忆").first.click()
            expect(page.get_by_label("记忆内容")).to_have_value(
                "我的稳定偏好：使用简短中文回答，预览验收标签 BLUE47。"
            )
            page.get_by_role("button", name="确认保存", exact=True).click()
            nav("记忆")
            expect(
                page.get_by_role("img", name="记忆漂流瓶，已载入 1 条记忆")
            ).to_be_visible(timeout=8000)
            record = get("/memory/records")["items"][0]
            page.screenshot(path=output / "02-memory-1366.png")
            results.append(
                {
                    "case": "chat-save-real-core-fragment",
                    "status": "pass",
                    "record_id": record["record_id"],
                }
            )
            new()
            send("我的稳定偏好和预览验收标签是什么？")
            detail = wait_run()
            assert "BLUE47" in detail["messages"][-1]["content"]
            trace = get("/runs/" + detail["run_id"] + "/context-trace")["items"]
            assert any(
                e["kind"] == "memory.request_delivered" and e["data"]["selected"]
                for e in trace
            )
            page.screenshot(path=output / "03-chat-receipt-1366.png")
            results.append(
                {
                    "case": "new-thread-recall-real-adapter-receipt",
                    "status": "pass",
                    "run_id": detail["run_id"],
                }
            )
            nav("记忆")
            page.get_by_role("button", name="查看记忆：", exact=False).first.focus()
            page.keyboard.press("Enter")
            expect(page.get_by_role("heading", name="记忆详情")).to_be_visible()
            page.get_by_role("button", name="更正", exact=True).click()
            page.get_by_label("记忆内容").fill(
                "我的稳定偏好：使用简短中文回答，预览验收标签 GREEN82。"
            )
            page.get_by_role("button", name="确认更正", exact=True).click()
            new()
            send("我的稳定偏好和预览验收标签是什么？")
            corrected = wait_run()
            trace = get("/runs/" + corrected["run_id"] + "/context-trace")["items"]
            assert "GREEN82" in corrected["messages"][-1]["content"]
            assert all(
                item["version"] == 2
                for e in trace
                for item in e["data"].get("selected", [])
            )
            nav("记忆")
            page.get_by_role("button", name="查看记忆：", exact=False).first.click()
            page.get_by_role("button", name="遗忘", exact=True).click()
            expect(page.get_by_text("以下副本保留")).to_be_visible()
            page.get_by_role("button", name="确认这个范围并遗忘").click()
            expect(page.get_by_text("还没有保存的记忆")).to_be_visible(timeout=8000)
            new()
            send("我的稳定偏好和预览验收标签是什么？")
            deleted = wait_run()
            assert not any(
                e["data"].get("selected")
                for e in get("/runs/" + deleted["run_id"] + "/context-trace")["items"]
            )
            results.append(
                {"case": "revision-and-forget-new-packets", "status": "pass"}
            )
            new()
            page.get_by_label("下一轮意图").select_option("task")
            page.get_by_label("下一轮模式").select_option("plan")
            page.get_by_label("验收文件", exact=True).fill("browser-plan.txt")
            page.get_by_label("准确内容", exact=True).fill("BROWSER VERIFIED")
            send("请按计划创建 browser-plan.txt，准确内容为 BROWSER VERIFIED。")
            wait_run("waiting_user")
            assert not (data / "workspace/browser-plan.txt").exists()
            expect(page.get_by_role("button", name="审阅并确认")).to_be_visible(
                timeout=8000
            )
            page.get_by_role("button", name="审阅并确认").click()
            page.get_by_role("button", name="确认批准此修订").click()
            done = wait_run()
            assert done["outcome_kind"] == "task_accepted"
            assert (
                data / "workspace/browser-plan.txt"
            ).read_text() == "BROWSER VERIFIED"
            nav("工作区")
            page.get_by_role("button", name="browser-plan.txt", exact=False).click()
            expect(page.get_by_text("BROWSER VERIFIED", exact=True)).to_be_visible()
            page.screenshot(path=output / "04-workspace-1366.png")
            results.append(
                {
                    "case": "plan-exact-approval-real-file-gate",
                    "status": "pass",
                    "run_id": done["run_id"],
                }
            )
            new()
            page.get_by_label("下一轮意图").select_option("chat")
            page.get_by_label("下一轮模式").select_option("fast")
            send("CANCEL_FIXTURE 等待取消")
            page.wait_for_timeout(500)
            page.get_by_role("button", name="停止任务", exact=True).click()
            stopped = wait_run("stopped")
            assert stopped["can_resume"]
            expect(page.get_by_role("button", name="恢复", exact=True)).to_be_visible(
                timeout=8000
            )
            page.get_by_role("button", name="恢复", exact=True).click()
            wait_run()
            results.append({"case": "cancel-resume", "status": "pass"})
            count = len(get("/threads")["items"])
            context.set_offline(True)
            page.wait_for_timeout(1000)
            context.set_offline(False)
            expect(page.get_by_text("已连接", exact=True)).to_be_visible(timeout=15000)
            page.reload()
            expect(page.get_by_text("已连接", exact=True)).to_be_visible(timeout=10000)
            assert len(get("/threads")["items"]) == count
            assert page.locator(".message.assistant").count() == 1
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.set_viewport_size({"width": 1920, "height": 1080})
            page.reload()
            expect(page.get_by_text("已连接", exact=True)).to_be_visible(timeout=10000)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=output / "05-chat-1920.png")
            results.append(
                {"case": "disconnect-reconnect-refresh-two-widths", "status": "pass"}
            )
            process.terminate()
            process.wait(timeout=15)
            process = start()
            page.goto(ORIGIN + location_path(page) + "#pair=offline-browser-pairing")
            expect(page.get_by_text("已连接", exact=True)).to_be_visible(timeout=15000)
            assert len(get("/threads")["items"]) == count
            assert active()["outcome_kind"] == "answer_finished"
            assert (
                data / "workspace/browser-plan.txt"
            ).read_text() == "BROWSER VERIFIED"
            results.append(
                {"case": "process-restart-snapshot-workspace", "status": "pass"}
            )
            assert not console, console
            report = {
                "type": "offline-provider-browser-E2E",
                "results": results,
                "page_errors": console,
                "viewport": [1366, 768, 1920, 1080],
                "motion": "reduced",
                "provider": "explicit fixture transport; real Core Runtime HTTP receipt, no live semantics",
            }
            (output / "results.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(json.dumps(report, ensure_ascii=False))
            context.close()
            browser.close()
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=15)
        log.close()


def location_path(page):
    return page.evaluate("location.pathname")


if __name__ == "__main__":
    main()

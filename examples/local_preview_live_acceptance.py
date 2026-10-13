"""Real MiMo GUI acceptance. Each launch requires a fresh output directory and authorization."""

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
ORIGIN = "http://127.0.0.1:8766"
PREFERENCE = "我的稳定表达偏好：使用简体中文，以三条简短要点回答，不使用颜文字；记忆测试标签为 BLUE47。"
CORRECTED = "我的稳定表达偏好：使用简体中文，以两条简短要点回答，不使用颜文字；记忆测试标签为 GREEN82。"
QUESTION = "请按我的稳定表达偏好回答，并告诉我偏好的语言、要点数量和记忆测试标签。没有可靠记录时请如实说明。"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--api-file", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    log = (output / "server.txt").open("w", encoding="utf-8")
    results = []
    process = None
    env = os.environ.copy()
    env.update(
        TEMP="E:/Projects/.workspace/tmp",
        TMP="E:/Projects/.workspace/tmp",
        TMPDIR="E:/Projects/.workspace/tmp",
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
    )

    def persist():
        ledger = (
            json.loads((output / "ledger.json").read_text(encoding="utf-8"))
            if (output / "ledger.json").exists()
            else {}
        )
        report = {
            "type": "real-MiMo-built-GUI",
            "source_identity": ledger.get("source_identity"),
            "cases": results,
            "requests": len(ledger.get("requests", [])),
            "usage": {
                k: sum(r.get("usage", {}).get(k, 0) for r in ledger.get("requests", []))
                for k in ["prompt_tokens", "completion_tokens", "total_tokens"]
            },
            "semantic_review": "pending human review of full responses; keywords are proxies only",
        }
        (output / "results.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def start(resume=False):
        command = [
            sys.executable,
            str(ROOT / "examples/local_preview_live_server.py"),
            "--output",
            str(output),
            "--api-file",
            str(args.api_file),
            "--port",
            "8766",
        ]
        if resume:
            command.append("--resume-ledger")
        owned = subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        for _ in range(150):
            try:
                if httpx.get(ORIGIN + "/v1/health").status_code == 200:
                    return owned
            except httpx.HTTPError:
                pass
            if owned.poll() is not None:
                raise RuntimeError("Live server did not start; inspect safe log")
            time.sleep(0.1)
        owned.terminate()
        owned.wait(timeout=15)
        raise RuntimeError("Live startup timed out")

    try:
        process = start()
        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                headless=True,
                args=["--disable-gpu"],
                env=env,
            )
            context = browser.new_context(
                viewport={"width": 1920, "height": 1080}, reduced_motion="reduce"
            )
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(ORIGIN + "/#pair=live-evaluator-pairing")
            expect(page.get_by_text("已连接", exact=True)).to_be_visible(timeout=15000)

            def get(path):
                response = page.request.get(ORIGIN + "/v1" + path)
                assert response.ok, (path, response.status)
                return response.json()

            def active():
                items = get("/threads")["items"]
                if not items:
                    return None
                runs = get("/threads/" + items[-1]["thread_id"])["runs"]
                return runs[-1] if runs else None

            def wait():
                for _ in range(2000):
                    detail = active()
                    if (
                        detail
                        and detail["status"] != "running"
                        and not detail["driver_active"]
                    ):
                        return detail
                    page.wait_for_timeout(100)
                raise RuntimeError("Run timeout; no automatic replay")

            def new(mode="fast", intent="chat"):
                previous = get("/threads")["items"]
                previous_id = previous[-1]["thread_id"] if previous else None
                page.locator(".new-chat").click()
                page.wait_for_timeout(350)
                latest = get("/threads")["items"][-1]["thread_id"]
                assert latest != previous_id, (
                    "Thread submission was rejected; no case reuse"
                )
                assert not get("/threads/" + latest)["runs"]
                page.get_by_label("下一轮模式").select_option(mode)
                page.get_by_label("下一轮意图").select_option(intent)

            def send(text):
                page.get_by_label("对话输入").fill(text)
                page.get_by_role("button", name="发送消息", exact=True).click()

            def nav(name):
                page.get_by_role("navigation", name="主要空间").get_by_role(
                    "button", name=name, exact=False
                ).click()

            def capture(case, detail, extra=None):
                trace = get("/runs/" + detail["run_id"] + "/context-trace")["items"]
                result = {
                    "case": case,
                    "status": detail["status"],
                    "outcome": detail["outcome_kind"],
                    "run_id": detail["run_id"],
                    "detail": detail,
                    "trace": trace,
                    **(extra or {}),
                }
                results.append(result)
                persist()
                print(
                    json.dumps(
                        {
                            "case": case,
                            "status": detail["status"],
                            "model_calls": detail["metrics"]["model_calls"],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                return result

            # GUI connection test goes through the bounded CompatibleModel instrumentation.
            page.get_by_role("button", name="模型与设置").click()
            page.get_by_role("button", name="检查连接", exact=True).click()
            expect(page.get_by_role("status").filter(has_text="连接")).to_be_visible(
                timeout=65000
            )
            connected = page.get_by_text("连接正常", exact=True).count() > 0
            results.append(
                {
                    "case": "P2-L00-connection",
                    "status": "connected" if connected else "failed",
                }
            )
            persist()
            if not connected:
                raise RuntimeError("Provider connection failed; no automatic retry")
            page.keyboard.press("Escape")
            new()
            send(PREFERENCE)
            first = wait()
            capture("P2-L01-preference-statement", first)
            if first["status"] != "completed":
                raise RuntimeError("Initial chat not accepted")
            page.locator(".message.user .message-save").first.click()
            expect(page.get_by_label("记忆内容")).to_have_value(PREFERENCE)
            page.get_by_role("button", name="确认保存", exact=True).click()
            nav("记忆")
            expect(
                page.get_by_role("img", name="记忆漂流瓶，已载入 1 条记忆").first
            ).to_be_visible(timeout=10000)
            record = get("/memory/records")["items"][0]
            page.screenshot(path=output / "01-real-memory.png")
            results.append(
                {
                    "case": "P2-L02-real-Core-GUI-save",
                    "status": "committed",
                    "record_id": record["record_id"],
                    "revision": record["revision"],
                }
            )
            persist()
            new()
            send(QUESTION)
            fast = wait()
            capture("P2-L03-FAST-new-thread-recall", fast)
            page.screenshot(path=output / "02-real-recall.png")
            nav("记忆")
            page.get_by_role("button", name="查看记忆：", exact=False).first.click()
            page.get_by_role("button", name="更正", exact=True).click()
            page.get_by_label("记忆内容").fill(CORRECTED)
            page.get_by_role("button", name="确认更正", exact=True).click()
            new()
            send(QUESTION)
            revised = wait()
            capture("P2-L05-corrected-current-version", revised)
            current_path = page.evaluate("location.pathname")
            process.terminate()
            process.wait(timeout=15)
            process = start(resume=True)
            page.goto(ORIGIN + current_path + "#pair=live-evaluator-pairing")
            page.reload()
            expect(page.get_by_text("已连接", exact=True)).to_be_visible(timeout=15000)
            new()
            send(QUESTION)
            restart = wait()
            capture("P2-L06-process-restart-current-memory", restart)
            nav("记忆")
            page.get_by_role("button", name="查看记忆：", exact=False).first.click()
            page.get_by_role("button", name="遗忘", exact=True).click()
            page.get_by_role("button", name="确认这个范围并遗忘").click()
            expect(page.get_by_text("还没有保存的记忆")).to_be_visible(timeout=10000)
            new()
            send(QUESTION)
            forgot = wait()
            capture("P2-L07-forgotten-source-absent", forgot)
            new("auto", "task")
            page.get_by_label("验收文件", exact=True).fill("live-auto.txt")
            page.get_by_label("准确内容", exact=True).fill("本地实测完成")
            send("请创建 live-auto.txt，准确完整内容为 本地实测完成，不要增加换行。")
            task = wait()
            capture("P2-L08-AUTO-real-file", task)
            new("plan", "task")
            page.get_by_label("验收文件", exact=True).fill("live-plan.txt")
            page.get_by_label("准确内容", exact=True).fill("计划验收完成")
            send(
                "先提出计划，等待我的批准后创建 live-plan.txt。准确完整内容为 计划验收完成，不增加换行。"
            )
            plan = wait()
            capture("P2-L09-PLAN-before-approval", plan)
            if plan["pending_approval"]:
                assert not (output / "data/workspace/live-plan.txt").exists()
                expect(page.get_by_role("button", name="审阅并确认")).to_be_visible(
                    timeout=10000
                )
                page.get_by_role("button", name="审阅并确认").click()
                with page.expect_response(
                    lambda response: (
                        response.request.method == "POST"
                        and response.url.endswith("/human")
                    )
                ) as command:
                    page.get_by_role("button", name="确认批准此修订").click()
                assert command.value.status == 202, "Approval was rejected; no replay"
                approved = wait()
                capture("P2-L10-PLAN-after-exact-approval", approved)
            new("auto", "task")
            page.get_by_label("验收文件", exact=True).fill("live-unwritten.txt")
            page.get_by_label("准确内容", exact=True).fill("依赖文件读取后交付")
            send(
                "先读取不存在的 missing-live-input.txt；只有确实读取到文件后才创建 live-unwritten.txt。若文件读取失败，请询问我提供文件或结束，不要重复读取或宣称完成。"
            )
            failed = wait()
            capture("P2-L11-read-failure-recovery", failed)
            if failed["status"] == "waiting_user" and not failed["pending_approval"]:
                expect(page.get_by_role("button", name="结束任务")).to_be_visible(
                    timeout=10000
                )
                with page.expect_response(
                    lambda response: (
                        response.request.method == "POST"
                        and response.url.endswith("/human")
                    )
                ) as command:
                    page.get_by_role("button", name="结束任务").click()
                assert command.value.status == 202, "Stop was rejected; no replay"
                stopped = wait()
                capture("P2-L12-user-stops-failed-read", stopped)
            new("auto", "task")
            page.get_by_label("验收文件", exact=True).fill("")
            page.get_by_label("准确内容", exact=True).fill("")
            before = len(json.loads((output / "ledger.json").read_text())["requests"])
            send("请交付一个任务，但我还没有提供明确验收条件。")
            expect(page.get_by_role("alert")).to_contain_text(
                "强验收条件", timeout=10000
            )
            after = len(json.loads((output / "ledger.json").read_text())["requests"])
            assert before == after
            results.append(
                {
                    "case": "P2-L13-task-missing-contract",
                    "status": "rejected-before-dispatch",
                    "requests": 0,
                }
            )
            persist()
            nav("工作区")
            page.screenshot(path=output / "03-real-workspace.png")
            assert not errors, errors
            context.close()
            browser.close()
    except Exception as error:
        results.append(
            {
                "case": "harness-interruption",
                "status": "inconclusive",
                "category": type(error).__name__,
                "message": str(error)[:180],
            }
        )
        persist()
        raise
    finally:
        if process and process.poll() is None:
            process.terminate()
            process.wait(timeout=15)
        log.close()
        persist()
    print((output / "results.json").read_text(encoding="utf-8"), flush=True)


if __name__ == "__main__":
    main()

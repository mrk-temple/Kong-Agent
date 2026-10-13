"""Explicit offline browser fixture; it uses the real service/Core/Runtime, a fake provider transport.
Never selected by a product entry point. Paid mode is a separate, bounded evaluator.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import uvicorn
import httpx
from kong.application.local_cli import prepare_environment
from kong.local_api.app import create_app
from kong.models.compatible import CompatibleModel
from kong.config import ModelConfig
from kong.contracts import (
    PlanProposal,
    Plan,
    PlanStep,
    Criterion,
    Act,
    Action,
    CompletePlanStep,
    FinalResponse,
    NeedUserInput,
)


class FixtureModel(CompatibleModel):
    def __init__(self):
        self.count = 0
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.reply))
        super().__init__(
            ModelConfig(
                base_url="http://fixture.invalid/v1",
                model="offline-browser-fixture",
                stream=False,
            ),
            client,
        )

    async def reply(self, request):
        self.count += 1
        packet = json.loads(request.content)
        text = json.dumps(packet["messages"], ensure_ascii=False)
        if "browser-plan.txt" in text:
            criterion = Criterion(
                id="delivery",
                description="caller exact text",
                kind="file_equals",
                path="browser-plan.txt",
                expected_text="BROWSER VERIFIED",
            )
            decisions = [
                PlanProposal(
                    plan=Plan(
                        goal="write browser-plan.txt",
                        rationale="bounded local fixture",
                        steps=[PlanStep(id="write", title="写入确认文本")],
                        success_criteria=[criterion],
                    )
                ),
                Act(
                    actions=[
                        Action(
                            name="write_file",
                            arguments={
                                "path": "browser-plan.txt",
                                "content": "BROWSER VERIFIED",
                            },
                        )
                    ]
                ),
                CompletePlanStep(step_id="write", evidence_ids=["a1"]),
                FinalResponse(content="已完成实际文件验收。", evidence_ids=["a1"]),
            ]
            decision = decisions[min(self.count - 1, 3)]
        elif "CANCEL_FIXTURE" in text:
            if self.count == 1:
                await asyncio.sleep(90)
            decision = FinalResponse(content="已恢复，中文回答。")
        elif "missing-browser.txt" in text:
            decision = (
                Act(
                    actions=[
                        Action(
                            name="read_file", arguments={"path": "missing-browser.txt"}
                        )
                    ]
                )
                if self.count == 1
                else NeedUserInput(question="文件不存在，请提供文件或结束任务。")
            )
        else:
            memory = (
                "GREEN82" if "GREEN82" in text else "BLUE47" if "BLUE47" in text else ""
            )
            decision = FinalResponse(
                content="中文回答："
                + (
                    f"你的稳定偏好是简短中文，验收标签 {memory}。"
                    if memory
                    else "已收到。我会先确认信息，再回答。"
                )
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": decision.model_dump_json()},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 40,
                    "completion_tokens": 20,
                    "total_tokens": 60,
                },
            },
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    root = prepare_environment(args.data_root)
    os.environ["PYTHONUTF8"] = "1"
    app = create_app(
        root,
        origin=f"http://127.0.0.1:{args.port}",
        token="offline-browser-pairing",
        model_factory=FixtureModel,
    )
    uvicorn.run(
        app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning"
    )


if __name__ == "__main__":
    main()

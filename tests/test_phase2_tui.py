import asyncio
import pytest
from textual.widgets import TextArea
from kong.application.client import LocalClient
from kong.models.fake import ScriptedModel
from kong.contracts import FinalResponse
from kong.tui.app import KongApp, MemoryScreen


@pytest.mark.parametrize("width", [80, 100, 140])
def test_tui_pilot_real_service_and_memory(tmp_path, width):
    async def check():
        async with LocalClient(
            tmp_path,
            model_factory=lambda: ScriptedModel(
                [FinalResponse(content="终端接受回答")]
            ),
        ) as client:
            app = KongApp(client)
            async with app.run_test(size=(width, 24)) as pilot:
                await pilot.pause()
                app.query_one("#composer", TextArea).load_text("你好")
                await pilot.press("f4")
                for _ in range(20):
                    await pilot.pause(0.1)
                    if app.detail and app.detail["status"] == "completed":
                        break
                assert app.detail["outcome_kind"] == "answer_finished"
                assert client.service.runtimes[app.current].metrics.model_calls == 1
                await pilot.press("ctrl+i")
                await pilot.pause()
                assert isinstance(app.screen, MemoryScreen)
                app.screen.query_one("#memory-body", TextArea).load_text(
                    "稳定偏好：简短中文"
                )
                await pilot.click("#remember")
                await pilot.pause(0.2)
                assert len((await client.memory_records())["items"]) == 1
                await pilot.press("escape")
                await pilot.pause()

    asyncio.run(check())

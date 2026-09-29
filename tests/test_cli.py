import json
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def cli(workspace, *args, input=None):
    return subprocess.run([sys.executable, "-m", "kong", "--workspace", str(workspace), *args],
                          input=input, capture_output=True, encoding="utf-8", timeout=20)


def test_local_execution_from_cli_and_http_protocol(tmp_path):
    requests = []
    decisions = [
        {"kind": "act", "actions": [{"name": "process_exec", "arguments": {
            "argv": ["python", "-c", "from pathlib import Path; Path('result.txt').write_text('verified',encoding='utf-8')"]}}]},
        {"kind": "act", "actions": [{"name": "read_file", "arguments": {"path": "result.txt"}}]},
        {"kind": "respond", "content": "verified artifact", "evidence_ids": ["a2"]},
    ]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            decision = decisions[min(len(requests) - 1, len(decisions) - 1)]
            response = json.dumps({"choices": [{"message": {"content": json.dumps(decision)}, "finish_reason": "stop"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = tmp_path / "model.toml"
        config.write_text(f'default_profile="local"\n[profiles.local]\nbase_url="http://127.0.0.1:{server.server_port}/v1"\nmodel="test"\napi_key_env="KONG_TEST_UNUSED"', encoding="utf-8")
        result = cli(tmp_path, "--config", str(config), "--global-memory-db", str(tmp_path / "global.db"),
                     "--allow-process", "run", "create and verify result", "--no-interactive")
        assert result.returncode == 0, result.stdout + result.stderr
        assert (tmp_path / "result.txt").read_text() == "verified"
        assert len(requests) == 3
        assert '"local_process"' in json.dumps(requests[0], ensure_ascii=False).replace('\\"', '"')
        saved = json.loads(next((tmp_path / ".kong" / "runs").glob("*.json")).read_text(encoding="utf-8"))
        assert saved["history"]["observations"][0]["output"]["exit_code"] == 0
        assert saved["state"]["status"] == "completed"
        assert "allow_process" not in saved  # Startup authorization is not restored from model/history.
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_offline_demo_completes_from_real_cli(tmp_path):
    result = cli(tmp_path, "demo", "--auto-approve")
    assert result.returncode == 0, result.stderr
    assert "completed" in result.stdout
    assert "确定性验收条件通过" in result.stdout
    snapshots = list((tmp_path / ".kong" / "runs").glob("*.json"))
    data = json.loads(snapshots[0].read_text(encoding="utf-8"))
    assert Path(data["workspace"], "kong-demo.txt").is_file()
    assert data["state"]["turn_count"] == 5


def test_demo_pause_exit_restart_and_approve(tmp_path):
    first = cli(tmp_path, "demo", input="/quit\n")
    assert first.returncode == 0, first.stderr
    path = next((tmp_path / ".kong" / "runs").glob("*.json"))
    before = json.loads(path.read_text(encoding="utf-8"))
    assert before["state"]["status"] == "waiting_user"
    assert not Path(before["workspace"], "kong-demo.txt").exists()
    second = cli(tmp_path, "resume", path.stem, input="/approve 1\n")
    assert second.returncode == 0, second.stderr
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["state"]["status"] == "completed"
    assert after["state"]["turn_count"] == 5


def test_interrupted_action_demo_script_all_checks(tmp_path):
    script = Path(__file__).resolve().parents[1] / "examples" / "interrupted_action_demo.py"
    result = subprocess.run([sys.executable, str(script), "--workspace", str(tmp_path / "ws")],
                            capture_output=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["all_passed"] is True
    assert evidence["checks"]["zero_replay"] is True
    assert not (tmp_path / "ws" / "canary.txt").exists()


def test_cli_memory_management_without_provider_calls(tmp_path):
    import asyncio
    from kong.contracts import FinalResponse
    from kong.continuity.memory_store import MemoryStore
    from kong.models.fake import ScriptedModel
    from kong.runtime.loop import Runtime
    from kong.storage import RunStore
    from kong.tools.defaults import default_registry
    from kong.workspace import Workspace
    workspace = Workspace(tmp_path)
    runtime = Runtime.new("hello", ScriptedModel([FinalResponse(content="ok")]), workspace,
                          default_registry(workspace), store=RunStore(tmp_path / ".kong" / "runs"))
    asyncio.run(runtime.run())
    before = runtime.state.model_dump()
    prefix = ("--global-memory-db", str(tmp_path / "global.db"), "memory", runtime.state.run_id)
    proposed = cli(tmp_path, *prefix, "add", "--kind", "decision", "--key", "database", "--scope", "workspace", "--text", "Use SQLite")
    assert proposed.returncode == 0, proposed.stderr
    memory_store = MemoryStore(tmp_path / ".kong" / "context.db")
    entry = memory_store.state(runtime.thread.thread_id).items[0]
    assert entry.status == "candidate"
    confirmed = cli(tmp_path, *prefix, "confirm", entry.id)
    assert confirmed.returncode == 0, confirmed.stderr
    assert memory_store.state(runtime.thread.thread_id).items[0].status == "active"
    forgotten = cli(tmp_path, *prefix, "forget", entry.id)
    assert forgotten.returncode == 0, forgotten.stderr
    rebuilt = cli(tmp_path, *prefix, "rebuild")
    assert rebuilt.returncode == 0, rebuilt.stderr
    assert memory_store.state(runtime.thread.thread_id).items[0].status == "rejected"
    assert runtime.store.load(runtime.state.run_id).state.model_dump() == before


def test_cli_real_http_to_local_fake_provider(tmp_path):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            body = json.dumps({"choices": [{"message": {"content": '{"kind":"respond","content":"你好，Kong！"}'},
                                           "finish_reason": "stop"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = tmp_path / "model.toml"
        config.write_text(f'default_profile="local"\n[profiles.local]\nbase_url="http://127.0.0.1:{server.server_port}/v1"\nmodel="test"\napi_key_env="KONG_TEST_UNUSED"', encoding="utf-8")
        result = cli(tmp_path, "--config", str(config), "run", "你好", "--no-interactive")
        assert result.returncode == 0, result.stderr
        assert "你好，Kong！" in result.stdout
        assert len(requests) == 1
        assert requests[0][0] == "/v1/chat/completions"
        first_path = next((tmp_path / ".kong" / "runs").glob("*.json"))
        first = json.loads(first_path.read_text(encoding="utf-8"))
        second = cli(tmp_path, "--config", str(config), "run", "第二个目标", "--thread",
                     first["thread_id"], "--no-interactive")
        assert second.returncode == 0, second.stderr
        listing = cli(tmp_path, "threads")
        assert listing.returncode == 0, listing.stderr
        assert first["thread_id"] in listing.stdout
        snapshots = [json.loads(p.read_text(encoding="utf-8"))
                     for p in (tmp_path / ".kong" / "runs").glob("*.json")]
        assert len(snapshots) == 2
        assert {s["thread_id"] for s in snapshots} == {first["thread_id"]}
        assert all(s["state"]["turn_count"] == 1 for s in snapshots)
        assert len(requests) == 2
        interactive = cli(tmp_path, "--config", str(config), input="你好我是c\n我是谁\n/context\n/memory\n/quit\n")
        assert interactive.returncode == 0, interactive.stderr
        snapshots = [json.loads(p.read_text(encoding="utf-8"))
                     for p in (tmp_path / ".kong" / "runs").glob("*.json")]
        conversation = [s for s in snapshots if s["thread_id"] != first["thread_id"]]
        assert len(conversation) == 2
        assert len({s["thread_id"] for s in conversation}) == 1
        assert len(requests) == 4
        assert "你好我是c" in json.dumps(requests[-1][1]["messages"], ensure_ascii=False)
        assert "recent_conversation" in interactive.stdout
        assert "Thread Memory:" in interactive.stdout
        context_view = cli(tmp_path, "context", first["state"]["run_id"])
        assert context_view.returncode == 0
        assert json.loads(context_view.stdout)["messages"] == requests[0][1]["messages"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

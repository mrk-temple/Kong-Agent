import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.mark.parametrize("command", ["/add preserve input", "/cancel"])
def test_real_cli_accepts_input_while_http_stream_is_incomplete(tmp_path, command):
    partial, release = threading.Event(), threading.Event()
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            def send(content, finish=None):
                event = {"choices": [{"index": 0, "delta": {"content": content}, "finish_reason": finish}]}
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
            try:
                if len(requests) == 1:
                    send('{"kind":"act",')
                    partial.set()
                    release.wait(5)
                    send('"actions":[{"name":"write_file","arguments":{"path":"obsolete.txt","content":"old"}}]}', "stop")
                else:
                    assert "preserve input" in json.dumps(body["messages"])
                    send('{"kind":"ask","question":"new constraint applied"}', "stop")
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = tmp_path / "model.toml"
    config.write_text(f'default_profile="test"\n[profiles.test]\nbase_url="http://127.0.0.1:{server.server_port}/v1"\nmodel="fixture"\nstream=true\n', encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    process = subprocess.Popen([sys.executable, "-m", "kong", "--workspace", str(tmp_path), "--config", str(config),
        "--global-memory-db", str(tmp_path / "global.db"), "run", "write a file"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env)
    try:
        assert partial.wait(8)
        assert not (tmp_path / "obsolete.txt").exists()
        process.stdin.write(command + "\n")
        process.stdin.flush()
        # Hold the provider response until the CLI acknowledges the input queue.
        # Reading lines avoids any arbitrary sleep used as an approval signal.
        initial = []
        while True:
            line = process.stdout.readline()
            initial.append(line)
            if "已排队" in line:
                break
            assert line, "CLI exited without queue acknowledgement"
        release.set()
        stdout, stderr = process.communicate(input="/quit\n", timeout=12)
        assert process.returncode == 0, "".join(initial) + stdout + stderr
        saved = json.loads(next((tmp_path / ".kong" / "runs").glob("*.json")).read_text(encoding="utf-8"))
        assert not (tmp_path / "obsolete.txt").exists() and not saved["history"]["observations"]
        assert saved["state"]["status"] == ("stopped" if command == "/cancel" else "waiting_user")
        assert any(e["kind"] == "input_applied" for e in saved["run_events"])
        assert len(requests) == (1 if command == "/cancel" else 2)
    finally:
        release.set()
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=3)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_cli_json_lines_events_match_saved_run(tmp_path):
    from test_cli import cli
    result = cli(tmp_path, "--events-json", "demo", "--auto-approve")
    assert result.returncode == 0, result.stdout + result.stderr
    records = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{"version"')]
    assert records[-1]["kind"] == "completed" and records[-1]["status"] == "completed"
    assert [e["sequence"] for e in records] == list(range(1, len(records) + 1))
    snapshot = json.loads(next((tmp_path / ".kong" / "runs").glob("*.json")).read_text(encoding="utf-8"))
    assert records == snapshot["run_events"]


def test_offline_comparison_has_real_outputs_and_reproducible_record(tmp_path):
    script = Path(__file__).resolve().parents[1] / "examples" / "interactive_comparison.py"
    process = subprocess.run([sys.executable, str(script), "--output", str(tmp_path / "comparison")],
        capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert process.returncode == 0, process.stdout + process.stderr
    report = json.loads(process.stdout)
    assert report["identical_results"] and report["all_passed"]
    assert report["concurrency_observed"]
    assert [row["max_active_reads"] for row in report["reads"]] == [1, 3]
    assert len(report["reads"][1]["results"]) == 6
